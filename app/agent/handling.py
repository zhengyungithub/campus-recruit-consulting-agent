"""办理类诉求的多轮状态机（S4.2）：改期 / 换站点 / 催办。

为什么需要它
------------
改期、换站点、催办这类诉求，光靠"每轮让模型重新理解对话"有两个问题：
1. 缺失信息（改到哪天？换到哪个城市？催哪一项？）全靠模型临场判断要不要问，行为不稳定；
2. 用户隔一轮补充的信息，没有地方"挂"到上一张工单上，常常被当成新问题另建一张单。

设计取舍：**先办后补，绝不拦住建单**
------------------------------------
首轮不追问、不阻塞——照常建单（否则"帮我改一下面试时间"这类明确办理请求会被拖成
两轮，现有评测集里 #22/#23/#25 等用例会直接失败）。建完单再在同一句话里补一句追问，
把槽位缺口告诉用户；用户下一轮补充时，状态机把信息回填到**已建的那张工单**上。
这样既保住了"明确提出就必须办"的既有口径，又拿到了多轮收集信息的能力。

状态存在 `handling_sessions` 表（按候选人 + 24 小时有效），跨请求持久化。
"""
import json
import re
from datetime import datetime, timedelta

from .store import get_conn

# ---- 槽位定义 ----------------------------------------------------------
KIND_DEFS = {
    "reschedule": {
        "label": "改期/重新安排面试",
        "slot": "new_time", "slot_label": "新的面试时间",
        "ask": "另外，方便告诉我你想改到哪一天/哪个时段吗？（比如「下周三下午」）",
        "detect": re.compile(
            r"(改期|改个?时间|换个时间|重新(安排|约|排)|调整(面试)?时间|延期|改约)"
            r"|((改|换|调)[^。！？]{0,6}(时间|日期|时段|场次))", re.I),
        # 时间表达式：相对日 / 绝对日期 / 时段 / 钟点，四选一命中即可
        "extract": re.compile(
            r"((下周[一二三四五六日天]|本周[一二三四五六日天]|周[一二三四五六日天]|明天|后天|大后天|今天)"
            r"[^，。！？]{0,6}(上午|下午|中午|晚上|早上)?[^，。！？]{0,6}"
            r"|([0-9]{1,2}\s*月\s*[0-9]{1,2}\s*[日号]?)"
            r"|([0-9]{1,2}\s*[:：]\s*[0-9]{2}|[0-9]{1,2}\s*点(半|整)?)"
            r"|(上午|下午|中午|晚上|早上))", re.I),
    },
    "change_site": {
        "label": "更换面试站点",
        "slot": "target_site", "slot_label": "目标站点/城市",
        "ask": "另外，方便告诉我你想换到哪个站点或城市吗？",
        "detect": re.compile(
            r"(换(个|一个|一下)?(面试|笔试|复试|初面)?(城市|站点|地点|地方)"
            r"|调岗|转岗|换方向|(换|调)[^。！？]{0,4}(站点|城市))", re.I),
        "extract": re.compile(
            r"((北京|上海|广州|深圳|武汉|成都|杭州|南京|西安|长沙|重庆|苏州|天津|郑州|青岛|合肥|厦门)"
            r"|(换|调|去|到|成)\s*([^，。！？\s]{2,8}(市|站|点)))", re.I),
    },
    "urge": {
        "label": "催办进度",
        "slot": "target", "slot_label": "催办事项",
        "ask": "另外，方便告诉我你希望优先催办的是哪一项吗？（比如面试安排 / 测评 / 体检 / 网签）",
        "detect": re.compile(r"(催(一下|一催|办)|加急|紧急处理)", re.I),
        "extract": re.compile(
            r"(面试|测评|体检|网签|三方|offer|录用|入职|简历|初面|复试|结果|进度|审批)", re.I),
    },
}

SESSION_TTL_HOURS = 24


# ---- 存储 --------------------------------------------------------------
def ensure_table():
    with get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS handling_sessions("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     "candidate_id INTEGER NOT NULL,"
                     "kind TEXT NOT NULL,"
                     "slot_value TEXT,"
                     "ticket_id INTEGER,"
                     "status TEXT NOT NULL DEFAULT 'open',"
                     "created_at TEXT, updated_at TEXT)")


# ---- 识别与抽取 --------------------------------------------------------
def detect_kind(question: str):
    """判断一句话属于哪类办理诉求；换站点优先于改期（"换个面试站点"含"换"）"""
    if not question:
        return None
    if KIND_DEFS["change_site"]["detect"].search(question):
        return "change_site"
    for kind in ("reschedule", "urge"):
        if KIND_DEFS[kind]["detect"].search(question):
            return kind
    return None


def extract_slot(kind: str, question: str):
    """抽取槽位值；抽不到返回 None。返回时去掉首尾空白。"""
    if not kind or not question:
        return None
    m = KIND_DEFS[kind]["extract"].search(question)
    if not m:
        return None
    v = m.group(0).strip()
    # 去掉句尾语气词（"明天上午十点可以吗" → "明天上午十点"），
    # 这个值会原样写进工单给 HR 看，带"可以吗"显得不干净
    v = re.sub(r"(可以吗|行吗|好吗|吧|呢|吗|呀|啊)+$", "", v).strip("，,。. ")
    return v or None


