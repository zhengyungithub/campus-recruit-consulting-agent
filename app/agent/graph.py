"""LangGraph 编排（第1期：planner → executor ↔ act → critic，含预算与降级）。

图结构：
    START → intent → plan ─┬→ clarify（信息不足反问）→ END
                           └→ agent ⇄ act ─┬→ critic → END
                                           └→ degrade（超预算/自我修复失败）→ END
                          （critic 未通过且仍有余量 → 回炉补检索）

相比第0期的变化：
    1. 新增 plan 节点：先把"要做几步"排出来写进提示词（见 planner.py），
       Agent 在清单上执行，复合问题不再被答一半。
    2. act 节点做失败分类（unknown_tool / exception / empty_result / duplicate），
       失败原因与建议回喂给下一次推理，支持一次"换路自救"。
    3. 全链路带预算：步数 / 工具调用次数 / 墙钟耗时任一超限即降级到固定管线，
       保证用户永远拿得到回答（可用即可观测）。
"""
import contextvars
import json
import time
from typing import Annotated, List, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from . import config, coref, guard, handling, memory, planner, prompts, retrieval, verifier
from . import intent as intent_mod
from .llm_service import get_chat_model
from .tools import TOOLS_REGISTRY

# ---- 事件外发（SSE 用）：request 级 sink，由 service 设置 ----
_sink: contextvars.ContextVar = contextvars.ContextVar("agent_sink", default=None)


def emit(event: dict):
    sink = _sink.get()
    if sink:
        sink.put(event)


class AgentState(TypedDict):
    candidate_id: int
    question: str
    # add_messages reducer：节点返回的消息是"追加"而非"替换"——
    # 否则 agent/act 各自返回的片段会覆盖完整历史，导致 tool_call_id 失配
    messages: Annotated[list, add_messages]
    citations: Annotated[List[dict], lambda a, b: a + b]   # 检索到的引用（只增）
    tool_trace: Annotated[List[dict], lambda a, b: a + b]
    tool_results: Annotated[List[dict], lambda a, b: a + b]  # 结果缓存：识别重复调用、给警惕依据
    failures: Annotated[List[dict], lambda a, b: a + b]      # 失败记录：tool/kind/advice
    answer: str
    verify: dict
    revisions: int
    intent: str
    intent_meta: dict
    # --- 第1期新增 ---
    plan: dict          # planner 产出的执行计划
    agent_steps: int    # 推理步数（预算）
    started_at: float   # 墙钟起点（预算）
    degraded: bool      # 是否走了降级固定管线
    failed: bool        # 模型调用失败标记：直接走降级，不再空转重试
    clarify: str        # 需要反问澄清时的文案
    # --- 第2期：红线（Redline）治理 ---
    redline_retries: int  # 因红线回炉重写的次数（红线最多给一次改过机会）
    redline_blocked: bool # 二次仍踩红线：禁止放行，强制降级到固定管线
    # --- 第4期：多轮状态机（改期/换站点/催办的槽位收集） ---
    handling_ctx: str   # 本轮开头算好的"办理中"提示块（避免节点内重复处理）
    handling_filled: bool   # 本轮补齐了槽位：不再建单，只确认
    handling_ticket: int    # 该办理事项对应的工单号
    # --- 第3期：记忆与个性化 ---
    resolved_question: str  # 指代还原后的自足问题（下游各决策点统一用它）
    referent: str           # 还原所依据的上文主题锚点
    repeat: bool            # 是否与上一轮高度重复（重复问要收敛，不要照抄长答案）
    profile: str            # 候选人画像自然语言块
    context_block: str      # 一次性算好的记忆注入块（回炉不再重复embedding）
    memory_enabled: bool    # False=评测隔离模式：不读历史、不做指代还原


# ---------------------------------------------------------------- 预算控制
def budget_exceeded(state: dict):
    """返回超限原因；未超限返回 None。预算值见 config（可用环境变量覆盖）。"""
    steps = state.get("agent_steps") or 0
    tools = len(state.get("tool_trace") or [])
    elapsed = time.time() - (state.get("started_at") or time.time())
    if steps >= config.AGENT_MAX_STEPS:
        return f"推理步数达上限（{steps}/{config.AGENT_MAX_STEPS}）"
    if tools >= config.AGENT_MAX_TOOL_CALLS:
        return f"工具调用次数达上限（{tools}/{config.AGENT_MAX_TOOL_CALLS}）"
    if elapsed > config.AGENT_DEADLINE_S:
        return f"单次运行耗时超限（{elapsed:.0f}s>{config.AGENT_DEADLINE_S}s）"
    return None


