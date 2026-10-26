"""
校招咨询 Agent · MVP 后端（Phase 1）
- 候选人端：白名单登录（模拟短信码）、FAQ 问答（bigram 检索）、个人进度时间线、
  超范围问题自动生成反馈工单（供 HR 处理后沉淀为 FAQ）
- HR 后台：候选人管理/导入、状态更新、知识库维护、反馈工单处理、统计看板
"""
import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from agent import dedupe as dedupe_mod
from agent import pii, prompts, ratelimit, retrieval, runtime
from agent import config as agent_config
from agent.pipeline import fixed_chat_reply
from agent.routes import router as agent_router

BASE = Path(__file__).parent
DB_PATH = BASE / "campus_bot.db"
STATIC = BASE / "static"

# HR 后台管理员账号（生产环境用环境变量覆盖）
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PWD = os.getenv("ADMIN_PASSWORD", "<REDACTED>")

app = FastAPI(title="校招咨询 Agent MVP")
app.include_router(agent_router)


@app.get("/api/health")
def health():
    return {"ok": True, "time": now()}

# ---------------------------------------------------------------- 常量
STATUSES = [
    "已初筛通过", "测评中", "已约初面", "初面通过", "已约复试", "复试通过",
    "待传面试资料", "资料审核中", "意向沟通完成", "待传体检资料",
    "体检资料审核通过", "已发offer", "网签中", "已应约", "三方已回收", "已归档",
]

NEXT_STEP = {
    "已初筛通过": "等待测评发起，请留意通知短信。",
    "测评中": "请在截止时间前完成在线测评。",
    "已约初面": "请按通知的时间参加初面（线上为腾讯会议，线下为站点现场）。",
    "初面通过": "恭喜通过初面，请等待复试安排通知。",
    "已约复试": "请按通知的时间参加复试。",
    "复试通过": "恭喜通过复试，请留意短信并及时上传面试资料。",
    "待传面试资料": "请尽快上传面试资料（入口见通知短信），逾期会影响后续环节。",
    "资料审核中": "面试资料正在审核，通过后会通知下一环节。",
    "意向沟通完成": "请准备并上传体检资料。",
    "待传体检资料": "请尽快上传体检资料。",
    "体检资料审核通过": "两份资料均已审核通过，等待录用审批。",
    "已发offer": "请关注 offer 发放及网签通知。",
    "网签中": "请在网签系统中及时应约，并打印纸质三方协议。",
    "已应约": "已收到你的应约，等待公司盖章返邮。",
    "三方已回收": "等待维护录用信息，请提前准备入职资料。",
    "已归档": "你的校招流程已全部完成，感谢配合！",
}

GREET_RE = re.compile(r"^(你好|您好|hi|hello|嗨|哈喽|在吗)[!！。~\s]*$", re.I)
THANKS_RE = re.compile(r"^(谢谢|多谢|感谢|thanks|thank you)[!！。~\s]*$", re.I)

