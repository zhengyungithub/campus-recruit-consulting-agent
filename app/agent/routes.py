"""Agent 路由：SSE 流式问答、灰度轨迹查询"""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import config, graph, retrieval, runtime
from .service import sse_chat

router = APIRouter()


def _candidate_auth(request: Request) -> int:
    from main import SESSIONS
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    sess = SESSIONS.get(token)
    if not sess or sess["type"] != "candidate":
        raise HTTPException(401, "未登录或登录已过期")
    return sess["id"]


def _admin_auth(request: Request):
    from main import SESSIONS
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    sess = SESSIONS.get(token)
    if not sess or sess["type"] != "admin":
        raise HTTPException(401, "未登录或登录已过期")
    return sess


@router.get("/api/agent/status")
def agent_status():
    """前端探测 Agent 能力，决定是否走流式链路"""
    return {"mode": config.AGENT_MODE, "llm_ready": config.llm_ready(),
            "retrieval": retrieval.status()}


@router.post("/api/chat/agent/stream")
async def chat_agent_stream(request: Request):
    cid = _candidate_auth(request)
    body = await request.json()
    question = (body.get("message") or "").strip()
    if not question:
        raise HTTPException(400, "消息不能为空")

    def gen():
        for ev in sse_chat(cid, question):
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/admin/agent_runs")
def agent_runs(request: Request, limit: int = 50):
    sess = _admin_auth(request)
    if sess.get("role") not in (None, "super"):   # Agent 灰度属模型业务，仅超管可见
        raise HTTPException(403, "仅超级管理员可查看 Agent 灰度")
    return {"runs": runtime.get_runs(limit),
            "mode": config.AGENT_MODE,
            "retrieval": retrieval.status()}


@router.get("/api/admin/agent_runs/low")
def low_runs(request: Request, limit: int = 30, max_ratio: float = 0.85):
    """第5期：线上低分样本（核验未过 / 裁判存疑 / 命中红线 / 降级兜底）。

    供管理端浏览并一键固化成评测用例，形成"线上翻车 → 回归覆盖"的闭环。
    """
    sess = _admin_auth(request)
    if sess.get("role") not in (None, "super"):
        raise HTTPException(403, "仅超级管理员可查看 Agent 灰度")
    return {"runs": runtime.get_low_runs(limit, max_ratio)}


@router.get("/api/admin/maintenance/checkpoints")
def ckpt_stats(request: Request):
    """S2.3：检查点文件体积 / 线程数 / 当前保留天数（超管可见）。"""
    sess = _admin_auth(request)
    if sess.get("role") not in (None, "super"):
        raise HTTPException(403, "仅超级管理员可查看运维信息")
    return runtime.ckpt_stats()


@router.post("/api/admin/maintenance/checkpoints/prune")
async def ckpt_prune(request: Request):
    """S2.3：手动触发检查点清理（整条线程一起删，保留最近 N 天）。

    删之前必须先清理掉检查点占用的连接——图是懒加载的，
    若已被请求实例化过，其 sqlite 连接仍在，VACUUM 会失败（但不影响删除）。
    """
    sess = _admin_auth(request)
    if sess.get("role") not in (None, "super"):
        raise HTTPException(403, "仅超级管理员可执行运维操作")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    days = body.get("days")
    try:
        days = int(days) if days is not None else None
    except Exception:
        raise HTTPException(400, "days 必须是整数")
    return runtime.prune_checkpoints(days)


@router.post("/api/admin/evals/promote")
async def promote_case(request: Request):
    """第5期：把一条低分运行固化成评测用例，写进 cases.jsonl。

    标注取自该次运行的实际结果（图内意图 / 工具序列 / 是否建单 / 是否带引用），
    不依赖模型再判一遍——这样用例描述的是"当时认定的正确口径"，
    后续若口径调整，由人改标注而不是让模型自证。
    """
    _admin_auth(request)
    body = await request.json()
    run_id = body.get("run_id")
    if not run_id:
        raise HTTPException(400, "缺少 run_id")
    from .eval import promote_run_to_case
    try:
        return promote_run_to_case(int(run_id), body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"回流失败：{e}")
