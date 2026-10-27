"""业务规则：候选人状态流转合法性 + 操作审计（S6.1）。

为什么单独开一个模块
--------------------
`main.STATUSES` 只是一个"合法值清单"——它能挡住拼错的状态名，但挡不住
`已归档 → 测评中` 这种语义上荒谬的变更。状态是这个系统里**最贵的数据**：
它决定候选人收到什么提醒、Agent 回答时用什么口吻、HR 看板怎么统计。
改错一次，推送、回答、统计三处同时错，而且事后查不到是谁改的。

所以这里补两件事：**流转能不能改** + **谁改的**。

流转口径（第一版默认，业务确认后可调整）
----------------------------------------
1. **前进**：允许跳步。批量导入、线下补录、破格录用都会跨步，
   硬拦会让 HR 绕开系统。跨 ≥2 步时**要求填原因**，审计标 `jump`，事后可筛。
2. **倒退**：只放行白名单里的真实业务回退（资料打回、网签重填、重新约面），
   **且必须填原因**——回退一定是"出了什么问题"，没原因的回退就是误操作。
   白名单外的倒退直接拒绝。
3. **终态**：`已归档` 是终态，任何变更都拒绝。归档后再改等于流程诈尸，
   候选人会收到一条与事实不符的提醒。
4. **同状态**：放行但不写库（调用方跳过），不产生重复通知。

刻意不做的事：**不禁止跳步**。因为真实业务里跳步合法，禁止只会逼 HR
先改成中间态再改成目标态，反而污染 `status_history`。
"""
from datetime import datetime

from .store import get_conn

# 主链路顺序：序号即流程位置，倒退/跳步都靠它判定。
# 与 main.STATUSES 保持一致；flow 不反向依赖 main（避免循环导入）。
ORDER = [
    "已初筛通过", "测评中", "已约初面", "初面通过", "已约复试", "复试通过",
    "待传面试资料", "资料审核中", "意向沟通完成", "待传体检资料",
    "体检资料审核通过", "已发offer", "网签中", "已应约", "三方已回收", "已归档",
]
IDX = {s: i for i, s in enumerate(ORDER)}

# 终态：进来就出不去
TERMINAL = {"已归档"}

# 合法倒退白名单：键是 (当前, 目标)，值是这类回退的业务含义（写进审计与提示）
BACK_WHITELIST = {
    ("资料审核中", "待传面试资料"): "面试资料审核不通过，需重新上传",
    ("体检资料审核通过", "待传体检资料"): "体检资料审核不通过，需重新上传",
    ("已应约", "网签中"): "网签信息需重填",
    ("三方已回收", "已应约"): "三方协议信息有误，需重新应约",
    ("初面通过", "已约初面"): "初面安排变更，重新约面",
    ("复试通过", "已约复试"): "复试安排变更，重新约面",
}

# 跨几步算"跳步"（≥ 此值要求填原因）
JUMP_THRESHOLD = 2


def now():
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 流转判定
def can_transit(cur: str, nxt: str, reason: str = "") -> dict:
    """判定一次状态变更是否合法。

    返回 {allowed, code, message, need_reason}
      code: same / forward / jump / back / denied
    """
    if nxt not in IDX:
        return {"allowed": False, "code": "denied", "message": f"未知状态「{nxt}」",
                "need_reason": False}
    if cur not in IDX:
        # 存量脏数据（状态被手工改过）：放行，但记一条审计，别让 HR 卡住
        return {"allowed": True, "code": "forward", "message": f"原状态「{cur}」不在主链路，按前进处理",
                "need_reason": True}

    if cur == nxt:
        return {"allowed": True, "code": "same", "message": "状态未变化", "need_reason": False}

    if cur in TERMINAL:
        return {"allowed": False, "code": "denied",
                "message": f"「{cur}」是终态，不能改为「{nxt}」；如需更正请联系超管",
                "need_reason": False}

    a, b = IDX[cur], IDX[nxt]
    if b > a:
        step = b - a
        if step >= JUMP_THRESHOLD:
            # 跳步合法但必须留痕：破格、线下补录都算，没原因的就是误操作。
            # 口径与回退一致——需要原因却没给，一律拒绝，不能"先放行再说"。
            if not (reason or "").strip():
                return {"allowed": False, "code": "denied", "need_reason": True,
                        "message": f"跨越 {step} 个环节（{cur} → {nxt}），需填写原因"}
            return {"allowed": True, "code": "jump", "need_reason": True,
                    "message": f"跨越 {step} 个环节（{cur} → {nxt}）：{reason.strip()}"}
        return {"allowed": True, "code": "forward", "message": "", "need_reason": False}

    # 倒退
    why = BACK_WHITELIST.get((cur, nxt))
    if not why:
        return {"allowed": False, "code": "denied", "need_reason": False,
                "message": f"不允许从「{cur}」回退到「{nxt}」；"
                           f"如需回退请走允许的打回路径（资料/体检打回、网签重填、重新约面）"}
    if not (reason or "").strip():
        return {"allowed": False, "code": "denied", "need_reason": True,
                "message": f"回退到「{nxt}」（{why}）必须填写原因"}
    return {"allowed": True, "code": "back", "message": why, "need_reason": True}


def describe(cur: str, nxt: str) -> str:
    """给审计 detail 用的一句话摘要。"""
    a, b = IDX.get(cur), IDX.get(nxt)
    if a is None or b is None:
        return f"{cur} → {nxt}"
    if b > a:
        return f"前进 {b - a} 步：{cur} → {nxt}"
    return f"回退 {a - b} 步：{cur} → {nxt}"


# ------------------------------------------------------------------ 操作审计
def ensure_tables():
    """建表与加列（幂等）。由 main.init 调用；独立脚本调用也安全。"""
    with get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,          -- 操作人：admin 用户名 / system
            action TEXT NOT NULL,         -- status_change / ticket_reply / faq_* / kb_import ...
            target TEXT NOT NULL,         -- candidate#3 / ticket#12 / faq#45
            detail TEXT,                  -- 变更前后、原因（纯文本，给人看）
            result TEXT NOT NULL,         -- ok / denied
            created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target)")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(status_history)")}
        if "operator" not in cols:
            conn.execute("ALTER TABLE status_history ADD COLUMN operator TEXT DEFAULT ''")
        if "reason" not in cols:
            conn.execute("ALTER TABLE status_history ADD COLUMN reason TEXT DEFAULT ''")
        conn.commit()


def log(actor: str, action: str, target: str, detail: str = "", result: str = "ok"):
    """写一条审计。审计失败绝不能影响主流程——吞掉异常只打印。"""
    try:
        with get_conn() as conn:
            conn.execute("INSERT INTO audit_log(actor,action,target,detail,result,created_at)"
                         " VALUES(?,?,?,?,?,?)",
                         (actor or "system", action, target, detail or "", result, now()))
            conn.commit()
    except Exception as e:
        print(f"[audit] 写入失败（忽略）: {e!r}", flush=True)


def recent(limit: int = 50, actor: str = "", action: str = ""):
    """管理端查看：最近的操作记录。"""
    sql = "SELECT * FROM audit_log WHERE 1=1"
    args = []
    if actor:
        sql += " AND actor=?"
        args.append(actor)
    if action:
        sql += " AND action=?"
        args.append(action)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(max(limit, 1), 500))
    try:
        with get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        return []