# ---------------------------------------------------------------- DB
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            school TEXT, major TEXT, position TEXT, site TEXT,
            status TEXT NOT NULL DEFAULT '已初筛通过',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS status_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            status TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS faqs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            category TEXT DEFAULT '通用',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            role TEXT NOT NULL,           -- user / bot / system
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            question TEXT NOT NULL,
            context TEXT,                  -- 命中前的对话上下文（JSON）
            status TEXT DEFAULT 'pending', -- pending / resolved
            answer TEXT,
            faq_added INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            type TEXT NOT NULL,            -- candidate / admin
            payload TEXT,                  -- JSON 扩展字段（如 candidate id）
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'hr',    -- super（超级管理员）/ hr（运营 HR）
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',  -- active / disabled
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,          -- agent_mode / llm_* / embed_*
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS eval_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT,                     -- intent / fixed / agent
            total INTEGER,
            metrics TEXT,                  -- JSON 汇总指标
            details TEXT,                  -- JSON 逐条明细
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS rate_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL, -- S1.4 用户级限流计数（不存正文，只记一次请求）
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rate_events ON rate_events(candidate_id, created_at);
        """
    )
    # 轻量迁移：候选人可选设置登录密码（手机号 + 密码登录）
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()]
    if "password_hash" not in cols:
        conn.execute("ALTER TABLE candidates ADD COLUMN password_hash TEXT")
    # 轻量迁移（S2.2）：FAQ 沉淀来源，便于按来源筛出并整体回滚
    fcols = [r["name"] for r in conn.execute("PRAGMA table_info(faqs)").fetchall()]
    if "source" not in fcols:
        conn.execute("ALTER TABLE faqs ADD COLUMN source TEXT DEFAULT 'manual'")
        conn.execute("UPDATE faqs SET source='manual' WHERE source IS NULL OR source=''")
    if conn.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"] == 0:
        seed(conn)
    conn.commit()
    conn.close()
    _ensure_super_account()


def _ensure_super_account():
    """
    幂等迁移：确保系统至少有一个超级管理员。
    首次运行时把历史上配置的管理员账号（settings 表或环境变量默认值）升级为 super 角色，
    之后所有 HR 登录都统一走 accounts 表。
    """
    conn = db()
    try:
        if conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]:
            return
        username = _get_kv("admin_username") or ADMIN_USER
        stored_h = _get_kv("admin_password_hash")
        pw_hash = stored_h or _hash_pwd(ADMIN_PWD)
        conn.execute(
            "INSERT INTO accounts(username,password_hash,role,display_name,status,created_at)"
            " VALUES(?,?,?,?,?,?)", (username, pw_hash, "super", "超级管理员", "active", now()))
        conn.commit()
    finally:
        conn.close()


def seed(conn):
    """初始化基线 FAQ（业务真实流程知识）；候选人名单由 HR 后台导入真实数据。"""
    now = datetime.now().isoformat(timespec="seconds")
    faqs = [
        ("校招的流程是什么？",
         "整体流程为：宣讲 → 简历初筛 → 在线测评 → 专业面试（初面）→ 复试 → 上传面试资料 → 审核 → 意向沟通会 → 上传体检资料 → 审核 → 录用审批 → offer 发放 → 网签 → 三方盖章返邮 → 录用资料归档。你可以在【我的进度】中查看自己当前所处的环节。"),
        ("初面是什么形式？",
         "分为两类站点：线上站点通过腾讯会议进行专业面试官初面；线下站点在酒店/站点现场面试。具体形式以你收到的面试通知短信为准。"),
        ("我错过了约定的面试时间怎么办？",
         "请尽快自行联系面试官重新约定时间，最迟不要超过原定面试时间的第二天。也可在对话框留言，由工作人员协助协调。"),
        ("复试在哪里进行？",
         "线上站点复试通过格慧招安排（基地人力面试）；线下站点在站点酒店现场进行。具体时间地点以通知为准。"),
        ("面试资料包括哪些？怎么上传？",
         "一般包括身份证、学生证、成绩单、获奖证书等扫描件。收到上传通知短信后，点击短信中的链接即可上传，支持 PDF 和图片格式。"),
        ("面试资料审核不通过怎么办？",
         "审核驳回后会通过短信告知驳回原因，请按原因修改后在原链接重新上传，系统会自动重新提交审核。"),
        ("体检资料有什么要求？",
         "需上传指定级别医院的体检报告。收到体检资料上传通知后，在链接中上传即可，审核通过后会进入录用审批环节。"),
        ("意向沟通会是什么？",
         "面试与资料审核通过后，领队会组织意向沟通会，说明岗位、地点、待遇等信息，并确认你的入职意向。请按通知时间参加。"),
        ("offer 什么时候发？",
         "面试资料与体检资料均审核通过后，领队制作录用表报领导审批，审批通过后制作 offer 发放表并发放 offer，请耐心等待通知。"),
        ("网签怎么操作？",
         "收到网签通知后，登录网签系统确认应约，并及时打印纸质三方协议寄回公司。公司盖章后会返邮给你，请保持电话畅通。"),
        ("纸质三方协议什么时候寄回？",
         "你在网签系统应约并打印纸质三方后寄给公司，公司盖章后返邮给你，寄送状态可在对话框咨询或留意通知。"),
        ("测评没做完/测评链接失效怎么办？",
         "请在对话框留言说明情况并留下手机号，工作人员会协助重新发送测评链接。"),
        ("可以更换投递岗位吗？",
         "岗位调整需要人工评估，请在对话框说明你的诉求，会有工作人员与你联系。"),
        ("校招结果在哪里查询？",
         "你可以在【我的进度】中实时查看自己当前所处的环节；涉及是否通过的结果，均以通知短信和进度页面为准。"),
    ]
    for q, a in faqs:
        conn.execute(
            "INSERT INTO faqs(question,answer,category,source,created_at) VALUES(?,?,?,?,?)",
            (q, a, "通用", "kb_seed", now))
    conn.execute(
        "INSERT INTO faqs(question,answer,category,source,created_at) VALUES(?,?,?,?,?)",
        ("HR 后台如何登录？", "使用管理员账号登录后台（账号密码由系统管理员配置）。", "内部", "kb_seed", now))
    conn.commit()


# ---------------------------------------------------------------- 检索（bigram + jieba 词覆盖）
try:
    import jieba
    _STOP = {"的", "了", "吗", "呢", "吧", "啊", "是", "有", "个", "我", "你", "他",
             "能", "可以", "怎么", "怎样", "什么", "请问", "想", "要", "在", "和", "与"}
    def _tokens(s: str):
        return {w for w in jieba.cut(s) if w.strip() and w not in _STOP and not re.fullmatch(r"[\W\d]", w)}
except ImportError:                      # 无 jieba 时退化为 bigram-only
    _tokens = None

def bigrams(s: str):
    s = re.sub(r"[\s?？!！。，,.的嘛呢吗吧]", "", s.lower())
    return [s[i:i + 2] for i in range(len(s) - 1)] or ([s] if s else [])


def similarity(a: str, b: str) -> float:
    A, B = set(bigrams(a)), set(bigrams(b))
    score = 0.0
    if A and B:
        score = 2 * len(A & B) / (len(A) + len(B))
    if a in b or b in a:                 # 子串直接加权
        score = max(score, 0.85)
    if _tokens:                          # 分词后按关键词覆盖率匹配（处理问法不同但意思相同）
        _GENERIC_CHAR = set("的了吗呢吧啊我你他个在和是有到想能可以不")
        def _contain(t, s):
            if t in s or s in t:
                sh = s if len(s) < len(t) else t
                return len(sh) > 1 or sh not in _GENERIC_CHAR
            return False
        ta, tb = _tokens(a), _tokens(b)
        if ta:
            matched, weight = 0, 0.0
            for t in ta:
                if t in tb:
                    matched += 1; weight += 1.0
                elif any(_contain(t, s) for s in tb):
                    matched += 1; weight += 0.5     # 词包含匹配（如"换个" vs "换"）
            if matched >= 2:                        # 至少两个词相关才计分，防单词误命中
                score = max(score, weight / len(ta))
    return score


# ---------------------------------------------------------------- 会话
SESSIONS = {}          # token -> {"type": "candidate"/"admin", ...}
SMS_CODES = {}         # phone -> code（演示环境；生产替换为短信网关）

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def auth(request: Request, typ: str):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    sess = SESSIONS.get(token)
    if not sess:
        # 内存未命中 → 从数据库恢复（服务重启后会话不丢失）
        conn = db()
        row = conn.execute("SELECT type, payload FROM sessions WHERE token=?",
                           (token,)).fetchone()
        conn.close()
        if row:
            import json as _json
            sess = {"type": row["type"]}
            if row["payload"]:
                try:
                    sess.update(_json.loads(row["payload"]))
                except Exception:
                    pass
            SESSIONS[token] = sess
    if not sess or sess["type"] != typ:
        raise HTTPException(401, "未登录或登录已过期")
    return sess


def persist_session(token: str, typ: str, payload: dict | None = None):
    """会话写入内存 + 数据库，重启不失效。"""
    import json as _json
    SESSIONS[token] = {"type": typ, **(payload or {})}
    conn = db()
    conn.execute("INSERT OR REPLACE INTO sessions(token, type, payload, created_at) VALUES(?,?,?,?)",
                 (token, typ, _json.dumps(payload or {}), now()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 账号密码
def auth_admin(request: Request):
    """运营端通用鉴权：返回会话（含 username / role）"""
    sess = auth(request, "admin")
    if not sess.get("role"):
        sess["role"] = "super"              # 迁移前老会话按超管兼容
    return sess


def auth_super(request: Request):
    """超管专属鉴权：模型配置、子账号管理等系统级能力"""
    sess = auth_admin(request)
    if sess.get("role") != "super":
        raise HTTPException(403, "仅超级管理员可操作：模型配置由超级管理员统一配置")
    return sess


def _norm_input(v: str) -> str:
    """登录输入规整：去首尾空格 + 全角字符转半角（兼容中文输入法误输，如 ａｄｍｉｎ１２３ / 密码尾部空格）"""
    import unicodedata
    return unicodedata.normalize("NFKC", (v or "").strip())


def _hash_pwd(pwd: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode(), 60000).hex()
    return f"{salt}${digest}"


def _check_pwd(pwd: str, stored: str) -> bool:
    try:
        salt = stored.split("$", 1)[0]
    except ValueError:
        return False
    return secrets.compare_digest(_hash_pwd(pwd, salt), stored)


def _get_kv(key: str):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def _set_kv(key: str, value: str):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES(?,?,?)",
                 (key, value, now()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 候选人端 API
@app.post("/api/login/request_code")
def request_code(body: dict):
    phone = (body.get("phone") or "").strip()
    if not re.fullmatch(r"1\d{10}", phone):
        raise HTTPException(400, "手机号格式不正确")
    conn = db()
    row = conn.execute("SELECT id FROM candidates WHERE phone=?", (phone,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "该手机号不在本次校招名单中，请确认后重试或联系领队")
    code = str(random.randint(100000, 999999))
    SMS_CODES[phone] = code
    # 生产环境此处对接短信网关；演示环境直接返回验证码
    return {"ok": True, "demo_code": code,
            "notice": "演示环境：验证码直接返回（生产环境将通过短信下发）"}


@app.post("/api/login/verify")
def verify(body: dict):
    phone = (body.get("phone") or "").strip()
    code = (body.get("code") or "").strip()
    if SMS_CODES.get(phone) != code:
        raise HTTPException(401, "验证码错误或已过期")
    del SMS_CODES[phone]
    conn = db()
    c = conn.execute("SELECT * FROM candidates WHERE phone=?", (phone,)).fetchone()
    conn.close()
    token = f"c{c['id']}-{random.randint(10**5, 10**6)}"
    persist_session(token, "candidate", {"id": c["id"]})
    return {"token": token, "name": c["name"], "status": c["status"]}


@app.post("/api/login/password")
def login_password(body: dict):
    """手机号 + 密码登录（候选人首次需先通过验证码登录后设置密码）"""
    phone = _norm_input(body.get("phone") or "")
    password = _norm_input(body.get("password") or "")
    conn = db()
    c = conn.execute("SELECT * FROM candidates WHERE phone=?", (phone,)).fetchone()
    conn.close()
    if not c or not c["password_hash"] or not _check_pwd(password, c["password_hash"]):
        raise HTTPException(401, "手机号或密码错误；未设置过密码请使用验证码登录")
    token = f"c{c['id']}-{random.randint(10**5, 10**6)}"
    persist_session(token, "candidate", {"id": c["id"]})
    return {"token": token, "name": c["name"], "status": c["status"]}


@app.get("/api/account/password")
def account_password_status(request: Request):
    sess = auth(request, "candidate")
    conn = db()
    row = conn.execute("SELECT password_hash FROM candidates WHERE id=?", (sess["id"],)).fetchone()
    conn.close()
    return {"has_password": bool(row and row["password_hash"])}


@app.post("/api/account/password")
def account_password_save(body: dict, request: Request):
    """候选人设置/修改登录密码：已设密码时需校验原密码"""
    sess = auth(request, "candidate")
    new_pwd = _norm_input(body.get("new_password") or "")
    old_pwd = _norm_input(body.get("old_password") or "")
    if not (6 <= len(new_pwd) <= 64):
        raise HTTPException(400, "密码长度需为 6-64 位")
    conn = db()
    row = conn.execute("SELECT password_hash FROM candidates WHERE id=?", (sess["id"],)).fetchone()
    if row and row["password_hash"]:
        if not _check_pwd(old_pwd, row["password_hash"]):
            conn.close()
            raise HTTPException(400, "原密码错误")
    conn.execute("UPDATE candidates SET password_hash=? WHERE id=?",
                 (_hash_pwd(new_pwd), sess["id"]))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    sess = auth(request, "candidate")
    conn = db()
    c = conn.execute("SELECT * FROM candidates WHERE id=?", (sess["id"],)).fetchone()
    hist = conn.execute(
        "SELECT status, note, created_at FROM status_history WHERE candidate_id=? ORDER BY id",
        (sess["id"],)).fetchall()
    conn.close()
    idx = STATUSES.index(c["status"]) if c["status"] in STATUSES else 0
    timeline = [{"status": s, "done": i < idx, "current": i == idx} for i, s in enumerate(STATUSES)]
    return {"candidate": dict(c), "history": [dict(h) for h in hist], "timeline": timeline,
            "next_step": NEXT_STEP.get(c["status"], "")}


@app.get("/api/chat/history")
def chat_history(request: Request):
    sess = auth(request, "candidate")
    conn = db()
    rows = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE candidate_id=? ORDER BY id",
        (sess["id"],)).fetchall()
    conn.close()
    return {"messages": [dict(r) for r in rows]}


@app.post("/api/chat")
def chat(body: dict, request: Request):
    """固定管线问答（Agent 灰度的对照基线；前端流式走 /api/chat/agent/stream）"""
    sess = auth(request, "candidate")
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "消息不能为空")
    cid = sess["id"]
    # 与 Agent 链路保持一致：先限流（记在 rate_events），落库时做 PII 掩码（S1.3）
    over, used, limit = ratelimit.record(cid)
    conn = db()
    conn.execute("INSERT INTO messages(candidate_id,role,content,created_at) VALUES(?,?,?,?)",
                 (cid, "user", pii.mask_pii(msg)[0], now()))
    conn.commit()
    conn.close()

    reply, ticket_id, _cites = fixed_chat_reply(cid, msg)
    conn = db()
    conn.execute("INSERT INTO messages(candidate_id,role,content,created_at) VALUES(?,?,?,?)",
                 (cid, "bot", reply, now()))
    conn.commit()
    conn.close()
    res = {"reply": reply, "ticket_id": ticket_id}
    if over:
        res["rate_limited"] = True
        res["notice"] = ratelimit.notice_text(used, limit)
    return res


@app.get("/api/my_tickets")
def my_tickets(request: Request):
    sess = auth(request, "candidate")
    conn = db()
    rows = conn.execute(
        "SELECT id, question, status, answer, created_at, resolved_at FROM tickets"
        " WHERE candidate_id=? ORDER BY id DESC", (sess["id"],)).fetchall()
    conn.close()
    return {"tickets": [dict(r) for r in rows]}


# ---------------------------------------------------------------- HR 端 API
@app.post("/api/admin/login")
def admin_login(body: dict):
    """运营端登录（超管 / HR 同一入口）：返回角色，前端按角色控制可见范围"""
    username = _norm_input(body.get("username") or "")
    password = _norm_input(body.get("password") or "")
    conn = db()
    acc = conn.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
    if not acc or acc["status"] != "active":
        conn.close()
        raise HTTPException(401, "账号或密码错误")
    if not _check_pwd(password, acc["password_hash"]):
        conn.close()
        raise HTTPException(401, "账号或密码错误")
    conn.execute("UPDATE accounts SET last_login_at=? WHERE id=?", (now(), acc["id"]))
    conn.commit()
    conn.close()
    token = f"a-{random.randint(10**8, 10**9)}"
    persist_session(token, "admin", {"username": username, "role": acc["role"], "uid": acc["id"]})
    return {"token": token, "username": username, "role": acc["role"],
            "display_name": acc["display_name"] or username}


@app.get("/api/admin/me")
def admin_me(request: Request):
    """当前运营端登录者的账号与角色（页面刷新后恢复角色视图）"""
    sess = auth(request, "admin")
    role = sess.get("role")
    if not role:                            # 迁移前的老会话：历史单管理员按超管兼容
        role = sess["role"] = "super"
    conn = db()
    row = conn.execute("SELECT display_name FROM accounts WHERE username=?",
                       (sess.get("username") or "",)).fetchone()
    conn.close()
    return {"username": sess.get("username") or ADMIN_USER, "role": role,
            "display_name": (row["display_name"] if row else "") or ""}


@app.get("/api/admin/account")
def admin_account(request: Request):
    """我的账号信息（超管 / HR 通用）"""
    sess = auth_admin(request)
    conn = db()
    row = conn.execute("SELECT username, role, display_name FROM accounts WHERE username=?",
                       (sess.get("username") or "",)).fetchone()
    conn.close()
    if row:
        return {"username": row["username"], "role": row["role"],
                "display_name": row["display_name"] or "", "custom_password": True}
    return {"username": sess.get("username") or ADMIN_USER, "role": sess.get("role") or "super",
            "display_name": "", "custom_password": False}


@app.post("/api/admin/account")
def admin_update_account(body: dict, request: Request):
    """修改我自己的用户名/密码：需校验当前密码；改后立即生效，当前会话保持"""
    sess = auth_admin(request)
    old = _norm_input(body.get("old_password") or "")
    new_u = _norm_input(body.get("username") or "")
    new_p = _norm_input(body.get("new_password") or "")
    conn = db()
    row = conn.execute("SELECT * FROM accounts WHERE username=?",
                       (sess.get("username") or "",)).fetchone()
    if not row:                             # 兼容：历史单管理员尚未迁移到 accounts
        conn.close()
        stored_h = _get_kv("admin_password_hash")
        if stored_h:
            if not _check_pwd(old, stored_h):
                raise HTTPException(400, "当前密码错误")
        elif old != ADMIN_PWD:
            raise HTTPException(400, "当前密码错误")
        if new_p:
            _set_kv("admin_password_hash", _hash_pwd(new_p))
        if new_u:
            _set_kv("admin_username", new_u)
        return {"ok": True, "username": new_u or sess.get("username")}
    if not _check_pwd(old, row["password_hash"]):
        conn.close()
        raise HTTPException(400, "当前密码错误")
    if not new_u and not new_p:
        conn.close()
        raise HTTPException(400, "请填写要修改的用户名或新密码")
    if new_u and new_u != row["username"]:
        if not re.fullmatch(r"[\w\-]{3,32}", new_u):
            conn.close()
            raise HTTPException(400, "用户名需为 3-32 位字母、数字、下划线或短横线")
        if conn.execute("SELECT 1 FROM accounts WHERE username=? AND id<>?", (new_u, row["id"])).fetchone():
            conn.close()
            raise HTTPException(400, "该用户名已存在")
        conn.execute("UPDATE accounts SET username=? WHERE id=?", (new_u, row["id"]))
    if new_p:
        if not (6 <= len(new_p) <= 64):
            conn.close()
            raise HTTPException(400, "密码长度需为 6-64 位")
        conn.execute("UPDATE accounts SET password_hash=? WHERE id=?", (_hash_pwd(new_p), row["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "username": new_u or row["username"]}


@app.get("/api/admin/settings")
def admin_get_settings(request: Request):
    """【超管专属】模型能力配置回显：密钥只返回是否已配置，不回传明文"""
    auth_super(request)
    return {
        "llm_base_url": agent_config.get_effective("LLM_BASE_URL"),
        "llm_model": agent_config.get_effective("LLM_MODEL"),
        "embed_model": agent_config.get_effective("EMBED_MODEL"),
        "agent_mode": agent_config.get_effective("AGENT_MODE"),
        "has_llm_key": bool(agent_config.get_effective("LLM_API_KEY")),
        "has_embed_key": bool(agent_config.get_embed_api_key()),
        "llm_ready": agent_config.llm_ready(),
        "rate_limit_per_hour": ratelimit.limit_per_hour(),
        "ckpt_retention_days": runtime.ckpt_retention_days(),
    }


@app.post("/api/admin/settings")
def admin_save_settings(request: Request, body: dict):
    """【超管专属】全局模型配置：立即生效，无需重启；Embedding 模型变更时自动重建向量索引"""
    auth_super(request)
    saved, reindexed = [], False

    embed_model_changed = ("embed_model" in body and body["embed_model"].strip()
                           and body["embed_model"].strip() != agent_config.get_effective("EMBED_MODEL"))
    for key in ("llm_base_url", "llm_api_key", "llm_model", "embed_api_key", "embed_model"):
        v = (body.get(key) or "").strip()
        if v:                                   # 留空 = 保持现有配置不变
            agent_config.set_setting(key, v)
            saved.append(key)
    if "agent_mode" in body and body["agent_mode"] in ("off", "shadow", "on"):
        agent_config.set_setting("agent_mode", body["agent_mode"])
        saved.append("agent_mode")

    # 限流阈值单独处理：上面循环里"留空表示不变"，但 0 是合法值（不限流），
    # 走循环会被当成"未填写"而丢弃，所以这里显式解析。
    if "rate_limit_per_hour" in body:
        raw = body.get("rate_limit_per_hour")
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            raise HTTPException(400, "限流阈值必须是整数（0 表示不限流）")
        if v < 0:
            raise HTTPException(400, "限流阈值不能为负数")
        agent_config.set_setting("rate_limit_per_hour", str(v))
        saved.append("rate_limit_per_hour")

    # 检查点保留天数同理：0 是合法值（不清理），必须显式解析
    if "ckpt_retention_days" in body:
        raw = body.get("ckpt_retention_days")
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            raise HTTPException(400, "检查点保留天数必须是整数（0 表示不清理）")
        if v < 0:
            raise HTTPException(400, "检查点保留天数不能为负数")
        agent_config.set_setting("ckpt_retention_days", str(v))
        saved.append("ckpt_retention_days")

    if saved:
        from agent import llm_service
        llm_service.reset_models()              # 清模型实例缓存，下次调用按新配置重建
    if embed_model_changed and agent_config.llm_ready():
        try:
            from agent import retrieval
            reindexed = retrieval.rebuild_index()
        except Exception:
            reindexed = False
    return {"ok": True, "saved": saved, "embed_reindexed": reindexed,
            "llm_ready": agent_config.llm_ready()}


# ---------------------------------------------------------------- 子账号管理（超管专属）
@app.get("/api/admin/eval/cases")
def eval_cases(request: Request):
    """评测集（只读展示，便于确认覆盖范围）"""
    auth_super(request)
    from agent.eval import load_cases
    return {"cases": load_cases()}


@app.post("/api/admin/eval/run")
def eval_run(request: Request, body: dict):
    """跑一轮评测回归；mode=intent 最便宜，agent 最贵（真实调工具，用独立账号跑完清理）"""
    auth_super(request)
    from agent.eval import run, save_run
    mode = (body.get("mode") or "intent").strip()
    if mode not in ("intent", "fixed", "agent"):
        raise HTTPException(400, "mode 只能是 intent / fixed / agent")
    try:
        limit = int(body.get("limit") or 0)
    except Exception:
        limit = 0
    r = run(mode=mode, limit=limit)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    rid = save_run(r)
    return {"run_id": rid, **r}


@app.get("/api/admin/eval/runs")
def eval_runs_list(request: Request):
    """历史评测结果（用于改动前后对比）"""
    auth_super(request)
    from agent.eval import latest_runs
    return {"runs": latest_runs(20)}


@app.get("/api/admin/eval/runs/{run_id}")
def eval_run_detail(request: Request, run_id: int):
    auth_super(request)
    from agent.eval import run_detail
    d = run_detail(run_id)
    if not d:
        raise HTTPException(404, "评测记录不存在")
    return d


@app.get("/api/admin/accounts")
def list_accounts(request: Request):
    auth_super(request)
    conn = db()
    rows = conn.execute(
        "SELECT id, username, role, display_name, status, created_at, last_login_at"
        " FROM accounts ORDER BY role, id").fetchall()
    conn.close()
    return {"accounts": [dict(r) for r in rows]}


@app.post("/api/admin/accounts")
def create_account(body: dict, request: Request):
    """新建 HR 子账号（超管可创建 hr 或 super）。默认 hr：只做运营，不碰模型配置"""
    sess = auth_super(request)
    username = _norm_input(body.get("username") or "")
    password = _norm_input(body.get("password") or "")
    role = body.get("role") or "hr"
    display_name = (body.get("display_name") or "").strip()
    if role not in ("hr", "super"):
        raise HTTPException(400, "角色只能是 hr 或 super")
    if not re.fullmatch(r"[\w\-]{3,32}", username):
        raise HTTPException(400, "用户名需为 3-32 位字母、数字、下划线或短横线")
    if not (6 <= len(password) <= 64):
        raise HTTPException(400, "密码长度需为 6-64 位")
    conn = db()
    if conn.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
        conn.close()
        raise HTTPException(400, "该用户名已存在")
    conn.execute(
        "INSERT INTO accounts(username,password_hash,role,display_name,status,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (username, _hash_pwd(password), role, display_name or username, "active", now()))
    conn.commit()
    conn.close()
    return {"ok": True, "username": username, "role": role}


@app.patch("/api/admin/accounts/{aid}")
def update_account(aid: int, body: dict, request: Request):
    """启/停用、重置密码、改名、改备注（改角色从 hr 升 super 需谨慎）"""
    sess = auth_super(request)
    conn = db()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "账号不存在")
    new_role = body.get("role") or row["role"]
    new_status = body.get("status") or row["status"]
    # 保护：不能停掉/降级自己；不能让系统失去最后一个超管
    if row["id"] == sess.get("uid") and (new_role != "super" or new_status != "active"):
        conn.close()
        raise HTTPException(400, "不能停用或降级当前登录的超管账号")
    if row["role"] == "super" and new_role != "super":
        n = conn.execute("SELECT COUNT(*) c FROM accounts WHERE role='super' AND status='active'").fetchone()["c"]
        if n <= 1:
            conn.close()
            raise HTTPException(400, "系统至少需要保留一个超级管理员")
    if new_role not in ("hr", "super"):
        conn.close()
        raise HTTPException(400, "角色只能是 hr 或 super")
    if new_status not in ("active", "disabled"):
        conn.close()
        raise HTTPException(400, "状态非法")
    if "password" in body:
        pwd = body["password"]
        if not (6 <= len(pwd) <= 64):
            conn.close()
            raise HTTPException(400, "密码长度需为 6-64 位")
        conn.execute("UPDATE accounts SET password_hash=? WHERE id=?", (_hash_pwd(pwd), aid))
    if "username" in body:
        new_u = (body["username"] or "").strip()
        if not re.fullmatch(r"[\w\-]{3,32}", new_u):
            conn.close()
            raise HTTPException(400, "用户名需为 3-32 位字母、数字、下划线或短横线")
        if conn.execute("SELECT 1 FROM accounts WHERE username=? AND id<>?", (new_u, aid)).fetchone():
            conn.close()
            raise HTTPException(400, "该用户名已存在")
        conn.execute("UPDATE accounts SET username=? WHERE id=?", (new_u, aid))
    if "display_name" in body:
        conn.execute("UPDATE accounts SET display_name=? WHERE id=?",
                     ((body["display_name"] or "").strip(), aid))
    conn.execute("UPDATE accounts SET role=?, status=? WHERE id=?", (new_role, new_status, aid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/admin/accounts/{aid}")
def delete_account(aid: int, request: Request):
    sess = auth_super(request)
    if aid == sess.get("uid"):
        raise HTTPException(400, "不能删除当前登录的账号")
    conn = db()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "账号不存在")
    if row["role"] == "super":
        n = conn.execute("SELECT COUNT(*) c FROM accounts WHERE role='super' AND status='active'").fetchone()["c"]
        if n <= 1:
            conn.close()
            raise HTTPException(400, "系统至少需要保留一个超级管理员")
    conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/admin/candidates")
def admin_candidates(request: Request):
    auth(request, "admin")
    conn = db()
    rows = conn.execute(
        "SELECT id, name, phone, school, major, position, site, status, created_at"
        " FROM candidates ORDER BY id").fetchall()
    conn.close()
    return {"candidates": [dict(r) for r in rows], "statuses": STATUSES}


@app.post("/api/admin/candidates/import")
def import_candidates(body: dict, request: Request):
    auth(request, "admin")
    items = body.get("items") or []
    conn = db()
    added, skipped = 0, 0
    for it in items:
        name, phone = (it.get("name") or "").strip(), (it.get("phone") or "").strip()
        if not name or not re.fullmatch(r"1\d{10}", phone):
            skipped += 1
            continue
        if conn.execute("SELECT 1 FROM candidates WHERE phone=?", (phone,)).fetchone():
            skipped += 1
            continue
        cur = conn.execute(
            "INSERT INTO candidates(name,phone,school,major,position,site,status,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (name, phone, it.get("school", ""), it.get("major", ""), it.get("position", ""),
             it.get("site", ""), "已初筛通过", now()))
        conn.execute("INSERT INTO status_history(candidate_id,status,note,created_at)"
                     " VALUES(?,?,?,?)", (cur.lastrowid, "已初筛通过", "名单导入", now()))
        added += 1
    conn.commit()
    conn.close()
    return {"added": added, "skipped": skipped}


@app.patch("/api/admin/candidates/{cid}/status")
def update_status(cid: int, body: dict, request: Request):
    auth(request, "admin")
    status = body.get("status")
    if status not in STATUSES:
        raise HTTPException(400, "非法状态")
    conn = db()
    if not conn.execute("SELECT 1 FROM candidates WHERE id=?", (cid,)).fetchone():
        conn.close()
        raise HTTPException(404, "候选人不存在")
    conn.execute("UPDATE candidates SET status=? WHERE id=?", (status, cid))
    conn.execute("INSERT INTO status_history(candidate_id,status,note,created_at)"
                 " VALUES(?,?,?,?)", (cid, status, body.get("note") or "HR 手动更新", now()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/admin/faqs")
def list_faqs(request: Request, source: str = ""):
    """source 为空返回全部；给值则按沉淀来源筛选（manual / ticket#id / doc_import / kb_seed）"""
    auth(request, "admin")
    src = (source or "").strip()
    conn = db()
    if src:
        rows = conn.execute(
            "SELECT * FROM faqs WHERE source=? ORDER BY id DESC", (src,)).fetchall()
        stats = conn.execute(
            "SELECT source, COUNT(*) c FROM faqs GROUP BY source ORDER BY c DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM faqs ORDER BY id DESC").fetchall()
        stats = conn.execute(
            "SELECT source, COUNT(*) c FROM faqs GROUP BY source ORDER BY c DESC").fetchall()
    conn.close()
    return {"faqs": [dict(r) for r in rows],
            "sources": [{"source": r["source"] or "manual", "count": r["c"]} for r in stats]}


