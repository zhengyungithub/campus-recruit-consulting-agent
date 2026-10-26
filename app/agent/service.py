"""服务层：三档灰度（off/shadow/on）+ SSE 富事件流 + shadow 旁路对比"""
import contextvars
import queue
import random
import threading
import time
from datetime import datetime

from . import config, graph, memory, pii, ratelimit
from . import intent as intent_mod
from .pipeline import fixed_chat_reply
from .store import get_conn
from . import prompts, runtime


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _record_user_message(cid, msg):
    """落库用户消息。

    这里做 PII 掩码（S1.3）——**只改写入副本，不改当次请求的原文**：
    graph 里流动的文本仍是原文，工具调用与检索不受影响。
    工单表同理保留原文（HR 要拿它处理业务），只有对话历史与向量记忆脱敏。
    """
    safe, _hits = pii.mask_pii(msg)
    with get_conn() as conn:
        conn.execute("INSERT INTO messages(candidate_id,role,content,created_at) VALUES(?,?,?,?)",
                     (cid, "user", safe, _now()))


def _bot_reply(cid, text):
    with get_conn() as conn:
        conn.execute("INSERT INTO messages(candidate_id,role,content,created_at) VALUES(?,?,?,?)",
                     (cid, "bot", text, _now()))


def sse_chat(cid: int, question: str):
    """SSE 生成器：按 AGENT_MODE 分发；Agent 异常 → fallback 事件 + fixed 管线兜底"""
    # 限流在记消息之前判定：第 31 次请求进来时窗口内已有 30 条 → 判超限。
    over, used, limit = ratelimit.record(cid)
    _record_user_message(cid, question)
    mode = config.AGENT_MODE

    if over:
        # 超限不是错误：仍然给答案（固定管线），只是额外提示一句。
        yield {"event": "notice", "data": {"level": "warn", "text": ratelimit.notice_text(used, limit)}}
        yield {"event": "intent", "data": intent_mod.classify(question, cid)}
        fixed_reply, tid, _ = fixed_chat_reply(cid, question)
        _bot_reply(cid, fixed_reply)
        yield {"event": "fallback", "data": {"text": fixed_reply, "ticket_id": tid,
                                             "rate_limited": True}}
        yield {"event": "done", "data": {"mode": "rate_limited"}}
        return

    if mode == "on" and config.llm_ready():
        # on 模式：意图由 graph 的 intent 节点识别并外发，此处不重复外发
        yield from _stream_agent(cid, question, mode)
    elif mode == "shadow" and config.llm_ready() and random.random() < config.AGENT_SHADOW_RATE:
        # shadow：用户仍拿 fixed 管线回答；Agent 只读旁路跑，轨迹落库供对比
        yield {"event": "intent", "data": intent_mod.classify(question, cid)}
        yield {"event": "stage", "data": {"name": "shadow", "label": "🧪 Agent shadow 旁路运行中"}}
        fixed_reply, tid, _ = fixed_chat_reply(cid, question, allow_ticket=False)
        _bot_reply(cid, fixed_reply)
        yield {"event": "fallback", "data": {"text": fixed_reply, "ticket_id": tid}}
        threading.Thread(target=_shadow_run, args=(cid, question), daemon=True).start()
        yield {"event": "done", "data": {"mode": "shadow"}}
    else:
        fixed_reply, tid, cites = fixed_chat_reply(cid, question)
        _bot_reply(cid, fixed_reply)
        yield {"event": "intent", "data": intent_mod.classify(question, cid)}
        yield {"event": "fallback", "data": {"text": fixed_reply, "ticket_id": tid}}
        yield {"event": "done", "data": {"mode": "fixed"}}


