"""意图识别层：规则优先（零延迟、可解释）→ LLM 兜底（模糊/长句/隐含诉求）。

定位：
    意图只做"路由决策"，不产生答案；它决定后续 Agent 该走哪条工具策略，
    并把结果以 SSE `intent` 事件外发、随轨迹落 agent_runs，供灰度对比与人工复核。

意图集合与编排策略：
    greeting       寒暄/致谢          → 不调工具，直接自然语言回应
    progress_query 查询个人进度/结果  → 必须调 get_my_progress，只陈述事实，不承诺录用
    kb_qa          校招政策/流程咨询  → 必须调 knowledge_search，只依据检索结果
    human_handoff  需人工办理/决策    → 调 create_ticket（改期/换岗/申诉/催办/索取联系方式）
    out_of_scope   与校招无关        → 明确说明无法自动回答，引导转人工

设计取舍：
    - 规则覆盖高频、边界清晰的表达，命中即返回（无网络开销，结果可解释、可审计）；
    - 规则未命中且 LLM 可用时才走模型分类，输出严格 JSON，解析失败一律降级为 kb_qa，
      保证"分类失败不影响主链路"。
"""
import json
import re
from typing import Optional

from . import config

INTENT_LABELS = {
    "greeting": "寒暄问候",
    "progress_query": "查询个人进度",
    "kb_qa": "校招政策/流程咨询",
    "human_handoff": "需人工办理",
    "out_of_scope": "超出自动回答范围",
}

# 建议路由（供工具编排与提示词注入使用）
ROUTE_HINTS = {
    "greeting": "无需调用任何工具，直接用自然语言简短寒暄并说明你能提供哪些帮助。",
    "progress_query": "请调用 get_my_progress 查询该候选人的真实状态后再回答；只陈述系统里的状态，"
                      "不要预测结果、不要承诺录用、不要编造时间。",
    "kb_qa": "请先调用 knowledge_search 检索知识库，只依据检索到的条目回答，并在结尾标注依据条目；"
             "若检索结果无法支撑，改为建议转人工。",
    "human_handoff": "该诉求需要人工办理或人工决策，请调用 create_ticket 创建反馈工单，"
                     "并明确告知候选人已转人工、可在【我的反馈】查看处理结果。"
                     "注意：只要候选人明确提出了办理请求（帮我改/申请换/催一下/联系我），"
                     "就必须建单——知识库里有自助处理建议时可以同时告知，但不能因此不建单。",
    "out_of_scope": "该问题超出校招咨询范围，不要臆测作答，请调用 create_ticket 转人工，"
                    "并说明机器人当前无法自动回答。",
}

# (意图, 正则, 置信度, 命中说明) —— 顺序即优先级，越靠前越优先
RULES = [
    ("greeting", re.compile(r"^(你好|您好|hi|hello|嗨|哈喽|在吗|早上好|中午好|晚上好|hey)[!！。~\s]*$", re.I), 0.95, "问候语"),
    ("greeting", re.compile(r"^(谢谢|多谢|感谢|thanks|thank you|thx|辛苦了)[!！。~\s]*$", re.I), 0.95, "致谢/寒暄"),

    ("progress_query", re.compile(r"(我的?的?(进度|状态|情况|结果|环节|流程))"
                                  r"|(我(现在)?(到|在|处于|走到)[哪那什么]"
                                  r"(一步|个|步|环节|阶段|状态|情况)?)", re.I), 0.92, "询问个人当前所处环节"),
    ("progress_query", re.compile(r"(通过没|过了没|有没有过|通过了吗|过了吗|录用了吗|录用没有|有希望吗|结果出来)", re.I), 0.9, "询问是否通过/录用结果"),
    # 注意：必须带第一人称"我"，否则"面试资料审核不通过怎么办""offer 什么时候发"
    # 这类政策咨询会被误判成查个人进度（评测集 regression 发现）
    ("progress_query", re.compile(r"我[^。！？]{0,10}(offer|体检|三方|网签|资料|测评|面试)[^。！？]{0,10}(发|下来|通过|审核|进度|状态|结果|怎么样|好没)", re.I), 0.85, "询问自己的具体环节进展"),

    # 必须人工办理的诉求：优先于知识问答，避免把"帮我改期"当成"改期政策咨询"
    ("human_handoff", re.compile(r"(改期|改时间|改个时间|换个时间|重新(安排|约|排)|调整(面试)?时间|延期|改约)", re.I), 0.9, "需要人工办理改期/重新安排"),
    # "帮我改一下面试时间"这类口语表达不在上面固定词组里；用"改/换/调 + 时间"兜住，
    # 这样断网或无模型额度时也能正确路由（第2期额度耗尽时暴露：原先完全依赖 LLM 兜底）
    ("human_handoff", re.compile(r"(改|换|调)[^。！？]{0,6}(时间|日期|时段|场次)", re.I), 0.88, "要求变更既定时间"),
    # 同理必须带诉求词（我想/帮我/能不能…），否则"入职后怎么换岗"这类政策咨询会被误判成换岗申请
    ("human_handoff", re.compile(r"(我(想|要|能|可以)?|帮我|申请|能不能|可不可以|可以|想|需要).{0,4}"
                                 r"(换(个|一个|一下)?(面试|笔试|复试|初面)?"
                                 r"(城市|站点|地点|岗位|岗|部门|职位|面试官)|调岗|转岗|换方向)", re.I), 0.9, "需要人工协调变更站点/岗位"),
    ("human_handoff", re.compile(r"(申诉|投诉|举报|催(一下|一催|办)|加急|紧急处理|人工|转人工|找.{0,4}(hr|HR|面试官|领队|工作人员))", re.I), 0.9, "需要人工介入处理"),
    ("human_handoff", re.compile(r"((联系|咨询).{0,6}(方式|电话|微信|二维码|邮箱))|(电话|微信|邮箱|二维码).{0,4}(多少|是什么|给我|发我)", re.I), 0.85, "索取联系渠道"),

    # 与校招无关
    ("out_of_scope", re.compile(r"(天气|股票|基金|彩票|游戏|外卖|快递|八卦|星座|算命|减肥|健身)", re.I), 0.8, "与校招无关的话题"),
]