@app.post("/api/admin/faqs")
def create_faq(body: dict, request: Request):
    auth(request, "admin")
    q, a = (body.get("question") or "").strip(), (body.get("answer") or "").strip()
    if not q or not a:
        raise HTTPException(400, "问题和答案不能为空")
    source = (body.get("source") or "manual").strip() or "manual"

    # S2.1 入库前查重：≥0.90 直接跳过，0.80~0.90 需 HR 显式确认（force=1）后入库
    dup = dedupe_mod.check(q, a)
    if dup["level"] in ("dup", "warn") and not body.get("force"):
        return {"ok": False, "skipped": True, "dedupe": dup}

    conn = db()
    conn.execute(
        "INSERT INTO faqs(question,answer,category,source,created_at) VALUES(?,?,?,?,?)",
        (q, a, body.get("category") or "通用", source, now()))
    conn.commit()
    conn.close()
    try:
        retrieval.sync_faqs()             # 新增即时生效（无 Key 时静默跳过）
    except Exception:
        pass
    return {"ok": True, "dedupe": dup}


@app.delete("/api/admin/faqs/by_source")
def delete_faqs_by_source(source: str, request: Request):
    """S2.2 按来源整体回滚（如撤销某次文档导入 / 某批工单沉淀）。

    注意必须注册在 `DELETE /api/admin/faqs/{fid}` 之前——
    否则 `by_source` 会被 `{fid}` 抢先匹配，再被 int 校验拒成 422。

    不允许回滚 manual：手工条目没有统一批次语义，误删无法按来源恢复。
    """
    auth(request, "admin")
    src = (source or "").strip()
    if not src or src == "manual":
        raise HTTPException(400, "仅支持按批次来源回滚（manual 不可整体删除）")
    conn = db()
    n = conn.execute("DELETE FROM faqs WHERE source=?", (src,)).rowcount
    conn.commit()
    conn.close()
    if n:
        try:
            retrieval.sync_faqs()
        except Exception:
            pass
    return {"ok": True, "deleted": n}


