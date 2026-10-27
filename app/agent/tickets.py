"""工单业务规则（S6.1）：负责人、处理中态、SLA 逾期。

改动前的状态
------------
`tickets` 只有 `pending` / `resolved` 两态，没有负责人、没有优先级、没有时限。
看板能回答的只有"有几张待处理"，回答不了"哪张快超了""这张谁在跟"。
POC 阶段单量少看不出问题，单量上来后必然丢单——而且丢的是候选人真实的求助，
丢的时候还没有任何信号。

三条规则
--------
1. **三态**：`pending`（待处理）→ `handling`（处理中）→ `resolved`（已解决）。
   加 `handling` 不是为了好看，是为了区分"没人看"和"有人在看但没解决"——
   这两种情况的处理方式完全不同：前者要派单，后者要催。
2. **认领即负责**。`claim()` 把当前操作人写进 `assignee`，同时置 `handling`。
   没有"匿名处理"：谁点的谁负责，看板上能直接找到人。
3. **SLA 按时长算，不存截止时间**。数据里没有 deadline 字段（S5.3 已确认），
   与其编一个假的截止时间，不如如实按"挂了多久"算：
   `pending/handling` 超过阈值即逾期。加急单阈值更短（8 小时 vs 普通 24 小时）。

逾期是**算出来的**，不是存下来的——改阈值即刻生效，不用跑数据迁移。
"""
from datetime import datetime

from .store import get_conn

PENDING, HANDLING, RESOLVED = "pending", "handling", "resolved"
PRIORITY_NORMAL, PRIORITY_URGENT = "normal", "urgent"
PRIORITY_LABELS = {PRIORITY_NORMAL: "普通", PRIORITY_URGENT: "加急"}

SLA_KEY = "ticket_sla_hours"
DEFAULT_SLA_HOURS = 24
URGENT_SLA_HOURS = 8          # 加急单：8 小时未闭环即提醒


def now():
    return datetime.now().isoformat(timespec="seconds")


def ensure_columns():
    """加列（幂等）。"""
    with get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tickets)")}
        if "assignee" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN assignee TEXT DEFAULT ''")
        if "priority" not in cols:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT '{PRIORITY_NORMAL}'")
        if "handled_at" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN handled_at TEXT DEFAULT ''")
        conn.commit()


def sla_hours(priority: str = PRIORITY_NORMAL) -> int:
    """SLA 阈值（小时）。加急单更短。"""
    if priority == PRIORITY_URGENT:
        return URGENT_SLA_HOURS
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (SLA_KEY,)).fetchone()
        if row and str(row["value"]).isdigit():
            return max(1, int(row["value"]))
    except Exception:
        pass
    return DEFAULT_SLA_HOURS


def _hours_since(iso: str) -> float:
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return 0.0


def annotate(row: dict) -> dict:
    """给一条工单补上展示需要的派生字段：逾期时长、SLA 阈值、是否逾期。"""
    d = dict(row)
    pr = d.get("priority") or PRIORITY_NORMAL
    lim = sla_hours(pr)
    spent = _hours_since(d.get("created_at") or "")
    d["priority_label"] = PRIORITY_LABELS.get(pr, pr)
    d["sla_hours"] = lim
    d["spent_hours"] = round(spent, 1)
    d["overdue"] = d.get("status") != RESOLVED and spent > lim
    d["overdue_hours"] = round(spent - lim, 1) if d["overdue"] else 0
    return d


def claim(tid: int, actor: str, priority: str = "") -> dict:
    """认领：置处理中 + 记负责人。已解决的不能认领。"""
    with get_conn() as conn:
        row = conn.execute("SELECT id, status FROM tickets WHERE id=?", (tid,)).fetchone()
        if not row:
            return {"ok": False, "error": "工单不存在"}
        if row["status"] == RESOLVED:
            return {"ok": False, "error": "工单已解决，无需认领"}
        pr = priority if priority in PRIORITY_LABELS else None
        if pr:
            conn.execute("UPDATE tickets SET status=?, assignee=?, priority=?, handled_at=?"
                         " WHERE id=?", (HANDLING, actor, pr, now(), tid))
        else:
            conn.execute("UPDATE tickets SET status=?, assignee=?, handled_at=?"
                         " WHERE id=?", (HANDLING, actor, now(), tid))
        conn.commit()
    return {"ok": True, "id": tid, "assignee": actor, "status": HANDLING}


def release(tid: int) -> dict:
    """退回待处理（认领错了 / 需要换人）。"""
    with get_conn() as conn:
        conn.execute("UPDATE tickets SET status=?, assignee='', handled_at='' WHERE id=?",
                     (PENDING, tid))
        conn.commit()
    return {"ok": True, "id": tid, "status": PENDING}


def set_priority(tid: int, priority: str) -> dict:
    if priority not in PRIORITY_LABELS:
        return {"ok": False, "error": "未知优先级"}
    with get_conn() as conn:
        conn.execute("UPDATE tickets SET priority=? WHERE id=?", (priority, tid))
        conn.commit()
    return {"ok": True, "id": tid, "priority": priority}


def stats():
    """看板用：待处理 / 处理中 / 逾期 / 我的口径不需要的都不算。"""
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, status, priority, created_at FROM tickets").fetchall()]
    pending = handling = resolved = overdue = 0
    for r in rows:
        a = annotate(r)
        if a["status"] == RESOLVED:
            resolved += 1
        elif a["status"] == HANDLING:
            handling += 1
        else:
            pending += 1
        if a["overdue"]:
            overdue += 1
    return {"pending": pending, "handling": handling, "resolved": resolved,
            "overdue": overdue, "total": len(rows),
            "sla_hours": sla_hours(), "urgent_sla_hours": URGENT_SLA_HOURS}
