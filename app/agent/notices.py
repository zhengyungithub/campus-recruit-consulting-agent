"""S5.3 事件触发主动推送：把「等用户来问」补成「有事主动说」。

三条硬约束，直接决定了实现方式：

1. **文案 100% 模板生成，不调模型。** 通知是让候选人照做的事实性内容，
   让模型润色确实更好听，但它会顺手编出具体的时间、地点、链接——这是红线。
   排期里这条标了"需模型"，实际做下来认为不该用：确定性优先于文采，
   而且模板能被断言、能进回归，模型不能。
2. **通知写独立表 `notices`，不混进 `messages`。** `messages` 是对话历史，
   会被 Agent 的指代还原、评测的历史播种读取；塞进系统通知等于污染模型输入。
3. **幂等靠数据库唯一索引，不靠内存状态。** 后台线程与接口即时触发是两条并发路径，
   内存去重重启即失效。用 `UNIQUE(candidate_id, kind, ref_type, ref_id)` + `INSERT OR IGNORE`，
   重复插入静默跳过，天然可重入。

两类事件：
- `status_change`：状态变更（`status_history` 水位线推进）
- `stale_todo`  ：待办态停留超阈值。**数据里没有 deadline 字段**，
  所以按「停滞时长」判定而非「临近截止」——宁可叫它停滞提醒，也不编一个不存在的截止时间。

首次扫描会做 bootstrap：直接把水位线推到当前最大 id 并跳过，
否则存量 `status_history` 会被当成新事件，一次性给所有人补发历史通知。
"""
import sqlite3
import threading
from datetime import datetime

from . import pii
from .config import DB_PATH

KIND_STATUS = "status_change"
KIND_STALE = "stale_todo"
KIND_TICKET = "ticket_reply"

# 需要候选人动手的状态 -> 停滞提醒里点明"卡在哪一步"。
# 其余状态（如"资料审核中""体检资料审核通过"）是公司侧动作，催候选人没有意义。
TODO_STATUSES = {
    "测评中": "在线测评尚未完成",
    "待传面试资料": "面试资料尚未上传",
    "待传体检资料": "体检资料尚未上传",
    "网签中": "网签尚未应约",
}

# 内部默认备注：写在 status_history 里给 HR 看，回显给候选人没有意义
_INTERNAL_NOTES = {"HR 手动更新", "名单导入", ""}

_DEFAULTS = {
    "notice_enabled": "1",
    "notice_scan_interval": "60",      # 后台扫描周期（秒）
    "notice_stale_days": "3",          # 待办态停留多少天开始提醒
    "notice_stale_cooldown": "3",      # 之后每隔多少天再提醒一次
}

# 由 main 在启动时注入，避免 notices -> main 的循环导入
_NEXT_STEP = {}