@app.put("/api/admin/faqs/{fid}")
def update_faq(fid: int, body: dict, request: Request):
    auth(request, "admin")
    conn = db()
    conn.execute("UPDATE faqs SET question=?, answer=?, category=?, active=? WHERE id=?",
                 (body.get("question"), body.get("answer"), body.get("category") or "通用",
                  1 if body.get("active", True) else 0, fid))
    conn.commit()
    conn.close()
    try:
        retrieval.sync_faqs()
    except Exception:
        pass
    return {"ok": True}


@app.delete("/api/admin/faqs/{fid}")
def delete_faq(fid: int, request: Request):
    auth(request, "admin")
    conn = db()
    conn.execute("DELETE FROM faqs WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    try:
        retrieval.sync_faqs()
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/admin/faqs/bulk")
def bulk_faqs(body: dict, request: Request):
    """批量入库（文档解析结果确认后调用），只同步一次向量索引。

    S2.1：逐条查重。命中的条目不入库，随响应回传（含已有条目 id 与相似度），
    前端据此提示 HR；HR 确认后对指定条目带 force=1 重提即可强制入库。
    """
    auth(request, "admin")
    items = body.get("items") or []
    source = (body.get("source") or "doc_import").strip() or "doc_import"
    force = bool(body.get("force"))

    to_add, skipped = [], []
    for it in items:
        q, a = (it.get("question") or "").strip(), (it.get("answer") or "").strip()
        if not q or not a:
            continue
        dup = dedupe_mod.check(q, a)
        if dup["level"] in ("dup", "warn") and not force and not it.get("force"):
            skipped.append({"question": q, "dedupe": dup})
            continue
        to_add.append((q, a, it.get("category") or "通用"))

    if not to_add:
        return {"ok": False, "added": 0, "skipped": skipped}

    conn = db()
    for q, a, cat in to_add:
        conn.execute(
            "INSERT INTO faqs(question,answer,category,source,created_at) VALUES(?,?,?,?,?)",
            (q, a, cat, source, now()))
    conn.commit()
    conn.close()
    try:
        retrieval.sync_faqs()
    except Exception:
        pass
    return {"ok": True, "added": len(to_add), "skipped": skipped}


@app.post("/api/admin/kb/preview")
def kb_preview(body: dict, request: Request):
    """S2.2 入库前预览：给出归一化后的问题表述与查重结论，不写库。

    HR 在前端可编辑 question 再确认入库（编辑后重新调一次本接口刷新查重结论）。
    """
    auth(request, "admin")
    q, a = (body.get("question") or "").strip(), (body.get("answer") or "").strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    return dedupe_mod.preview(q, a, (body.get("source") or "manual").strip() or "manual")
    return {"ok": True, "added": added}


# ---- 知识库文档导入解析 ----
DOC_MAX_BYTES = 10 * 1024 * 1024
DOC_MAX_CHARS = 60000

_QA_PROMPT = """你是校招知识库整理助手。从下面的文档片段中提取适合"校招候选人咨询机器人"使用的 FAQ 问答对。
要求：
- question：候选人可能的真实问法，口语化，不超过 40 字；
- answer：严格依据文档内容作答，准确、完整、可直接回复给候选人，不超过 300 字；文档中没有依据的信息禁止编造；
- category：从「通用」「回流」「内部」中选一个（HR 内部操作/流程用「内部」，往届生/回流相关用「回流」，其余用「通用」）；
- 若该片段没有可提取的问答内容，返回 []。
只输出 JSON 数组，格式：[{{"question":"...","answer":"...","category":"通用"}}]，不要输出任何其他文字。

文档片段：
{chunk}"""


def _extract_doc_text(filename: str, data: bytes) -> str:
    import io
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    if ext in ("txt", "md", "markdown", "csv"):
        for enc in ("utf-8", "gb18030"):
            try:
                return data.decode(enc)
            except Exception:
                continue
        return data.decode("utf-8", "ignore")
    if ext == "docx":
        from docx import Document
        doc = Document(io.BytesIO(data))
        parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for t in doc.tables:
            for row in t.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    if ext == "doc":
        raise HTTPException(400, "暂不支持旧版 .doc，请用 Word 另存为 .docx 后上传")
    raise HTTPException(400, f"不支持的文件类型 .{ext}（支持 txt/md/csv/docx/pdf）")


@app.post("/api/admin/kb/import_doc")
async def kb_import_doc(request: Request, file: UploadFile = File(...)):
    """上传文档 → 解析文本 → LLM 抽取 FAQ 问答对（返回待确认列表，不直接入库）"""
    auth(request, "admin")
    if not agent_config.llm_ready():
        raise HTTPException(400, "解析文档需要 LLM 能力，请先在【API Key 配置】中配置 LLM API Key")
    data = await file.read()
    if len(data) > DOC_MAX_BYTES:
        raise HTTPException(400, "文件超过 10MB 限制")
    try:
        text = _extract_doc_text(file.filename or "", data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"文档解析失败：{e}")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 30:
        raise HTTPException(400, "未能从文档中解析出有效文本内容（扫描版 PDF 暂不支持）")
    text = text[:DOC_MAX_CHARS]

    from agent.llm_service import get_chat_model
    llm = get_chat_model()
    CHUNK = 3000
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)][:20]
    items, failed = [], 0
    # S1.2 注入检测：分块入库前先过 guard 规则库，命中分块整体拒绝，
    # 其余分块照常抽取。拒绝明细回传给 HR（含片段位置），不静默丢弃。
    from agent.guard import find_hits
    rejected = []
    for ci, ch in enumerate(chunks):
        hits = find_hits(ch)
        if hits:
            rejected.append({"chunk_index": ci,
                             "reasons": [{"label": h["label"], "matched": h["matched"],
                                          "snippet": h["snippet"]} for h in hits]})
            failed += 1
            continue
        try:
            resp = llm.invoke(_QA_PROMPT.format(chunk=ch))
            raw = (resp.content or "").strip() if hasattr(resp, "content") else str(resp)
            if "reasoning_content" in getattr(resp, "additional_kwargs", {}) and not raw:
                raw = str(resp.additional_kwargs["reasoning_content"])
            s, e = raw.find("["), raw.rfind("]")
            if s < 0 or e <= s:
                failed += 1
                continue
            arr = json.loads(raw[s:e + 1])
            for it in arr if isinstance(arr, list) else []:
                q, a = str(it.get("question", "")).strip(), str(it.get("answer", "")).strip()
                if q and a:
                    cat = str(it.get("category", "通用")).strip()
                    items.append({"question": q, "answer": a,
                                  "category": cat if cat in ("通用", "回流", "内部") else "通用"})
        except Exception:
            failed += 1
    if not items:
        msg = "文档中未抽取出可用的问答内容"
        if rejected:
            msg += f"（{len(rejected)} 个片段命中注入检测被拒）"
        elif failed:
            msg += f"（{failed} 个片段解析失败）"
        raise HTTPException(400, msg)
    return {"filename": file.filename, "chars": len(text), "items": items,
            "failed_chunks": failed,
            "rejected_chunks": rejected}


