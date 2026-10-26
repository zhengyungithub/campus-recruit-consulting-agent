"""第1期：Planner —— 把"一句话"变成"几步能做"的执行计划。

为什么要有这一层：
    原来的图是 intent → (agent ↔ act)* → verify：让 LLM 从零决定每一步，
    遇到复合问题（"我到哪一步了？另外体检要查什么"）容易只答一半，
    且重复调同一工具会陷入原地打转。Planner 先把候选步骤排出来写进提示词，
    Agent 只需要在清单上打勾，既降低成本也便于观测与评测。

设计取舍：
    - 默认走**规则规划**（零延迟、零网络、结果可解释）；只有把 agent_planner 设为 llm
      且规则判定为多步时才让模型拆步骤（成本换覆盖）。
    - 规则/模型产出的步骤都要经过工具白名单校验，非法步骤直接丢弃后回落到模板。
    - plan 只是**建议清单**：Agent 判断不适用时可以不用，最终仍由提示词红线兜底。
"""
import json
import os
import re
from typing import Optional

STEP_TEMPLATES = {
    "get_my_progress": "读取候选人当前所处环节与最近流转记录",
    "knowledge_search": "检索校招知识库相关政策/流程条目",
    "create_ticket": "创建反馈工单转人工处理",
    "list_my_tickets": "查看候选人历史反馈工单及处理状态",
}
VALID_TOOLS = set(STEP_TEMPLATES)

# 意图 → 默认步骤模板（goal 是给 Agent 看的"这一步要拿到什么"）
INTENT_PLANS = {
    "greeting": [],
    "progress_query": [{"tool": "get_my_progress", "goal": "读取本人当前环节与最近流转，据此陈述事实"}],
    # 注意：kb 类模板里不再列 create_ticket 可选步骤。
    # 早期版本列了"可选转人工"，结果把清单当成必须打勾的任务，知识库明明答得上来的问题
    # 也被硬建一张工单（评测集 case 10 就是这么被弄坏的）。是否转人工交给提示词红线判断。
    "kb_qa": [{"tool": "knowledge_search", "goal": "检索政策/流程依据后再回答"}],
    "human_handoff": [{"tool": "create_ticket", "goal": "该诉求需人工办理，建单并告知候选人"}],
    "out_of_scope": [{"tool": "create_ticket", "goal": "超出范围，建单并说明无法自动回答"}],
}

# ---- 子任务信号：一句话里可能同时包含"查进度"和"问政策"两种诉求 ----
PROGRESS_SIG = re.compile(
    r"(我[^，。？?！!]{0,6}的?(进度|状态|环节|结果|情况|简历|投递))"
    r"|(我(现在|目前|这儿)?(到|在|处于|走到|排到)[哪那])"
    r"|(我的?(offer|录用|体检|资料|测评|面试|网签|三方))", re.I)
KB_SIG = re.compile(
    r"(材料|资料|体检|网签|三方|流程|要求|政策|怎么|怎样|如何|哪些|什么时候|怎么办|注意|标准|格式|链接)", re.I)
TICKET_SIG = re.compile(
    r"(帮我|申请|我想|我要|能不|可不可以|催|加急|改期|换(站点|城市|岗|部门))", re.I)

# 多步问题的显式标记
MULTI_MARK = re.compile(r"(并且|而且|另外|还有|同时|顺便|以及|除此之外|其次|还有就是|，另)")
SENT_SPLIT = re.compile(r"[？?；;。！!]+")

# 指代不明、没有先行语的追问：需要反问澄清而不是瞎猜
VAGUE_RE = re.compile(
    r"^\s*(什么意思|怎么弄|怎么做|那呢|然后呢|这个呢|为什么|然后|是吗|真的吗|具体点|还有呢|ok|OK|嗯|哦)?\s*[？?！!。.\s]*$", re.I)


def detect_subtasks(question: str, intent: str) -> list:
    """返回本句涉及的子任务类型列表：progress / kb / ticket（去重、按优先级排序）"""
    q = (question or "").strip()
    if intent == "greeting":
        return []
    subs = []
    if intent in ("human_handoff", "out_of_scope"):
        subs.append("ticket")
    if intent == "progress_query" or PROGRESS_SIG.search(q):
        subs.append("progress")
    if intent == "kb_qa" or (intent != "greeting" and KB_SIG.search(q)):
        subs.append("kb")
    if not subs:
        subs.append("kb")
    # 去重建序：ticket 优先（必须人工办的先建单），再 progress → kb
    order = {"ticket": 0, "progress": 1, "kb": 2}
    return sorted(set(subs), key=lambda x: order[x])


def is_multi_step(question: str, subs: list) -> bool:
    """多步判定：出现并列连接词、多个问句，或同时命中两类及以上子任务"""
    q = question or ""
    if len(SENT_SPLIT.findall(q)) >= 2 and len(q) >= 8:
        return True
    if MULTI_MARK.search(q):
        return True
    return len(subs) >= 2