def bind(next_step: dict):
    """注入状态 -> 下一步指引的文案表（main.NEXT_STEP）。"""
    _NEXT_STEP.clear()
    _NEXT_STEP.update(next_step or {})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def conn_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(conn: sqlite3.Connection = None):
    own = conn is None
    conn = conn or conn_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS notices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            kind TEXT NOT NULL,                -- status_change / stale_todo
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            ref_type TEXT NOT NULL,            -- status_history / status_stale
            ref_id TEXT NOT NULL,              -- 关联记录标识，参与幂等
            created_at TEXT NOT NULL,
            read_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_notices_dedup
            ON notices(candidate_id, kind, ref_type, ref_id);
        CREATE INDEX IF NOT EXISTS idx_notices_cand ON notices(candidate_id, id DESC);
        """
    )
    if own:
        conn.commit()
        conn.close()


def _get_kv(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row["value"] not in (None, ""):
        return row["value"]
    return default


def _set_kv(conn, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES(?,?,?)",
                 (key, value, _now()))


def get_config(conn: sqlite3.Connection = None) -> dict:
    own = conn is None
    conn = conn or conn_db()
    out = {}
    for k, d in _DEFAULTS.items():
        raw = _get_kv(conn, k, d)
        try:
            out[k] = int(raw)
        except (TypeError, ValueError):
            out[k] = int(d)
    if own:
        conn.close()
    return out


def set_config(conn: sqlite3.Connection, **kv):
    for k, v in kv.items():
        if k in _DEFAULTS:
            _set_kv(conn, k, str(int(v)))


# ---------------------------------------------------------------- 文案（模板，确定性）
def _join(parts) -> str:
    """逐句补句号：NEXT_STEP 里的文案自带句号，直接拼接会出现「。。」。"""
    out = ""
    for p in parts:
        if not p:
            continue
        p = p.strip()
        out += p if p.endswith(("。", "！", "？", "；")) else p + "。"
    return out


def _status_body(status: str, note: str) -> str:
    return _join([
        f"你的校招进度已更新为「{status}」",
        f"备注：{note}" if (note and note not in _INTERNAL_NOTES) else "",
        _NEXT_STEP.get(status, ""),
        "如有疑问可在对话框里直接问我",
    ])


def _stale_body(status: str, days: int, hint: str) -> str:
    return _join([
        f"你在「{status}」已停留 {days} 天（{hint}）",
        _NEXT_STEP.get(status, ""),
        "如已办理完成可忽略本条提醒",
    ])


# ---------------------------------------------------------------- 扫描器
_scan_lock = threading.Lock()


def scan(conn: sqlite3.Connection = None) -> dict:
    """扫一遍事件源，生成通知。可重入：并发调用由锁串行，重复事件由唯一索引吸收。

    返回 {"bootstrap": bool, "status_change": n, "stale_todo": n, "cursor": int}
    """
    own = conn is None
    conn = conn or conn_db()
    res = {"bootstrap": False, "status_change": 0, "stale_todo": 0, "cursor": 0}
    with _scan_lock:
        try:
            ensure_tables(conn)
            cfg = get_config(conn)
            if not cfg["notice_enabled"]:
                res["cursor"] = int(_get_kv(conn, "notice_status_cursor", "0") or 0)
                return res

            # --- bootstrap：首次运行只落水位线，不补发历史 ---
            raw_cursor = _get_kv(conn, "notice_status_cursor", "")
            if raw_cursor == "":
                mx = conn.execute("SELECT COALESCE(MAX(id),0) m FROM status_history").fetchone()["m"]
                _set_kv(conn, "notice_status_cursor", str(mx))
                conn.commit()
                res["bootstrap"] = True
                res["cursor"] = mx
            else:
                res["status_change"] = _scan_status(conn)
                res["cursor"] = int(_get_kv(conn, "notice_status_cursor", "0") or 0)

            res["stale_todo"] = _scan_stale(conn, cfg)
            conn.commit()
        except Exception as e:
            conn.rollback()
            res["error"] = f"{type(e).__name__}: {e}"
        finally:
            if own:
                conn.close()
    return res


def _scan_status(conn) -> int:
    """状态变更：推进 status_history 水位线；前后状态相同视为无实质变更，不打扰。"""
    cursor = int(_get_kv(conn, "notice_status_cursor", "0") or 0)
    rows = conn.execute(
        "SELECT h.id, h.candidate_id, h.status, h.note, h.created_at,"
        " (SELECT p.status FROM status_history p"
        "  WHERE p.candidate_id=h.candidate_id AND p.id<h.id ORDER BY p.id DESC LIMIT 1) AS prev"
        " FROM status_history h WHERE h.id>? ORDER BY h.id", (cursor,)).fetchall()
    made = 0
    max_id = cursor
    for r in rows:
        max_id = max(max_id, r["id"])
        if r["prev"] == r["status"]:
            continue                      # HR 重复保存了同一状态，不推
        if not conn.execute("SELECT 1 FROM candidates WHERE id=?", (r["candidate_id"],)).fetchone():
            continue                      # 候选人已被删除
        cur = conn.execute(
            "INSERT OR IGNORE INTO notices(candidate_id,kind,title,body,ref_type,ref_id,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (r["candidate_id"], KIND_STATUS, f"进度更新：{r['status']}",
             _status_body(r["status"], r["note"] or ""),
             "status_history", str(r["id"]), _now()))
        made += cur.rowcount
    _set_kv(conn, "notice_status_cursor", str(max_id))
    return made


def _scan_stale(conn, cfg: dict) -> int:
    """待办态停滞：按「进入该状态的第几天」分桶，桶号写进 ref_id 做幂等 + 冷却。

    第 stale_days 天首提醒，之后每 cooldown 天一次；同一天内重复扫描不会重复推。
    """
    stale_days = max(1, cfg["notice_stale_days"])
    cooldown = max(1, cfg["notice_stale_cooldown"])
    # 每个候选人取当前所处状态与进入时间（最近一条历史）
    rows = conn.execute(
        "SELECT c.id cid, c.status status, h.created_at since"
        " FROM candidates c"
        " JOIN status_history h ON h.id=(SELECT id FROM status_history"
        "   WHERE candidate_id=c.id ORDER BY id DESC LIMIT 1)"
        " WHERE c.status IN ({})".format(",".join("?" * len(TODO_STATUSES))),
        tuple(TODO_STATUSES)).fetchall()
    today = datetime.now().date()
    made = 0
    for r in rows:
        try:
            since = datetime.fromisoformat(r["since"]).date()
        except (TypeError, ValueError):
            continue
        days = (today - since).days
        if days < stale_days:
            continue
        bucket = (days - stale_days) // cooldown
        status = r["status"]
        cur = conn.execute(
            "INSERT OR IGNORE INTO notices(candidate_id,kind,title,body,ref_type,ref_id,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (r["cid"], KIND_STALE, f"待办提醒：{status}（已 {days} 天）",
             _stale_body(status, days, TODO_STATUSES[status]),
             "status_stale", f"{status}@{since.isoformat()}#{bucket}", _now()))
        made += cur.rowcount
    return made


def notify_ticket_reply(candidate_id: int, ticket_id: int, answer: str) -> int:
    """工单被 HR 回复 → 推一条站内提醒（S6.1）。

    两条取舍：
    - **只放摘要，不放全文**。HR 回复里可能带手机号、姓名等（"已帮你联系张经理 138..."），
      通知栏是候选人一眼扫过的地方，不适合承载完整内容；而且通知表与对话表不同，
      没有经过红线扫描。摘要取前 40 字并做一次 PII 掩码，全文仍走【我的反馈】。
    - **幂等按工单号**，不是按回复次数。HR 补充回复同一张单不重复打扰——
      候选人看到一次就够了，点进去能看到最新内容。
    """
    summary = " ".join((answer or "").split())[:40]
    # mask_pii 的契约是返回 (脱敏文本, 命中类型列表)，只要前半段——
    # 直接把元组拼进文案会输出成 "('已帮你改到…', [])"，第一版就是这么踩的。
    summary = pii.mask_pii(summary)[0]
    body = (f"HR 已回复你的工单 #{ticket_id}"
            + (f"：{summary}…" if summary else "。")
            + "完整回复可在【我的反馈】查看。")
    conn = conn_db()
    ensure_tables(conn)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notices(candidate_id,kind,title,body,ref_type,ref_id,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (candidate_id, KIND_TICKET, f"人工回复 · 工单#{ticket_id}", body,
             "ticket", str(ticket_id), _now()))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------- 读取与已读
def list_for(candidate_id: int, limit: int = 50, unread_only: bool = False) -> list:
    conn = conn_db()
    ensure_tables(conn)
    sql = ("SELECT id, kind, title, body, created_at, read_at FROM notices"
           " WHERE candidate_id=?")
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id DESC LIMIT ?"
    rows = conn.execute(sql, (candidate_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def unread_count(candidate_id: int) -> int:
    conn = conn_db()
    ensure_tables(conn)
    n = conn.execute("SELECT COUNT(*) c FROM notices WHERE candidate_id=? AND read_at IS NULL",
                     (candidate_id,)).fetchone()["c"]
    conn.close()
    return n


def mark_read(candidate_id: int, ids: list = None, all_: bool = False) -> int:
    conn = conn_db()
    ensure_tables(conn)
    if all_:
        cur = conn.execute("UPDATE notices SET read_at=? WHERE candidate_id=? AND read_at IS NULL",
                           (_now(), candidate_id))
    elif ids:
        qs = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE notices SET read_at=? WHERE candidate_id=? AND read_at IS NULL"
            f" AND id IN ({qs})", [_now(), candidate_id] + [int(i) for i in ids])
    else:
        cur = None
    n = cur.rowcount if cur else 0
    conn.commit()
    conn.close()
    return n


def admin_list(limit: int = 100, kind: str = "") -> list:
    """推送记录（给 HR 看"推了什么、推给谁、看了没"）。"""
    conn = conn_db()
    ensure_tables(conn)
    sql = ("SELECT n.id, n.candidate_id, c.name, c.phone, n.kind, n.title, n.body,"
           " n.created_at, n.read_at FROM notices n"
           " LEFT JOIN candidates c ON c.id=n.candidate_id")
    args = []
    if kind:
        sql += " WHERE n.kind=?"
        args.append(kind)
    sql += " ORDER BY n.id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats() -> dict:
    conn = conn_db()
    ensure_tables(conn)
    row = conn.execute(
        "SELECT COUNT(*) total, SUM(read_at IS NOT NULL) read_n,"
        " SUM(kind=?) sc, SUM(kind=?) st FROM notices", (KIND_STATUS, KIND_STALE)).fetchone()
    sent_users = conn.execute("SELECT COUNT(DISTINCT candidate_id) c FROM notices").fetchone()["c"]
    conn.close()
    return {"total": row["total"] or 0, "read": row["read_n"] or 0,
            "status_change": row["sc"] or 0, "stale_todo": row["st"] or 0,
            "sent_users": sent_users or 0}


# ---------------------------------------------------------------- 后台扫描线程
_worker_thread = None
_worker_stop = threading.Event()


def start_worker(interval_s: int = None):
    """启动后台扫描线程（daemon，随主进程退出）。重复调用只会起一次。"""
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return _worker_thread
    cfg = get_config()
    interval = interval_s or max(10, cfg["notice_scan_interval"])
    _worker_stop.clear()

    def loop():
        # 先扫再等：启动时立刻跑一轮，顺便完成水位线 bootstrap
        # （否则存量 status_history 要等到第一个周期结束才被跳过，期间有补发风险）
        while not _worker_stop.is_set():
            try:
                r = scan()
                if r.get("status_change") or r.get("stale_todo"):
                    print(f"[notice] 扫描新增 状态变更 {r['status_change']} 条"
                          f" / 待办停滞 {r['stale_todo']} 条", flush=True)
            except Exception as e:
                print(f"[notice] 扫描异常（已跳过本轮）: {e!r}", flush=True)
            if _worker_stop.wait(interval):
                break

    _worker_thread = threading.Thread(target=loop, name="notice-scanner", daemon=True)
    _worker_thread.start()
    print(f"[notice] 后台扫描线程已启动，周期 {interval}s")
    return _worker_thread


def stop_worker():
    _worker_stop.set()