@app.get("/api/admin/tickets")
def list_tickets(request: Request, status: str = ""):
    auth(request, "admin")
    conn = db()
    sql = ("SELECT t.*, c.name, c.phone, c.status AS candidate_status FROM tickets t"
           " JOIN candidates c ON c.id = t.candidate_id")
    params = ()
    if status in ("pending", "resolved"):
        sql += " WHERE t.status=?"
        params = (status,)
    sql += " ORDER BY t.id DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {"tickets": [dict(r) for r in rows]}


@app.post("/api/admin/tickets/{tid}/reply")
def reply_ticket(tid: int, body: dict, request: Request):
    auth(request, "admin")
    answer = (body.get("answer") or "").strip()
    if not answer:
        raise HTTPException(400, "回复内容不能为空")
    conn = db()
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "工单不存在")
    conn.execute("UPDATE tickets SET status='resolved', answer=?, resolved_at=?, faq_added=? WHERE id=?",
                 (answer, now(), 1 if body.get("add_to_faq") else 0, tid))
    conn.execute("INSERT INTO messages(candidate_id,role,content,created_at) VALUES(?,?,?,?)",
                 (t["candidate_id"], "bot",
                  f"【人工回复 · 工单#{tid}】{answer}", now()))
    # S2.1/S2.2：沉淀前查重。判为重复则跳过并回传已有条目，
    # 来源记为 ticket#id，便于按批次整体回滚。
    faq_result = None
    if body.get("add_to_faq"):
        src = f"ticket#{tid}"
        # 允许 HR 在预览里改过问题表述；没改则用规则归一化后的版本
        q_norm = ((body.get("faq_question") or "").strip()
                  or dedupe_mod.normalize_question(t["question"]))
        dup = dedupe_mod.check(q_norm, answer)
        if dup["level"] == "dup" and not body.get("force"):
            faq_result = {"ok": False, "skipped": True, "dedupe": dup, "source": src}
        else:
            conn.execute(
                "INSERT INTO faqs(question,answer,category,source,created_at) VALUES(?,?,?,?,?)",
                (q_norm, answer, "回流", src, now()))
            faq_result = {"ok": True, "skipped": False, "dedupe": dup, "source": src}
    conn.commit()
    conn.close()
    return {"ok": True, "faq": faq_result}