def _stream_agent(cid: int, question: str, mode: str):
    """on 模式：子线程跑 graph，事件经队列推给 SSE；异常降级 fallback"""
    q: queue.Queue = queue.Queue()
    graph._sink.set(q)
    box = {}

    def run():
        t0 = time.time()
        try:
            # 每个问题独立 thread：检查点只服务本次运行的续跑/HITL 审批；
            # 跨问题的历史由 memory 模块负责，避免检查点无限累积与残缺状态复用
            tid = f"c{cid}-{time.time_ns()}"
            box["result"] = graph.run_agent(cid, question, thread_id=tid)
            box["thread_id"] = tid
            box["latency"] = int((time.time() - t0) * 1000)
        except Exception as e:                       # Agent 链路任何异常都降级
            box["error"] = repr(e)
        finally:
            q.put({"type": "__end__"})

    # 关键坑：ContextVar 的值不会自动继承到 threading.Thread 的新线程，
    # 直接传 run 会让 graph 内部的 emit() 全部静默丢弃（Agent 过程面板空白）。
    # 用 copy_context().run(...) 显式把当前上下文（含已设置的 sink）带进子线程。
    ctx = contextvars.copy_context()
    th = threading.Thread(target=lambda: ctx.run(run), daemon=True)
    th.start()

    events, first = [], True
    while True:
        ev = q.get()
        if ev.get("type") == "__end__":
            break
        if first:
            yield {"event": "stage", "data": {"name": "start", "label": "🤖 Agent 已接管"}}
            first = False
        events.append(ev)
        yield {"event": ev["type"], "data": {k: v for k, v in ev.items() if k != "type"}}

    # 注意：不能调用 _sink.reset(token)——生成器可能在不同的执行上下文中被恢复，
    # reset 跨 Context 会抛 "Token was created in a different Context" 并中断流。
    # 每个 ASGI 请求都有独立的上下文副本，这里直接覆盖为 None 即可，不会泄漏到其他请求。
    graph._sink.set(None)

    if "error" in box:
        # 双格式兼容：先 fallback，再透传固定管线回答
        fixed_reply, tid, _ = fixed_chat_reply(cid, question)
        _bot_reply(cid, fixed_reply)
        yield {"event": "fallback", "data": {"text": fixed_reply, "ticket_id": tid,
                                             "reason": box["error"]}}
        yield {"event": "done", "data": {"mode": "fallback"}}
        return

    r = box["result"]
    answer = r.get("answer") or "抱歉，我这次没能生成回答，已为你转人工处理。"
    # 模拟分片输出（graph 内部为一次性生成；分片仅作流式呈现）
    for i in range(0, len(answer), 48):
        yield {"event": "delta", "data": {"text": answer[i:i + 48]}}
    if r.get("citations"):
        cite_items = [{"question": c["question"],
                       "score": c.get("hybrid", c.get("score", 0))}
                      for c in r["citations"][:3]]
        # 引用与终局核验是 graph 返回后由本层补发的，不补进轨迹会导致
        # 灰度看板与低分回流看不到引用来源（第5期修）。
        events.append({"type": "citation", "items": cite_items})
        yield {"event": "citation", "data": {"items": cite_items}}
    v = r.get("verify", {})
    # 图内 verify 事件是 critic 的初判，这里是 apply_judge 之后的终局结论，
    # 用 verify_final 区分，避免看板把初判当终判。
    events.append({"type": "verify_final",
                   "status": v.get("status"), "ratio": v.get("ratio"),
                   "redlines": v.get("redlines") or [],
                   "judge": (v.get("judge") or {}).get("verdict", "")})
    yield {"event": "verify", "data": v}
    _bot_reply(cid, answer)
    runtime.save_run(cid, question, mode, events, answer, "",
                     v.get("status", "none"), v.get("ratio", 0), box.get("latency", 0),
                     prompts.fingerprint())
    yield {"event": "done", "data": {"mode": mode, "thread_id": box.get("thread_id", "")}}


def _shadow_run(cid: int, question: str):
    """shadow 旁路：跑 agent 并落 agent_runs，不影响用户"""
    try:
        q: queue.Queue = queue.Queue()
        graph._sink.set(q)
        t0 = time.time()
        r = graph.run_agent(cid, question, thread_id=f"c{cid}-shadow-{time.time_ns()}")
        latency = int((time.time() - t0) * 1000)
        graph._sink.set(None)   # 同 _stream_agent：覆盖而非 reset，避免跨 Context 报错
        events = []
        while True:
            ev = q.get(timeout=1)
            if ev.get("type") == "__end__":
                break
            events.append(ev)
        fixed_reply, _, _ = fixed_chat_reply(cid, question, allow_ticket=False)
        runtime.save_run(cid, question, "shadow", events, r.get("answer", ""), fixed_reply,
                         r.get("verify", {}).get("status", "none"),
                         r.get("verify", {}).get("ratio", 0), latency,
                         prompts.fingerprint())
    except Exception:
        pass
