"""记忆层（第3期）：短期窗口 + 长期画像 + 会话主题锚点。

三层结构：
    ① 短期 最近 6 轮原文对话（保持连贯，靠 recent_messages）
    ② 长期 候选人画像（站点/岗位/当前环节，来自 candidates 表）+ Qdrant 历史问答向量召回
    ③ 会话 追问锚点：上一轮聊的是什么主题（next_turn 里的 extract_topic / last_topic）

第3期新增 ②③ 的"可用性"改造：画像从 JSON dump 改成可读的自然语言块并带上最近关注的话题，
并且让上层能在用户的追问里把指代（"那这个要多久"）还原成带主题的完整问题（见 coref.py）。
"""
import json
import re

from .store import get_conn

SHORT_WINDOW = 6

# 抽主题时剥掉的疑问/虚词（只去掉句首句尾的冗余，保留实体）
_Q_WORD = re.compile(
    r"(请问|麻烦|我想|问一下|咨询一下|想问|帮我|请问一下|不好意思|打扰)"
    r"|(怎么|怎样|如何|什么|多少|多久|几天|哪里|哪里|是否|可否|能不能|可以|能否)"
    r"|(吗|呢|吧|啊|了|的|吗呢)|[？?！!。，,、\s]", )
_TOPIC_STOP = re.compile(r"^(那|这|它|再|还有|然后|另外|以及|接着|同时)")
_MAX_TOPIC = 24


def recent_messages(cid: int, n: int = SHORT_WINDOW):
    """最近 n 条对话（candidates 的通用 messages 表）"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE candidate_id=? ORDER BY id DESC LIMIT ?",
            (cid, n)).fetchall()
    return [{"role": "user" if r["role"] == "user" else "assistant",
             "content": r["content"]} for r in reversed(rows)]


def last_user_question(cid: int) -> str:
    """上一轮用户提的问题（不含本轮），用于追问指代还原"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT content FROM messages WHERE candidate_id=? AND role='user'"
            " ORDER BY id DESC LIMIT 1 OFFSET 1", (cid,)).fetchone()
    return row["content"] if row else ""


def candidate_profile(cid: int):
    with get_conn() as conn:
        c = conn.execute("SELECT name, school, major, position, site, status"
                         " FROM candidates WHERE id=?", (cid,)).fetchone()
    return dict(c) if c else {}


# 主题在"谓语/疑问词"处截止：体检资料有什么要求 → 体检资料
# "要准备/要带/要交"这组动宾必须显式列出：只写单字"要"会误伤"三方重要吗"（切成"三方重"），
# 不写又会把"复试要准备些什么"切成"复试要准备些"——这个残句当作指代锚点拼进下一轮检索时，
# 会把 query 稀释成"复试要准备些offer 什么时候发"（S5.3 实测）。
_CUT_AT = re.compile(r"(怎么|如何|什么|哪些|多久|什么时候|何时|几号|多少|几天|几周"
                     r"|要准备|要注意|要带|要交|要传|要提供|要填|要签|要不要"
                     r"|能不能|可不可以|可以|能否|需要|包含|包括|含有|还有"
                     r"|有|是)+.*$")
_MIN_TOPIC_LEN = 2


def extract_topic(text: str) -> str:
    """抽出一句话里的实体主题："体检资料有什么要求" → "体检资料"。

    做法是"截到第一个谓语/疑问词 + 去停用语气词"，刻意不用分词/模型：
    这里的产出要给下层检索当 query 用，可预测比聪明更重要。
    截断后太短（说明实体就在谓语后面）就退回去停用词的整句型主题。
    """
    t = _TOPIC_STOP.sub("", (text or "").strip())
    t = re.sub(r"[（(].*?[)）]", "", t)
    m = _CUT_AT.search(t)
    core = t[:m.start()] if m and m.start() >= _MIN_TOPIC_LEN else t
    core = core.strip("的了吗呢吧啊，。？?！!、 ")
    if len(core) < _MIN_TOPIC_LEN:            # 实体信息太少，回退：只去疑问语气词
        core = _Q_WORD.sub("", t).strip("的了吗呢吧啊，。？?！!、 ")
    return core[:_MAX_TOPIC]


def recent_topics(cid: int, n: int = 3):
    """最近问过的主题（去重、去空），用于画像里的最近关注列表"""
    out = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT content FROM messages WHERE candidate_id=? AND role='user'"
            " ORDER BY id DESC LIMIT ?", (cid, n * 3)).fetchall()
    for r in rows:
        t = extract_topic(r["content"])
        if t and t not in out:
            out.append(t)
        if len(out) >= n:
            break
    return out


def profile_block(cid: int, include_topics: bool = True) -> str:
    """结构化画像 → 自然语言块（第3期）。

    原来是 json.dumps 一把梭，模型读到的是一串 key/value；改成可读句式后，
    回答里带上"你在 XX 站点投递的 XX 岗位"这类个性化信息明显更自然。
    """
    p = candidate_profile(cid)
    if not p:
        return ""
    lines = []
    who = p.get("name") or "（未留名）"
    pos, site = p.get("position"), p.get("site")
    pj = f"投递岗位：{pos}" + (f"（{site}）" if site and site != "—" else "")
    lines.append(f"候选人：{who}｜{pj}｜当前环节：{p.get('status') or '未知'}")
    school, major = p.get("school"), p.get("major")
    if school and school != "—":
        lines.append(f"毕业院校：{school}" + (f" · {major}" if major and major != "—" else ""))
    if include_topics:
        topics = recent_topics(cid)
        if topics:
            lines.append("最近关注：" + "、".join(topics))
    return "\n".join(lines)


def build_context_block(cid: int, query: str) -> str:
    """注入 system prompt 的记忆上下文：画像 + 当前环节 + 相似历史问答"""
    import json
    p = candidate_profile(cid)
    hist = retrieval_recall(cid, query)
    parts = []
    if p:
        parts.append(f"候选人画像：{json.dumps(p, ensure_ascii=False)}")
    if hist:
        parts.append("相似历史问答（供参考，不要当作最新事实）：\n" +
                     "\n".join(f"Q: {h['question']}\nA: {h['answer'][:200]}" for h in hist))
    return "\n\n".join(parts)


def retrieval_recall(cid: int, query: str):
    from . import retrieval
    return retrieval.recall_history(cid, query)


def remember(cid: int, question: str, answer: str):
    """问答落向量记忆（chat_history 集合，失败静默）。

    入向量前做 PII 掩码（S1.3）：向量库是"可被任意检索命中的副产品"，
    一旦写入裸号码，任何一次相似问答都可能把它带出来。
    """
    from . import pii, retrieval
    safe_q, _ = pii.mask_pii(question)
    retrieval.store_chat_memory(cid, safe_q, answer)