@app.get("/api/admin/stats")
def admin_stats(request: Request):
    auth(request, "admin")
    conn = db()
    total = conn.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"]
    by_status = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) c FROM candidates GROUP BY status"):
        by_status[r["status"]] = r["c"]
    pending = conn.execute("SELECT COUNT(*) c FROM tickets WHERE status='pending'").fetchone()["c"]
    resolved = conn.execute("SELECT COUNT(*) c FROM tickets WHERE status='resolved'").fetchone()["c"]
    faq_count = conn.execute("SELECT COUNT(*) c FROM faqs WHERE active=1").fetchone()["c"]
    reflow = conn.execute("SELECT COUNT(*) c FROM tickets WHERE faq_added=1").fetchone()["c"]
    top = [dict(r) for r in conn.execute(
        "SELECT question, COUNT(*) c FROM tickets GROUP BY question ORDER BY c DESC LIMIT 5")]
    conn.close()
    return {"total": total, "by_status": by_status, "pending": pending,
            "resolved": resolved, "faq_count": faq_count, "reflow": reflow, "top_questions": top}


# ---------------------------------------------------------------- 静态页
@app.get("/")
def home():
    return JSONResponse({"app": "校招咨询 Agent",
                         "candidate": "/ui/ (Vue 3)",
                         "admin": "/ui/#/admin (Vue 3)"})

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# Vue 3 + Element Plus 前端（Vite 构建产物），hash 路由：/ui/ 候选人端 · /ui/#/admin HR 后台
# 本地开发：frontend/dist；部署（打包上传）：app/web_dist
FRONTEND_DIST = BASE / "web_dist"
if not FRONTEND_DIST.exists():
    FRONTEND_DIST = BASE.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/ui", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="ui")