def need_clarify(question: str, has_history: bool) -> Optional[str]:
    """信息严重不足时反问澄清（只在没有上下文且问句极短/纯指代时触发）。

    为什么保守：反问是有成本的（用户要再输一次），宁可少问。
    所以只有"无历史上下文 + 纯感叹/指代/超短句"才澄清，其余一律尝试作答。
    """
    if has_history:
        return None
    q = (question or "").strip()
    core = re.sub(r"[\s，,。.！!？?~]+", "", q)
    if not core:
        return "你好像还没输入内容，可以说说想咨询的具体问题吗？例如：面试资料需要哪些？"
    if VAGUE_RE.match(q) and len(core) <= 4:
        return ("你的问题我还没太看明白 🤔 可以补充一下具体场景吗？\n"
                "比如：想问校招流程、某项材料怎么上传，还是想查自己的进度？")
    return None


def rule_plan(question: str, intent: str) -> dict:
    """按意图模板 + 子任务信号产出计划（默认路径，零延迟）"""
    subs = detect_subtasks(question, intent)
    multi = is_multi_step(question, subs)
    steps = []
    if multi:
        # 复合问题：按"查事实 → 查依据 → 收尾转人工"的顺序排步骤
        if "progress" in subs:
            steps.append({"tool": "get_my_progress", "goal": "读取本人当前环节（复合问题的一问）"})
        if "kb" in subs:
            steps.append({"tool": "knowledge_search", "goal": "检索政策依据（复合问题的另一问）"})
        if "ticket" in subs:
            steps.append({"tool": "create_ticket", "goal": "需要人工办理的那部分建单（不必等第二轮）"})
        # 复合问题的知识那半不再挂"可选转人工"，避免清单诱导多建单（理由见 INTENT_PLANS 注释）
    else:
        steps = list(INTENT_PLANS.get(intent, []))
    if not steps and multi:                      # 兜底：复合但子任务未识别，退回意图模板
        steps = list(INTENT_PLANS.get(intent, []))
    for s in steps:
        s.setdefault("goal", STEP_TEMPLATES.get(s["tool"], ""))
    return {"source": "rule", "multi": multi, "subtasks": subs, "steps": steps}


def llm_plan(question: str, intent: str) -> Optional[dict]:
    """模型规划（可选）。任何异常/非法工具名都返回 None，由调用方回落到规则计划。"""
    try:
        from .llm_service import get_chat_model
        from . import config, prompts
        if not config.llm_ready():
            return None
        llm = get_chat_model(temperature=0)
        raw = llm.invoke(prompts.load("plan_llm").format(intent=intent, q=question[:200]))
        content = getattr(raw, "content", "") or ""
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.S)
        if m:
            content = m.group(1)
        s, e = content.find("{"), content.rfind("}")
        if s < 0 or e <= s:
            return None
        data = json.loads(content[s:e + 1])
        steps = [{"tool": x.get("tool", ""), "goal": (x.get("goal") or "")[:40]}
                 for x in (data.get("steps") or []) if x.get("tool") in VALID_TOOLS]
        if not steps:
            return None
        return {"source": "llm", "multi": True, "subtasks": [], "steps": steps[:3]}
    except Exception:
        return None


def use_llm_planner() -> bool:
    """是否启用模型规划：默认关闭（规则优先，零延迟）。开启：环境变量/DB settings agent_planner=llm"""
    try:
        from . import config
        v = config.get_setting("agent_planner")
    except Exception:
        v = None
    return (v or os.getenv("AGENT_PLANNER", "rule")).lower() == "llm"


def make_plan(question: str, intent: str, has_history: bool = False) -> dict:
    """产出执行计划：{source, multi, subtasks, steps, clarify}"""
    clarify = need_clarify(question, has_history)
    plan = rule_plan(question, intent)
    if plan["multi"] and use_llm_planner():
        llm_out = llm_plan(question, intent)
        if llm_out:
            plan = {**plan, **llm_out}
    plan["clarify"] = clarify
    return plan


def plan_prompt(plan: dict, failures: list, tool_trace: list) -> str:
    """把计划 + 已完成 + 已失败写成给 Agent 看的提示词块"""
    if not plan or not plan.get("steps"):
        return ""
    done_tools = {t["tool"] for t in (tool_trace or [])}
    lines = []
    for i, s in enumerate(plan["steps"], 1):
        mark = "✅已完成" if s["tool"] in done_tools else ("（可选）" if s.get("optional") else "⬜待办")
        lines.append(f"{i}. {s['tool']} —— {s.get('goal','')} [{mark}]")
    block = "【执行计划】（建议清单，按你的判断增删；已完成的不要重复调用）\n" + "\n".join(lines)

    if failures:
        block += "\n\n【已发生的失败，请换路不要硬撞】\n"
        for f in failures[-3:]:
            block += f"- {f.get('tool','?')}：{f.get('kind','')} → {f.get('advice','')}\n"
    return block


FAILURE_ADVICE = {
    "unknown_tool": "工具不存在，请改用 knowledge_search / get_my_progress / create_ticket",
    "exception": "执行异常，请换一种问法或直接改用其他工具",
    "empty_result": "检索无结果，尝试更短的同义关键词再检索一次；仍无结果请改为建议转人工",
    "duplicate": "同一参数已调用过且拿到过结果，不要再重复调用，直接据此作答",
}
