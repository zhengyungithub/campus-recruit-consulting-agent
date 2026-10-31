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
# knowledge_search 的**旁路输出**：最近一次命中的结构化数据（含 score 等内部字段）。
# 只给框架（引用 / 看板）用，模型看不到——见 _knowledge_search 里的说明。
# 单线程同步调用（tool.invoke 后立刻取走），不存在并发错配问题。
_KB_LAST = {"hits": []}


def take_kb_hits() -> list:
    """取走最近一次知识库命中（结构化，含内部字段），取完即清。"""
    return _KB_LAST.pop("hits", []) or []


class KnowledgeSearchArgs(BaseModel):
    query: str = Field(description="检索问题，如：面试资料包括哪些")


@_audited("knowledge_search")
def _knowledge_search(candidate_id, query: str):
    """检索知识库，返回**自然语言**给模型；结构化命中走旁路给框架（见 take_kb_hits）。

    为什么不给 JSON：实测模型会把工具返回的 JSON 结构原样抄进答案——
        「依据条目：{"question": "可以更换投递岗位吗？", "score": 0.6868}」
    去掉 score 之后它照样抄 `{"question": ...}` 这个壳子。
    **删字段是打地鼠，根因是"给模型看了结构化数据"**：模型要复述依据，
    而 JSON 就在眼前，照抄是最省力的选择。

    于是拆成两条通道：
        模型看到 → 自然语言（只有问题与答案，无内部字段）
        框架拿到 → take_kb_hits() 旁路（含 score 等，用于引用与看板）
    这与 S4.4 补执行"回灌用自然语言而非裸 JSON"是同一条原则。

    受众：这里是**候选人**在问，所以 `include_internal=False`——
    标了"内部"的条目（薪资带宽、内部评级口径等）不进候选人的检索结果，
    否则模型会把它当普通知识原样答出去。HR 侧的知识库列表与查重不受影响。
    """
    hits = retrieval.search(query, k=4, include_internal=False)
    _KB_LAST["hits"] = hits or []
    if not hits:
        return "知识库未命中任何条目。请如实告知候选人：该问题知识库暂无答案，建议转人工处理。"
    lines = [f"知识库命中 {len(hits)} 条："]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. 问题：{h['question']}")
        lines.append(f"   答案：{h['answer']}")
    return "\n".join(lines)


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
