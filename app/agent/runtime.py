"""运行时：SqliteSaver 检查点（会话可断点续跑/HITL 恢复）+ SQLAlchemy agent_runs 轨迹表"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, Integer, Text, Float, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import CKPT_PATH, DB_PATH

_lock = threading.Lock()
_saver = None

Base = declarative_base()
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    candidate_id = Column(Integer)
    question = Column(Text)
    mode = Column(String(10))            # shadow / on
    trajectory = Column(Text)            # JSON：节点与工具调用轨迹
    answer = Column(Text)                # agent 回答
    fixed_answer = Column(Text)          # 同题 fixed 管线回答（灰度对比）
    verify_status = Column(String(20))
    verify_ratio = Column(Float)
    latency_ms = Column(Integer)
    created_at = Column(String(32))
    prompt_version = Column(String(64))     # 提示词版本指纹，如 sys2-plan1-judge3


def init_tables():
    Base.metadata.create_all(engine)
    # 老库补列（SQLAlchemy 的 create_all 不会给已存在的表加新列）
    with sqlite3.connect(DB_PATH) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(agent_runs)")}
        if "prompt_version" not in cols:
            c.execute("ALTER TABLE agent_runs ADD COLUMN prompt_version TEXT")


def save_run(cid, question, mode, trajectory, answer, fixed_answer,
             verify_status, verify_ratio, latency_ms, prompt_version=""):
    with SessionLocal() as s:
        s.add(AgentRun(candidate_id=cid, question=question, mode=mode,
                       trajectory=json.dumps(trajectory, ensure_ascii=False),
                       answer=answer, fixed_answer=fixed_answer,
                       verify_status=verify_status, verify_ratio=verify_ratio,
                       latency_ms=latency_ms,
                       prompt_version=prompt_version,
                       created_at=datetime.now().isoformat(timespec="seconds")))
        s.commit()


def get_runs(limit=50):
    with SessionLocal() as s:
        rows = s.query(AgentRun).order_by(AgentRun.id.desc()).limit(limit).all()
        return [{"id": r.id, "candidate_id": r.candidate_id, "question": r.question,
                 "mode": r.mode, "answer": r.answer, "fixed_answer": r.fixed_answer,
                 "verify_status": r.verify_status, "verify_ratio": r.verify_ratio,
                 "latency_ms": r.latency_ms, "created_at": r.created_at,
                 "prompt_version": r.prompt_version or ""} for r in rows]


def _trajectory_facts(trajectory_json: str) -> dict:
    """从轨迹里抽出一条运行的关键事实（意图/工具/引用/裁判）。

    轨迹是事件流，查询时再解析即可——不额外建列，避免和已有的 trajectory 字段双写不一致。
    """
    try:
        evs = json.loads(trajectory_json or "[]")
    except Exception:
        evs = []
    intent, tools, judge, cites, redlines = "", [], "", 0, []
    for e in evs if isinstance(evs, list) else []:
        t = e.get("type")
        if t == "intent":
            intent = e.get("intent") or intent
        elif t == "tool" and e.get("name"):
            tools.append(e["name"])
        elif t == "citation":
            cites = max(cites, len(e.get("items") or []))
        elif t == "judge":
            judge = (e.get("verdict") or "" if isinstance(e.get("verdict"), str)
                     else (e.get("verdict") or {}).get("verdict", "")) or judge
        elif t == "verify":
            redlines = (e.get("redlines") or []) or redlines
        elif t == "verify_final":
            # 终局结论优先于图内初判（见 service.sse_chat 注释）
            redlines = (e.get("redlines") or []) or redlines
            judge = e.get("judge") or judge
    return {"intent": intent, "tools": tools, "cites": cites,
            "judge": judge, "redlines": redlines}


def get_low_runs(limit=50, max_ratio=0.85):
    """线上低分样本：核验未通过 / 被裁判判存疑 / 命中红线 / 走了降级。

    用途是把线上真实翻车的问题固化成评测用例，避免"改完不知道有没有改坏"。
    按 candidate_id+question 去重，同一问题反复翻车只留最新一条。
    """
    with SessionLocal() as s:
        rows = s.query(AgentRun).order_by(AgentRun.id.desc()).limit(500).all()
        out, seen = [], set()
        for r in rows:
            if len(out) >= limit:
                break
            f = _trajectory_facts(r.trajectory)
            bad_judge = f["judge"] in ("unsupported", "off_topic")
            low = ((r.verify_ratio or 0) < max_ratio or bad_judge
                   or bool(f["redlines"]) or r.verify_status in ("fail", "block", "degrade"))
            if not low:
                continue
            key = (r.candidate_id, (r.question or "").strip())
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": r.id, "candidate_id": r.candidate_id,
                        "question": r.question, "mode": r.mode,
                        "answer": r.answer, "verify_status": r.verify_status,
                        "verify_ratio": r.verify_ratio, "latency_ms": r.latency_ms,
                        "created_at": r.created_at,
                        "prompt_version": r.prompt_version or "",
                        "intent": f["intent"], "tools": f["tools"],
                        "cites": f["cites"], "judge": f["judge"],
                        "redlines": f["redlines"]})
        return out


def get_checkpointer():
    global _saver
    if _saver is None:
        with _lock:
            if _saver is None:
                from langgraph.checkpoint.sqlite import SqliteSaver
                conn = sqlite3.connect(str(CKPT_PATH), check_same_thread=False)
                _saver = SqliteSaver(conn)
    return _saver


def ckpt_retention_days() -> int:
    """检查点保留天数；settings 表可调，0 = 不清理（默认 7 天）。"""
    from . import config
    try:
        return int(config.get_effective("CKPT_RETENTION_DAYS") or 7)
    except Exception:
        return 7


def prune_checkpoints(days: int = None) -> dict:
    """清理过期检查点：整条会话线程一起删，保留最近 N 天。

    **为什么要按"线程"删而不是按"检查点"删**：
    langgraph 的检查点是一条链，每个 checkpoint 通过 parent_checkpoint_id 串到上一个。
    只删链中间的某几个节点，会让剩下的检查点指向已不存在的父节点——
    将来若接回 HITL 恢复，读到的会是断链状态。所以判据是「整条线程最后一次活动的时间」。

    **时间从哪来**：checkpoints 表没有独立的 created_at 列，
    ts 在 checkpoint 这个 blob 里，而且 langgraph 用 msgpack 序列化（不是 JSON），
    必须走它自己的序列化器解，直接 json.loads 会抛 UnicodeDecodeError。

    **解码失败的条目一律保留**：拿不到时间的，不能当成"过期"处理。

    返回 {'deleted': 删除的检查点条数, 'threads': 删除的线程数,
          'size_before': 字节, 'size_after': 字节}
    """
    if days is None:
        days = ckpt_retention_days()
    if days <= 0:
        return {"deleted": 0, "threads": 0, "size_before": 0, "size_after": 0,
                "note": "保留天数<=0，已跳过清理"}
    if not CKPT_PATH.exists():
        return {"deleted": 0, "threads": 0, "size_before": 0, "size_after": 0,
                "note": "检查点文件不存在"}

    size_before = CKPT_PATH.stat().st_size
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        ser = JsonPlusSerializer()
    except Exception as e:                            # 拿不到序列化器就别冒险删
        return {"deleted": 0, "threads": 0, "size_before": size_before,
                "size_after": size_before, "note": f"序列化器不可用，已跳过: {type(e).__name__}"}

    latest = {}                                       # thread_id -> 该线程最后活动时间
    unreadable = set()                                # 有任意一条解不出时间的线程
    conn = sqlite3.connect(str(CKPT_PATH))
    try:
        for tid, t, blob in conn.execute("SELECT thread_id, type, checkpoint FROM checkpoints"):
            try:
                ts = (ser.loads_typed((t, blob)) or {}).get("ts") or ""
            except Exception:
                ts = ""
            if not ts:
                unreadable.add(tid)                   # 只要有一条解不出来，整条线程保留
                continue
            if ts > latest.get(tid, ""):
                latest[tid] = ts

        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        stale = []
        for tid, ts in latest.items():
            if tid in unreadable:
                continue
            try:
                if datetime.fromisoformat(ts) < cutoff_dt:
                    stale.append(tid)
            except Exception:
                continue

        deleted = 0
        for tid in stale:
            deleted += conn.execute(
                "DELETE FROM checkpoints WHERE thread_id=?", (tid,)).rowcount
            conn.execute("DELETE FROM writes WHERE thread_id=?", (tid,))
        conn.commit()
    finally:
        conn.close()

    # VACUUM 才能把删除的空间真正还给磁盘；必须在无其他事务时执行，故单独开连接
    try:
        v = sqlite3.connect(str(CKPT_PATH))
        v.isolation_level = None
        v.execute("VACUUM")
        v.close()
    except Exception:
        pass

    size_after = CKPT_PATH.stat().st_size if CKPT_PATH.exists() else 0
    return {"deleted": deleted, "threads": len(stale),
            "size_before": size_before, "size_after": size_after}


def ckpt_stats() -> dict:
    """检查点文件体积与线程数，供后台展示。"""
    if not CKPT_PATH.exists():
        return {"exists": False, "size": 0, "threads": 0, "retention_days": ckpt_retention_days()}
    try:
        conn = sqlite3.connect(str(CKPT_PATH))
        n = conn.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints").fetchone()[0]
        m = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        conn.close()
    except Exception:
        n, m = 0, 0
    return {"exists": True, "size": CKPT_PATH.stat().st_size,
            "threads": n, "checkpoints": m, "retention_days": ckpt_retention_days()}
