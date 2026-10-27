"""固定管线（无 LLM 问答）：从 main.py 抽出，供 /api/chat 与 Agent fallback/灰度对比共用"""
import json
import re
from datetime import datetime

from .store import get_conn
from .retrieval import text_similarity
from . import intent as intent_mod, kb

GREET_RE = re.compile(r"^(你好|您好|hi|hello|嗨|哈喽|在吗)[!！。~\s]*$", re.I)
THANKS_RE = re.compile(r"^(谢谢|多谢|感谢|thanks|thank you)[!！。~\s]*$", re.I)

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


def _now():
    return datetime.now().isoformat(timespec="seconds")


def fixed_chat_reply(cid: int, msg: str, allow_ticket: bool = True):
    """返回 (reply, ticket_id, citations)；不写 messages 表（调用方负责统一落库）

    路由：先做意图识别（规则优先 / LLM 兜底），再按意图分派：
      greeting       → 直接寒暄
      progress_query → 查库返回真实状态 + 下一步提示
      human_handoff  → 直接建工单（人工办理类诉求不再走知识库，避免答非所问）
      kb_qa          → 知识库检索；未命中则视为超范围并转工单
    """
    it = intent_mod.classify(msg, cid).get("intent", "kb_qa")

    if it == "greeting":
        if GREET_RE.match(msg):
            return ("你好！我是校招咨询助手，可以解答校招流程、材料、网签等问题，"
                    "也可以在【我的进度】查看当前环节。请问有什么可以帮你？"), None, []
        if THANKS_RE.match(msg):
            return "不客气～祝求职顺利！还有问题随时问我。", None, []

    if it == "progress_query":
        with get_conn() as conn:
            c = conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        return (f"你当前处于【{c['status']}】环节。下一步：{NEXT_STEP.get(c['status'], '请留意通知。')}"
                f" 可在【我的进度】查看完整时间线。"), None, []

    if it == "out_of_scope":
        tid = _create_ticket(cid, msg) if allow_ticket else None
        return (f"这个问题超出了我的自动回答范围，{'已为你生成反馈工单（编号 #' + str(tid) + '），' if tid else ''}"
                f"工作人员会尽快处理，处理结果将在【我的反馈】中显示。"
                f"你也可以拨打站点领队电话咨询。"), tid, []

    if it == "human_handoff" and allow_ticket:
        tid = _create_ticket(cid, msg)
        return (f"这个需求需要人工办理，我已为你生成反馈工单（编号 #{tid}），"
                f"工作人员会尽快处理，处理结果将在【我的反馈】中显示。"), tid, []

    hits = _faq_search(msg)
    if hits:
        best = hits[0]
        return best["answer"] + f"\n\n（参考条目：{best['question']}）", None, hits[:2]

    tid = _create_ticket(cid, msg) if allow_ticket else None
    return (f"这个问题超出了我的自动回答范围，{'已为你生成反馈工单（编号 #' + str(tid) + '），' if tid else ''}"
            f"工作人员会尽快处理，处理结果将在【我的反馈】中显示。"
            f"你也可以拨打站点领队电话咨询。"), tid, []


def _faq_search(msg: str):
    with get_conn() as conn:
        faqs = conn.execute(f"SELECT * FROM faqs WHERE {kb.PUBLISHED_SQL}").fetchall()
    scored = sorted(((text_similarity(msg, f["question"]), f) for f in faqs),
                    key=lambda x: -x[0])
    return [{"question": f["question"], "answer": f["answer"], "score": round(s, 4)}
            for s, f in scored if s >= 0.35]


def _create_ticket(cid: int, msg: str) -> int:
    """只建工单，不写 messages（消息由调用方统一落库，避免重复）"""
    with get_conn() as conn:
        hist = conn.execute(
            "SELECT role, content FROM messages WHERE candidate_id=? ORDER BY id DESC LIMIT 4",
            (cid,)).fetchall()
        context = json.dumps([dict(r) for r in reversed(hist)], ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO tickets(candidate_id,question,context,created_at) VALUES(?,?,?,?)",
            (cid, msg, context, _now()))
        return cur.lastrowid
