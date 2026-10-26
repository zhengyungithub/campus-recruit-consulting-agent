"""用户级滑动窗口限流（S1.4）。

**为什么单开一张 `rate_events` 表，而不是直接 COUNT messages**

最直觉的做法是"统计近一小时 messages 表里 role='user' 的条数"，
但这里有个坑：`tools._create_ticket` 建单时会往 messages **再插一条 user 消息**（工单正文），
于是"一次提问 + 一次建单"被记成两次，正常用户会被提前限流。
所以计数与存储解耦——限流只关心"用户发起了多少次请求"。

**为什么用滑动窗口而不是固定整点**
整点窗口的毛病是"23:59 打满 30 条，00:00 立刻又能打 30 条"，
短时峰值翻倍。滑动窗口按每条记录的 created_at 统计，行为平稳。

**超限时为什么不报错**
用户体验上，限流不是错误——仍然给出固定管线的答案，只是额外提示一句。
这样既守住成本上限，又不会让用户拿不到回答。
"""
from datetime import datetime, timedelta

from . import config
from .store import get_conn

WINDOW_S = 3600                 # 统计窗口：1 小时
DEFAULT_LIMIT = 30              # 默认阈值（settings 表可覆盖）
KEEP_S = 24 * 3600              # 事件保留时长：够统计也够事后排查
SETTING_KEY = "rate_limit_per_hour"

_TABLE_READY = False


def ensure_table():
    """建表（幂等）。启动期由 main.init 建好；这里留着兜底，避免独立脚本首次调用报错。"""
    global _TABLE_READY
    if _TABLE_READY:
        return
    with get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS rate_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            created_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_events"
                     " ON rate_events(candidate_id, created_at)")
    _TABLE_READY = True


def _since(seconds: int) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def limit_per_hour() -> int:
    """当前生效阈值（超管后台改完立即生效，0 表示不限流）。"""
    try:
        raw = config.get_setting(SETTING_KEY)
    except Exception:
        return DEFAULT_LIMIT
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LIMIT
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return DEFAULT_LIMIT


def record(cid: int):
    """记一次请求，返回 `(是否超限, 窗口内条数, 阈值)`。

    顺序是**先统计后记账**：第 31 次请求进来时窗口内已有 30 条 → 判超限。
    """
    limit = limit_per_hour()
    if limit <= 0:
        return False, 0, limit
    ensure_table()
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM rate_events WHERE candidate_id=? AND created_at>=?",
            (cid, _since(WINDOW_S))).fetchone()["c"]
        over = n >= limit
        conn.execute("INSERT INTO rate_events(candidate_id,created_at) VALUES(?,?)",
                     (cid, datetime.now().isoformat(timespec="seconds")))
        # 只在放行时顺手清理：超限说明有人在高频刷，没必要每次都陪着扫一遍
        if not over:
            conn.execute("DELETE FROM rate_events WHERE created_at<?", (_since(KEEP_S),))
    return over, n + 1, limit


def notice_text(used: int, limit: int) -> str:
    return (f"⏳ 提问太频繁了（近 1 小时 {used}/{limit} 条），"
            f"本次已由知识库直接回答；如需人工协助请稍后再试或提交工单。")