# ---------------------------------------------------------------- 节点
def _last_ticket_id(final: dict, cid: int = None):
    """本轮新建的工单号。

    tool_trace 只记 {tool,args}，不含返回内容——工单号在 tool_results 的 result 里
    （create_ticket 返回 JSON）。两处都取不到时，回退到"该候选人最新一张工单"，
    宁可多查一次库，也不能让状态机拿不到单号（拿不到就会导致下一轮重复建单）。
    """
    import re as _re
    for t in reversed(final.get("tool_results") or []):
        if t.get("tool") != "create_ticket":
            continue
        m = _re.search(r'"ticket_id"\s*:\s*(\d+)', str(t.get("result", "")))
        if m:
            return int(m.group(1))
    if cid:
        from .store import get_conn
        with get_conn() as conn:
            row = conn.execute("SELECT id FROM tickets WHERE candidate_id=? "
                               "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        return row["id"] if row else None
    return None


def node_context(state: AgentState):
    """第3期记忆节点：画像 + 历史召回 + 追问指代还原 + 重复问检测。

    为什么要单独一个节点（而不是散在各节点里各自查一遍）：
        history recall 要走一次 Embedding，回炉重答时如果每轮重算，
        用户要为同一次提问多等好几秒。这里算一次写进 context_block，后续复用。
    """
    cid, q = state["candidate_id"], state["question"]
    enabled = state.get("memory_enabled", True)
    prev_q = memory.last_user_question(cid) if enabled else ""
    r = coref.resolve(q, prev_question=prev_q)
    repeat = bool(prev_q and enabled and retrieval.text_similarity(prev_q, q) >= 0.85)
    profile = memory.profile_block(cid) if enabled else ""

    parts = []
    if profile:
        parts.append("【候选人画像】\n" + profile)
    if enabled:
        hist = retrieval.recall_history(cid, r["question"])
        if hist:
            parts.append("相似历史问答（供参考，不要当作最新事实）：\n" +
                         "\n".join(f"Q：{h['question']}\nA：{h['answer'][:200]}" for h in hist))
    if r["resolved"]:
        parts.append(f"【本轮已做指代还原】用户原话是「{r['original']}」，"
                     f"承接上文「{r['anchor']}」，实际要问的是：{r['question']}")
    if repeat:
        parts.append("【重复提问】同一问题刚问过，请简明重述要点（不要再输出一遍完整长答案），"
                     "并追问是否需要更详细的说明或转人工。")
    # S4.2 多轮状态机：正在进行中的办理事项（改期/换站点/催办），
    # 告知模型"已建过单、缺哪一项"，避免它被当成新问题另建一张工单。
    if enabled:
        # 优先用 run_agent 算好的结果（那里已处理过"回填工单"），
        # 只在直接调节点时（如评测/脚本）才现算一次
        hb = state.get("handling_ctx") or handling.context_block(cid, r["question"])
        if hb:
            parts.append(hb)
    block = "\n\n".join(parts)

    emit({"type": "memory", "profile": profile[:120], "repeat": repeat,
          "resolved": r["question"] if r["resolved"] else None,
          "referent": r["anchor"] if r["resolved"] else None})
    emit({"type": "stage", "name": "memory",
          "label": ("🧠 记忆：" + ("重复提问" if repeat else
                                 ("指代还原 → " + r["question"] if r["resolved"] else "已加载画像")))})
    return {"resolved_question": r["question"], "referent": r["anchor"], "repeat": repeat,
            "profile": profile, "context_block": block}


def node_intent(state: AgentState):
    """意图识别节点：规则优先，模糊时 LLM 兜底；结果外发 SSE intent 事件。"""
    # 用指代还原后的问题判定意图，否则"那要多久"这类追问只能靠猜
    q = state.get("resolved_question") or state["question"]
    r = intent_mod.classify(q, state.get("candidate_id"), emit_fn=emit)
    # 追问被还原过 → 纠偏：延续上一件事的政策咨询不该判成查个人进度
    if state.get("referent") and r.get("source") != "rule":
        r = intent_mod.keep_after_coref(r, state["question"])
        emit({"type": "intent", "intent": r["intent"], "label": intent_mod.INTENT_LABELS.get(r["intent"]),
              "confidence": r.get("confidence"), "source": r.get("source"), "reason": r.get("reason")})
    return {"intent": r["intent"], "intent_meta": r}


def node_plan(state: AgentState):
    """第1期规划节点：意图 → 执行计划（多为规则直出，可选 LLM 拆解），并给出是否需澄清。"""
    # 有历史对话时不轻易反问：追问"什么意思"是有先行语的，正常作答即可
    has_history = sum(1 for m in state.get("messages", [])
                      if getattr(m, "type", "") == "human") > 1
    p = planner.make_plan(state.get("resolved_question") or state["question"],
                          state.get("intent") or "kb_qa", has_history)
    # S4.2：槽位补齐轮不排 create_ticket（工单已存在），计划置空避免模型照清单再建一张
    if state.get("handling_filled"):
        p = {"source": "rule", "multi": False, "steps": [], "clarify": False}
    emit({"type": "plan", "source": p.get("source"), "multi": bool(p.get("multi")),
          "steps": [s["tool"] for s in p.get("steps", [])],
          "clarify": bool(p.get("clarify"))})
    emit({"type": "stage", "name": "plan",
          "label": ("🧭 规划：" + " → ".join(s["tool"] for s in p["steps"])) if p["steps"]
                   else "🧭 规划：无需工具"})
    out = {"plan": p}
    if p.get("clarify"):
        out["clarify"] = p["clarify"]
    return out


def node_clarify(state: AgentState):
    """信息不足：不猜，直接反问（比生成一个似是而非的答案更负责）"""
    text = state.get("clarify") or "可以补充一下具体场景吗？我会根据你的情况精确回答。"
    emit({"type": "stage", "name": "clarify", "label": "🙋 信息不足，反问澄清"})
    return {"answer": text}


def node_agent(state: AgentState):
    """Executor 推理节点：带执行计划与失败反馈，决定调工具还是产出回答"""
    llm = get_chat_model()
    llm_with_tools = llm.bind_tools(list(TOOLS_REGISTRY.values()))
    msgs = list(state["messages"])
    it = state.get("intent") or "kb_qa"
    rq = state.get("resolved_question") or state["question"]
    # 追问被还原后要同步替换最后一条用户消息，否则模型只能看到"那这个要多久"
    if rq != state["question"] and msgs and getattr(msgs[-1], "type", "") == "human":
        msgs[-1] = HumanMessage(content=f"{rq}\n（用户原话：{state['question']}）")
    ctx = state.get("context_block")
    if ctx is None:                     # 兜底：context 节点未跑过（如直接单测）
        ctx = memory.build_context_block(state["candidate_id"], rq)
    # 计划清单 + 已失败项的换路建议注入 system（DashScope 兼容层要求 system 在开头）
    plan_block = planner.plan_prompt(state.get("plan") or {}, state.get("failures") or [],
                                     state.get("tool_trace") or [])
    # S4.2：本轮补上了办理事项的槽位 → 工单早已建好，这里必须压掉"再建一张"的倾向，
    # 否则用户每补充一条信息就多一张工单（实测出现过 #182/#183 连开两张）
    if state.get("handling_filled"):
        intent_hint = (f"候选人正在补充此前办理事项的信息，工单 #{state.get('handling_ticket')} 已存在。"
                       f"**本轮不要调用 create_ticket，也不要新建任何工单**；"
                       f"只需用一两句话确认收到补充即可。"
                       f"不要复述工单处理流程、不要承诺处理时效或结果——"
                       f"（这类复述会被核验判成「无依据承诺」，第4期踩过）")
    else:
        intent_hint = intent_mod.route_hint(it)
    hint = SystemMessage(content=prompts.load("system").format(
        cid=state["candidate_id"], context=ctx,
        intent_label=intent_mod.INTENT_LABELS.get(it, it),
        intent_hint=intent_hint, plan=plan_block))
    msgs[0] = hint
    try:
        resp = llm_with_tools.invoke(msgs)
    except Exception as e:                      # 模型不可用：标记失败，由路由直接降级兜底
        emit({"type": "stage", "name": "error", "label": "⚠️ 模型调用失败，转降级"})
        return {"messages": [HumanMessage(content=f"（模型调用失败：{e}）")],
                "agent_steps": (state.get("agent_steps") or 0) + 1, "failed": True, "answer": "",
                # 记进 failures，评测才能把"模型挂了"和"Agent 答不好"区分开
                "failures": [{"tool": "llm", "kind": "llm_error", "advice": repr(e)[:160]}]}
    upd = {"messages": [resp], "agent_steps": (state.get("agent_steps") or 0) + 1}
    if not getattr(resp, "tool_calls", None):      # 无工具调用 = 本轮产出最终回答
        upd["answer"] = resp.content or ""
    return upd


def _cached_result(state, tool_name, args):
    for r in state.get("tool_results") or []:
        if r.get("tool") == tool_name and r.get("args") == args:
            return r.get("result")
    return None


def node_act(state: AgentState):
    """自建 act：逐个执行 tool_calls；失败分类并回喂建议。
    注意：必须为每个 tool_call 返回 ToolMessage（不可提前 return），
    否则下一轮 LLM 调用会因 tool_call 无应答被 DashScope/DeepSeek 拒绝。"""
    last = state["messages"][-1]
    out, trace, cites, results, failures = [], [], [], [], []
    for tc in last.tool_calls:
        tool = TOOLS_REGISTRY.get(tc["name"])
        tid = tc["id"]

        # ⓪ 补槽位轮硬拦截建单：工单已存在，再建一张就是工单洪水。
        # 提示词只是软约束，模型偶尔仍会照做（第5期全量回归实测 #40 偶发建单），
        # 这类"多建一张就是脏数据"的分支必须规则层兜底。
        if state.get("handling_filled") and tc["name"] == "create_ticket":
            tid_ = state.get("handling_ticket")
            msg = (f"该办理事项已有工单{f' #{tid_}' if tid_ else ''}，无需再次创建。"
                   f"直接用一两句话确认已收到候选人的补充信息即可。")
            out.append(ToolMessage(content=msg, tool_call_id=tid))
            continue

        # ① 未知工具
        if tool is None:
            failures.append({"tool": tc["name"], "kind": "unknown_tool",
                             "advice": planner.FAILURE_ADVICE["unknown_tool"]})
            out.append(ToolMessage(content=planner.FAILURE_ADVICE["unknown_tool"], tool_call_id=tid))
            continue

        # ② 重复调用（同工具同参数）：直接复用缓存结果，打断原地打转
        cached = _cached_result(state, tc["name"], tc["args"])
        if cached is not None:
            failures.append({"tool": tc["name"], "kind": "duplicate",
                             "advice": planner.FAILURE_ADVICE["duplicate"]})
            out.append(ToolMessage(content=f"{cached}\n【提示】{planner.FAILURE_ADVICE['duplicate']}",
                                   tool_call_id=tid))
            continue

        # ③ 执行异常
        try:
            result = tool.invoke(tc["args"], config={"configurable": {
                "candidate_id": state["candidate_id"], "emit": emit}})
        except Exception as e:
            failures.append({"tool": tc["name"], "kind": "exception",
                             "advice": planner.FAILURE_ADVICE["exception"]})
            out.append(ToolMessage(content=f"{planner.FAILURE_ADVICE['exception']}（{e}）",
                                   tool_call_id=tid))
            continue

        results.append({"tool": tc["name"], "args": tc["args"], "result": result})
        trace.append({"tool": tc["name"], "args": tc["args"]})

        # ④ 空结果（知识库未命中）：给出一个可以自查的自愈方向
        content = result
        if tc["name"] == "knowledge_search":
            try:
                data = json.loads(result)
                if data.get("results"):
                    # 多次检索要累加（原来被最后一次覆盖，导致前面检索到的依据在核验时丢失）
                    for r in data["results"]:
                        if r.get("question") not in {c.get("question") for c in cites}:
                            cites.append(r)
                else:
                    failures.append({"tool": tc["name"], "kind": "empty_result",
                                     "advice": planner.FAILURE_ADVICE["empty_result"]})
                    content = f"{result}\n【提示】{planner.FAILURE_ADVICE['empty_result']}"
            except Exception:
                pass
        out.append(ToolMessage(content=content, tool_call_id=tid))
    return {"messages": out, "citations": cites, "tool_trace": trace,
            "tool_results": results, "failures": failures}


def _self_identifiers(cid: int, tool_results: list | None = None) -> list:
    """核验白名单：本人信息 + 本轮自己刚创建的工单号。

    本人手机号/姓名：向本人复述本人信息不算泄露隐私。
    工单号：它是系统本次生成的（create_ticket 返回），不是"引用的事实"，
            若不豁免就会被硬信息比对判成编造数字——out_of_scope 的转人工答复会莫名 fail。
    """
    allow = []
    try:
        from .store import get_conn
        with get_conn() as conn:
            row = conn.execute("SELECT name, phone FROM candidates WHERE id=?", (cid,)).fetchone()
        if row:
            allow += [v for v in (row["name"], row["phone"]) if v]
    except Exception:
        pass
    for r in tool_results or []:
        if r.get("tool") != "create_ticket":
            continue
        try:
            allow.append(str(json.loads(r.get("result") or "{}").get("ticket_id")))
        except Exception:
            pass
    return [a for a in allow if a]


def node_critic(state: AgentState):
    """Critic（第2期：双通道核验 + 红线治理）

    L1 规则：红线扫描 / 硬信息比对 / 句子级引用锚点（零延迟）
    L2 LLM 裁判：规则卡住或踩红线时才调用，做语义级裁定

    处置策略：
        红线     → 給一次改写机会；二次仍踩 → 禁止放行，直接降级固定管线
        fail     → 回炉补检索一次
        其余     → 放行
    """
    answer = state.get("answer", "")
    # 两阶段：先用纯规则判断"要不要回炉"（零延迟，不因等 Judge 而让用户多等一轮），
    # 只有在确定要放行时才补一次 Judge 做质量观测——延迟只加在最后一次，不叠加。
    v = verifier.verify_answer(answer, state.get("citations", []),
                               question=state.get("question", ""),
                               allow_values=_self_identifiers(state.get("candidate_id"),
                                                              state.get("tool_results")),
                               use_judge=False)
    emit({"type": "verify", "status": v["status"], "ratio": v["ratio"],
          "redlines": [r["code"] for r in v.get("redlines", [])],
          "judge": (v.get("judge") or {}).get("verdict")})

    redlines = v.get("redlines") or []
    if redlines:
        names = "、".join(r["name"] for r in redlines)
        emit({"type": "stage", "name": "redline", "label": f"🛡 红线拦截：{names}"})
        if (state.get("redline_retries") or 0) < 1 and not budget_exceeded(state) \
                and not state.get("failed"):
            tips = "；".join(f"{r['evidence']} → {r['advice']}" for r in redlines)
            correction = HumanMessage(content=(
                f"你上一版回答触发了合规红线【{names}】，必须先修掉再作答：\n{tips}\n"
                "请删除或修改上述表述，用中性、有依据的方式重新回答；"
                "不确定时说明以官方通知为准或建议转人工，不要再说一句承诺/给出无依据的时间点。"))
            return {"messages": [correction], "verify": v, "answer": "",
                    "redline_retries": (state.get("redline_retries") or 0) + 1}
        # 二次仍踩红线：不再相信它的自我修正，交给固定管线兜底（保守模板，绝不承诺）
        emit({"type": "stage", "name": "redline_block",
              "label": "🛡 二次红线，拒绝放行，转固定管线"})
        return {"verify": v, "answer": "", "redline_blocked": True}

    if v["status"] == "fail" and state.get("revisions", 0) < 1 and not budget_exceeded(state):
        emit({"type": "stage", "name": "revise", "label": "🔁 校验未过，回炉补检索"})
        # 纠偏消息用 Human 而非 System：DashScope/DeepSeek 兼容层要求 system 只能出现在开头
        offenders = v.get("unsupported") or v.get("ungrounded") or []
        correction = HumanMessage(content=(
            "你上一版回答包含引用原文无法支撑的内容："
            f"{offenders[:5]}。请重新调用 knowledge_search 核实，"
            "或删除无法支撑的内容后重新回答；仍无法核实的改为建议转人工。"))
        return {"messages": [correction], "verify": v,
                "revisions": state.get("revisions", 0) + 1, "answer": ""}
    # 确定放行：只有规则已经卡住（partial/fail）时才补一次 Judge，
    # pass 的用例不再白花一次模型调用（实测每条 +13~20s，P95 会被推到 37s）。
    if answer and v["status"] != "pass" and verifier._judge_enabled(config.AGENT_JUDGE):
        try:
            jv = verifier.judge_answer(state.get("question", ""), answer,
                                       state.get("citations", []))
            verifier.apply_judge(v, jv)
            emit({"type": "judge", "verdict": jv.get("verdict"), "note": jv.get("note", ""),
                  "issues": len(jv.get("issues") or [])})
        except Exception:
            pass
    return {"verify": v}


def node_degrade(state: AgentState):
    """降级：预算耗尽/反复失败时，用固定管线保证交付一个确定的回答"""
    reason = budget_exceeded(state) or "执行链路未能收敛"
    emit({"type": "stage", "name": "degrade", "label": "⚠️ 超预算，降级固定管线"})
    emit({"type": "degrade", "reason": reason})
    try:
        from .pipeline import fixed_chat_reply
        reply, _tid, cites = fixed_chat_reply(state["candidate_id"],
                                              state.get("resolved_question") or state["question"],
                                              allow_ticket=True)
    except Exception as e:                      # 连固定管线都失败，退回静态兜底
        reply, cites = (f"抱歉，这次没能调用到完整的推理链路（{reason}），"
                        f"你的问题我已记录，建议稍后重试或在【我的反馈】中查看人工回复。"), []
    return {"answer": reply, "citations": cites or [], "degraded": True}


# ---------------------------------------------------------------- 路由
def route_after_plan(state: AgentState):
    return "clarify" if state.get("plan", {}).get("clarify") else "agent"


def route_after_agent(state: AgentState):
    if state.get("failed"):
        return "degrade"
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return "verify"
    return "degrade" if budget_exceeded(state) else "act"


def route_after_act(state: AgentState):
    # 工具已执行完仍未产出回答，且预算用尽 → 直接兜底，别再空转
    return "degrade" if budget_exceeded(state) else "agent"


def route_after_verify(state: AgentState):
    if state.get("redline_blocked"):      # 红线不放行：即使还有预算也必须降级
        return "degrade"
    if state.get("answer"):
        return END
    return "degrade" if (budget_exceeded(state) or state.get("failed")) else "agent"


def build_graph():
    from .runtime import get_checkpointer
    g = StateGraph(AgentState)
    g.add_node("context", node_context)  # 第3期：画像/指代还原/重复检测，只算一次
    g.add_node("intent", node_intent)    # 入口先做意图识别，再规划，最后进入执行循环
    g.add_node("plan", node_plan)
    g.add_node("clarify", node_clarify)
    g.add_node("agent", node_agent)
    g.add_node("act", node_act)
    g.add_node("verify", node_critic)
    g.add_node("degrade", node_degrade)
    g.add_edge(START, "context")
    g.add_edge("context", "intent")
    g.add_edge("intent", "plan")
    g.add_conditional_edges("plan", route_after_plan, {"agent": "agent", "clarify": "clarify"})
    g.add_edge("clarify", END)
    g.add_conditional_edges("agent", route_after_agent,
                            {"act": "act", "verify": "verify", "degrade": "degrade"})
    g.add_conditional_edges("act", route_after_act, {"agent": "agent", "degrade": "degrade"})
    g.add_conditional_edges("verify", route_after_verify,
                            {"agent": "agent", "degrade": "degrade", END: END})
    g.add_edge("degrade", END)
    return g.compile(checkpointer=get_checkpointer())


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def run_agent(cid: int, question: str, thread_id: str, use_history: bool = True) -> dict:
    """同步执行一次 agent；返回 {answer, citations, verify, tool_trace, plan, failures, degraded}

    use_history=False（评测用）：不读历史记忆、也不回写记忆。
    同一账号连跑多条用例时，历史会把上一条的答案带进下一条上下文造成串味，
    评测必须隔离；线上对话则相反，需要记忆保持连贯。

    S1.2 注入检测：提问入口先过 guard 规则库，命中直接拒绝（不进图、不调模型），
    避免注入文本被写进 messages / 向量记忆 / 检查点。
    """
    hits = guard.find_hits(question)
    if hits:
        t0 = time.time()
        emit({"type": "stage", "name": "guard", "label": "🛡️ 已拦截异常请求"})
        emit({"type": "verify", "status": "block", "ratio": 0,
              "advice": "提问命中注入检测规则：" + "；".join(h["label"] for h in hits[:3])})
        # 审计留痕：与正常轨迹同表，verify_status=block，verify_ratio=0，
        # question 原文入表（审计需要），但 answer 只存拒绝话术、不产生任何 LLM 副作用
        from . import runtime
        runtime.save_run(cid, question, "agent", [],
                         guard.REFUSAL_TEXT, "", "block", 0,
                         int((time.time() - t0) * 1000) or 1,
                         prompts.fingerprint())
        return {"answer": guard.REFUSAL_TEXT, "citations": [],
                "verify": {"status": "block", "ratio": 0,
                           "advice": "命中注入检测：" + "、".join(h["label"] for h in hits)},
                "tool_trace": [], "plan": {}, "failures": [], "degraded": False,
                "resolved_question": question, "referent": "", "repeat": False,
                "profile": "", "intent": "injection", "guard_hits": hits}
    graph = get_graph()
    # 入口 system 只放静态规则；意图提示与计划由 node_agent 在识别/规划后注入
    ctx = prompts.load("system").format(
        cid=cid,
        context=memory.build_context_block(cid, question) if use_history else "",
        intent_label="待判定", intent_hint="按规则 1~6 正常编排。", plan="")
    msgs = [SystemMessage(content=ctx)]
    if use_history:
        msgs += [HumanMessage(content=m["content"]) if m["role"] == "user"
                 else AIMessage(content=m["content"]) for m in memory.recent_messages(cid)]
    msgs.append(HumanMessage(content=question))
    emit({"type": "stage", "name": "agent", "label": "🧭 理解问题并编排"})
    # S4.2 状态机：本轮开头先查有没有"办理中"的会话——用户上一轮被追问的槽位，
    # 如果这一轮补上了，就直接回填到那张工单上（在这里算一次，避免节点里重复处理）
    pre = handling.before_turn(cid, question) if use_history else {"filled": None, "note": ""}
    cfg = {"configurable": {"thread_id": thread_id, "candidate_id": cid}}
    final = graph.invoke({"candidate_id": cid, "question": question, "messages": msgs,
                          "citations": [], "tool_trace": [], "tool_results": [], "failures": [],
                          "revisions": 0, "intent": "", "intent_meta": {},
                          "plan": {}, "agent_steps": 0, "started_at": time.time(),
                          "degraded": False, "failed": False, "clarify": "",
                          "redline_retries": 0, "redline_blocked": False,
                          "resolved_question": question, "referent": "", "repeat": False,
                          "profile": "", "context_block": "",
                          "handling_ctx": pre.get("context", ""),
                          "handling_filled": bool(pre.get("filled")),
                          "handling_ticket": pre.get("ticket_id"),
                          "memory_enabled": use_history}, cfg)
    answer = final.get("answer", "")
    # 状态机的两段追加：① 槽位补齐的确认语；② 新开办理事项的追问语
    if pre.get("note"):
        answer = (answer or "").rstrip() + pre["note"]
        emit({"type": "stage", "name": "handling",
              "label": f"🧩 已补充：{pre.get('filled')}（工单 #{pre.get('ticket_id')}）"})
    intent_now = final.get("intent", "") or (final.get("intent_meta") or {}).get("intent", "")
    if use_history:
        ask = handling.after_turn(cid, question, intent_now, _last_ticket_id(final, cid))
        if ask:
            answer = (answer or "").rstrip() + ask
            emit({"type": "stage", "name": "handling", "label": "🧩 办理信息待补充，已追问"})
    if answer and use_history:
        memory.remember(cid, question, answer)
    return {"answer": answer, "citations": final.get("citations", []),
            "verify": final.get("verify", {}), "tool_trace": final.get("tool_trace", []),
            "plan": final.get("plan", {}), "failures": final.get("failures", []),
            "degraded": bool(final.get("degraded")),
            "resolved_question": final.get("resolved_question", question),
            "referent": final.get("referent", ""), "repeat": bool(final.get("repeat")),
            "profile": final.get("profile", ""),
            # 返回图中真正采用的意图（含追问纠偏），供评测/观测对齐
            "intent": final.get("intent", "") or (final.get("intent_meta") or {}).get("intent", "")}