def _now():
    return datetime.now().isoformat(timespec="seconds")


def active_session(cid: int):
    """取该候选人 24 小时内未关闭的会话（同一时间只维护一个）"""
    ensure_table()
    cutoff = (datetime.now() - timedelta(hours=SESSION_TTL_HOURS)).isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM handling_sessions WHERE candidate_id=? AND status='open' "
            "AND updated_at>=? ORDER BY id DESC LIMIT 1", (cid, cutoff)).fetchone()
    return dict(row) if row else None


def open_session(cid: int, kind: str, ticket_id):
    ensure_table()
    with get_conn() as conn:
        conn.execute("UPDATE handling_sessions SET status='closed' "
                     "WHERE candidate_id=? AND status='open'", (cid,))
        cur = conn.execute(
            "INSERT INTO handling_sessions(candidate_id,kind,ticket_id,status,created_at,updated_at)"
            " VALUES(?,?,?,'open',?,?)", (cid, kind, ticket_id, _now(), _now()))
        return cur.lastrowid


def _append_to_ticket(ticket_id: int, note: str) -> bool:
    """把补充信息追加到已建工单（改 context 字段，兼容它是 JSON 数组的情况）"""
    if not ticket_id:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT context FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            return False
        raw = row["context"] or "[]"
        try:
            cur = json.loads(raw)
            if isinstance(cur, list):
                cur.append(note)
                new = json.dumps(cur, ensure_ascii=False)
            else:
                new = f"{raw}\n{note}"
        except Exception:
            new = f"{raw}\n{note}"
        conn.execute("UPDATE tickets SET context=? WHERE id=?", (new, ticket_id))
    return True


def close_session(session_id: int, value: str):
    with get_conn() as conn:
        conn.execute("UPDATE handling_sessions SET slot_value=?, status='closed', updated_at=? "
                     "WHERE id=?", (value, _now(), session_id))


# ---- 每轮的进出处理 ----------------------------------------------------
def before_turn(cid: int, question: str) -> dict:
    """本轮开头：若有未闭合会话且本轮补齐了槽位 → 回填工单并给出确认话术。

    返回 {"filled": 补充到的值 or None, "note": 追加到回答里的确认语, "session": ...}
    """
    sess = active_session(cid)
    if not sess:
        return {"filled": None, "note": "", "session": None}
    value = extract_slot(sess["kind"], question or "")
    if not value:
        # 仍没补上：把"还差什么"告诉模型，让回答保持追问而不是另起话题
        d = KIND_DEFS[sess["kind"]]
        return {"filled": None, "session": sess,
                "context": (f"【办理中】候选人正在办理：{d['label']}（工单 #{sess['ticket_id']}）。"
                            f"仍然缺少「{d['slot_label']}」，本轮也没补充到，请在回答里继续追问这一项。")}
    ok = _append_to_ticket(sess["ticket_id"], f"【补充信息】{KIND_DEFS[sess['kind']]['slot_label']}：{value}")
    close_session(sess["id"], value)
    note = (f"\n\n已把「{value}」补充到工单 #{sess['ticket_id']}，工作人员会据此处理。"
            if ok else f"\n\n已记录你的补充：「{value}」。")
    return {"filled": value, "note": note, "session": sess, "ticket_id": sess["ticket_id"]}


def already_said(answer: str, text: str) -> bool:
    """回答里是否已经问过同一句，用于避免重复追问。

    S5.3 实测：模型自己在末尾问了"方便告诉我你想改到哪一天/哪个时段吗？"，
    状态机又追加了一遍同一句，候选人看到同一问题问两次。
    判据用去标点后的前 8 字指纹——足够特异（"方便告诉我你想改"），
    又不会被"另外/此外"这类连接词差异影响。
    """
    norm = lambda s: re.sub(r"[，。！？、；：\s（）()【】「」“”\"'…·]", "", s or "")  # noqa: E731
    a = norm(answer)
    b = re.sub(r"^(另外|此外|还有)", "", norm(text))
    return len(b) >= 8 and b[:8] in a


def after_turn(cid: int, question: str, intent: str, ticket_id) -> str:
    """本轮结尾：办理类诉求若槽位缺失 → 开会话并返回追问语（追加到回答末尾）。"""
    if intent != "human_handoff":
        return ""
    kind = detect_kind(question or "")
    if not kind:
        return ""                                   # 转人工但不属三类办理（如索取联系方式）
    if extract_slot(kind, question or ""):
        return ""                                   # 信息齐了，不用追问
    open_session(cid, kind, ticket_id)
    return "\n\n" + KIND_DEFS[kind]["ask"]


def context_block(cid: int, question: str) -> str:
    """给 node_context 用的提示块（只在本轮开始前调用一次，避免重复查库）"""
    return before_turn(cid, question).get("context", "")