_LLM_PROMPT = """你是校招咨询机器人的意图分类器，只判断用户这句话的意图，不要回答问题。

可选 intent（只能选其一）：
- greeting：寒暄、打招呼、致谢
- progress_query：询问"我自己"的进度/当前环节/状态/是否通过/录用结果
- kb_qa：询问校招流程、政策、材料要求、操作指引等通用知识
- human_handoff：需要人工办理或人工决策，例如改期、换站点/换岗、申诉投诉、催办、索取联系方式
- out_of_scope：与校招完全无关的话题

只输出一行 JSON，不要解释、不要代码块，格式：{{"intent":"<上述之一>","reason":"<不超过20字>"}}

用户问题：{q}"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _rule_classify(text: str) -> Optional[dict]:
    for intent, rx, conf, reason in RULES:
        if rx.search(text):
            return {"intent": intent, "confidence": conf, "source": "rule", "reason": reason}
    return None


def _retrieval_classify(text: str) -> Optional[dict]:
    """检索兜底：与知识库已有问法高度相似 → 直接判为 kb_qa。

    放在 LLM 之前的原因：政策类问题（如"错过面试怎么办"）在语义上容易被模型
    误判成"需要人工协调改期"。以知识库命中为据既更快（零网络开销）也更准。
    """
    try:
        from .store import get_conn
        from .retrieval import text_similarity
        with get_conn() as conn:
            faqs = conn.execute("SELECT question FROM faqs WHERE active=1").fetchall()
        best = max((text_similarity(text, f["question"]) for f in faqs), default=0.0)
        if best >= 0.5:
            return {"intent": "kb_qa", "confidence": round(min(0.9, best), 2),
                    "source": "retrieval", "reason": f"知识库强命中({best:.2f})"}
    except Exception:
        pass
    return None


def _llm_classify(text: str) -> Optional[dict]:
    """模型兜底分类；任何异常都返回 None，由调用方降级。"""
    if not config.llm_ready():
        return None
    try:
        from .llm_service import get_chat_model
        llm = get_chat_model(temperature=0)
        raw = llm.invoke(_LLM_PROMPT.format(q=text[:200]))
        content = getattr(raw, "content", "") or ""
        m = _FENCE_RE.search(content)
        if m:
            content = m.group(1)
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(content[start:end + 1])
        intent = (data.get("intent") or "").strip()
        if intent not in INTENT_LABELS:
            return None
        return {"intent": intent, "confidence": 0.7, "source": "llm",
                "reason": (data.get("reason") or "模型判定")[:40]}
    except Exception:
        return None


def keep_after_coref(rseen: dict, original_question: str) -> dict:
    """指代还原之后的意图纠偏（第3期）。

    场景：上一轮聊"体检资料有什么要求"，这轮追问"那这个要多久能出结果"。
    还原后是"体检资料要多久能出结果"——它延续的是**政策咨询**，但模型很容易
    看到"多久/出结果"就判成 progress_query（查个人进度），于是路由到
    get_my_progress，答非所问。

    规则：只要本问不含第一人称诉求（"我的/我通过了吗/…"），就把
    progress_query 纠回 kb_qa —— 用户是在追问上一件事，不是在问自己到哪一步。
    """
    if (rseen or {}).get("intent") != "progress_query":
        return rseen
    q = original_question or ""
    if re.search(r"(我的?|我(通过|录用|应聘|投)|我.{0,6}(进度|环节|状态|情况))", q):
        return rseen
    return {**rseen, "intent": "kb_qa", "confidence": 0.8,
            "source": "rule-override", "reason": "追问延续上一轮政策咨询，非查询个人进度"}


def classify(question: str, cid: Optional[int] = None, emit_fn=None) -> dict:
    """识别一句话的意图。

    返回 {intent, label, confidence, source, reason}
    - 规则命中 → 直接返回（source=rule）
    - 未命中且 LLM 可用 → 模型判定（source=llm）
    - 都不可用 → 降级 kb_qa（保证主链路不受影响）
    """
    text = (question or "").strip()
    # 规则 → 知识库命中 → 模型判定 → 兜底 kb_qa
    r = _rule_classify(text)
    if r is None:
        r = _retrieval_classify(text)
    if r is None and len(text) >= 4:
        r = _llm_classify(text)
    if r is None:
        r = {"intent": "kb_qa", "confidence": 0.5, "source": "fallback", "reason": "未命中规则且未启用模型"}
    r = {**r, "label": INTENT_LABELS.get(r["intent"], r["intent"])}
    if emit_fn:
        emit_fn({"type": "intent", "intent": r["intent"], "label": r["label"],
                 "confidence": r["confidence"], "source": r["source"], "reason": r["reason"]})
    return r


def route_hint(intent: str) -> str:
    return ROUTE_HINTS.get(intent, ROUTE_HINTS["kb_qa"])
