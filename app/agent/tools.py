"""工具层：LangChain @tool + Pydantic schema + _audited 审计包装（staff_only 边界 + 截断）

审批说明：候选人在对话里的写操作只有一个——建反馈工单，而工单本来就要 HR 在后台处理，
审批闭环已经落在工单流程里。因此 Agent 层不再做 interrupt 挂起，
若将来新增"改期 / 换站点"这类会直接改业务数据的工具，再一次性把审批接回来。
"""
import json

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from . import retrieval
from .store import get_conn

MAX_TOOL_RESULT = 1200      # 工具返回截断，防上下文爆炸
TOOLS_REGISTRY = {}


def _audited(name, staff_only=False):
    """审计装饰器：权限边界 + 结果截断 + 工具事件上报（emit 由 graph 注入）"""
    def deco(fn):
        def wrapper(config: RunnableConfig = None, **kwargs):
            cfg = config or {}
            cid = (cfg.get("configurable") or {}).get("candidate_id")
            emit = (cfg.get("configurable") or {}).get("emit")
            if emit:
                emit({"type": "tool", "name": name, "args": kwargs})
            result = fn(candidate_id=cid, **kwargs)
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            if emit:
                emit({"type": "tool_result", "name": name, "summary": text[:160]})
            return text[:MAX_TOOL_RESULT]
        wrapper._staff_only = staff_only
        wrapper._raw = fn
        return wrapper
    return deco


# ---------------------------------------------------------------- 工具实现
class KnowledgeSearchArgs(BaseModel):
    query: str = Field(description="检索问题，如：面试资料包括哪些")


@_audited("knowledge_search")
def _knowledge_search(candidate_id, query: str):
    hits = retrieval.search(query, k=4)
    if not hits:
        return json.dumps({"results": [], "note": "知识库未命中"}, ensure_ascii=False)
    # 注意：**不要把 source（hybrid/dense/keyword 融合模式）放进返回**。
    # 它是纯内部元数据，对作答毫无帮助，但模型会照抄进答案回显给用户
    # （实测出现「依据条目：xxx（来源：hybrid）」，被 internal_leak 红线判负）。
    # 内部字段一律不进模型视野——这是"不把内部实现暴露给用户"的第一道闸。
    return json.dumps({"results": [
        {"question": h["question"], "answer": h["answer"], "score": h["hybrid"]}
        for h in hits]}, ensure_ascii=False)


class ProgressArgs(BaseModel):
    pass


@_audited("get_my_progress")
def _get_my_progress(candidate_id):
    with get_conn() as conn:
        c = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not c:
            return "未找到候选人档案"
        hist = conn.execute(
            "SELECT status, note, created_at FROM status_history WHERE candidate_id=? ORDER BY id DESC LIMIT 3",
            (candidate_id,)).fetchall()
    return json.dumps({
        "name": c["name"], "status": c["status"], "site": c["site"], "position": c["position"],
        "recent_history": [dict(h) for h in hist],
    }, ensure_ascii=False)


class TicketArgs(BaseModel):
    question: str = Field(description="需要人工处理的完整问题")


@_audited("create_ticket")      # 写操作：建反馈工单，审批在 HR 后台工单流程完成
def _create_ticket(candidate_id, question: str):
    from datetime import datetime
    from . import pii
    # 工单正文保留原文（HR 要拿它处理业务，脱敏会导致无法办理）；
    # 同步写进 messages 的那一份做掩码（S1.3）——两份口径不同是刻意的。
    safe_q, _ = pii.mask_pii(question)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tickets(candidate_id,question,context,created_at) VALUES(?,?,?,?)",
            (candidate_id, question, "[]", datetime.now().isoformat(timespec="seconds")))
        tid = cur.lastrowid
        conn.execute("INSERT INTO messages(candidate_id,role,content,created_at) VALUES(?,?,?,?)",
                     (candidate_id, "user", safe_q, datetime.now().isoformat(timespec="seconds")))
    return json.dumps({"ticket_id": tid, "message": "已创建反馈工单，HR 将尽快处理"}, ensure_ascii=False)


class ListTicketArgs(BaseModel):
    pass


@_audited("list_my_tickets")
def _list_my_tickets(candidate_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, question, status, answer, created_at FROM tickets"
            " WHERE candidate_id=? ORDER BY id DESC LIMIT 5", (candidate_id,)).fetchall()
    return json.dumps({"tickets": [dict(r) for r in rows]}, ensure_ascii=False)


def build_tools() -> list:
    tools = [
        StructuredTool.from_function(
            func=_knowledge_search, name="knowledge_search",
            description="检索校招知识库（流程、材料、体检、offer、网签等）。回答任何流程/政策问题前必须先调用。",
            args_schema=KnowledgeSearchArgs),
        StructuredTool.from_function(
            func=_get_my_progress, name="get_my_progress",
            description="查询当前候选人自己的校招进度（当前环节、最近流转记录）。凡涉及'我到哪一步/我的状态'必须调用。",
            args_schema=ProgressArgs),
        StructuredTool.from_function(
            func=_create_ticket, name="create_ticket",
            description="当知识库无法回答或需要人工办理（改期/换岗/换站点等）时，为候选人创建反馈工单。",
            args_schema=TicketArgs),
        StructuredTool.from_function(
            func=_list_my_tickets, name="list_my_tickets",
            description="查询该候选人最近的反馈工单及处理状态。",
            args_schema=ListTicketArgs),
    ]
    for t in tools:
        TOOLS_REGISTRY[t.name] = t
    return tools


TOOLS = build_tools()