@app.middleware("http")
async def no_cache_index(request: Request, call_next):
    """index.html 禁止缓存：构建产物文件名带 hash，旧缓存会导致引用已删除的 JS 而白屏"""
    resp = await call_next(request)
    p = request.url.path
    if p in ("/ui", "/ui/", "/ui/index.html", "/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    # 兜底自愈：浏览器缓存的旧 index.html 引用了已被新构建删除的 hash JS（404）。
    # 此时返回一段引导脚本，带时间戳参数跳转，强制浏览器重新拉取最新 index.html。
    if resp.status_code == 404 and p.startswith("/ui/assets/") and p.endswith(".js"):
        return Response(
            content="location.replace('/ui/?r=' + Date.now() + location.hash);",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )
    return resp

init_db()
runtime.init_tables()                 # SQLAlchemy：agent_runs 等表

# 提示词版本同步：内容变了才插新版本，指纹写进 agent_runs 便于归因
try:
    _pf = prompts.sync()
    print(f"[agent] 提示词版本指纹 {_pf}")
except Exception as e:
    print(f"[agent] 提示词版本同步失败（不影响运行）: {e!r}")


@app.on_event("startup")
def startup_agent():
    # S2.3 启动时按保留天数回收过期检查点。
    # 必须早于任何图调用——图会在首次请求时才被实例化并占住 sqlite 连接，
    # 那时再做 VACUUM 就回收不了空间了。
    try:
        r = runtime.prune_checkpoints()
        print(f"[agent] 检查点清理：删除 {r['threads']} 个会话 / {r['deleted']} 条，"
              f"{r['size_before']//1024}KB → {r['size_after']//1024}KB")
    except Exception as e:
        print(f"[agent] 检查点清理失败（不影响运行）: {e!r}")
    try:
        n = retrieval.sync_faqs()     # FAQ → Qdrant（无 Key 时自动跳过）
        print(f"[agent] Qdrant 同步完成，更新 {n} 条知识条目")
    except Exception as e:
        print(f"[agent] Qdrant 初始化失败（不影响 keyword 检索）: {e!r}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
