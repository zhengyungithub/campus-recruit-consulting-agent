"""答案核验 Verifier（第2期：规则通道 + LLM Judge 双轨）。

第0/1期只有一个能力：把回答里的硬信息（数字/日期/书名号/英文缩写）抠出来，
回引用原文逐条比对。它能抓住"编了个日期"，但抓不住三类真正的风险：

    ① 越权承诺 —— "你肯定能被录用""保证下周发 offer"，字字有据也可能违规
    ② 泄露他人信息 —— 回答里带了手机号/身份证，硬信息比对反而可能判它"有依据"
    ③ 答非所问 / 无据断言 —— 整句找不到任何依据支撑，但恰好没写硬数字

因此第2期把核验拆成两条通道：

┌ 规则通道 L1（零延迟、零成本、可解释，永远先跑）
│   A. 红线扫描 REDLINES：承诺录用 / 确定性承诺排期 / 泄露个人标识 / 泄漏内部实现
│   B. 硬信息比对：日期、数字、书名号、英文缩写逐条回引用原文比对（第0期能力）
│   C. 句子级引用锚点：逐句找最相似依据句，标出"无依据句"（弱主张）
└ LLM Judge 通道 L2（按需调用）
    规则通道判 pass 时不调用（省成本省延迟）；一旦卡住（partial/fail）或命中红线嫌疑，
    才交给模型做语义级裁定：句子是否被依据支撑、是否答非所问、是否越权。

对外契约（不要破坏，eval / runtime / SSE 都依赖）：
    status: pass | partial | fail | block（block = 红线，优先级最高）
    ratio / items / unsupported：硬信息比对结果（沿用第0期字段名）
"""
import json
import re
from difflib import SequenceMatcher

from . import config, prompts

_NUM = re.compile(r"\d+(?:\.\d+)?")
_DATE = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}月\d{1,2}[日号]")
_BOOK = re.compile(r"《(.+?)》")
_ALPHA = re.compile(r"[A-Za-z]{2,}")
_LIST_MARKER = re.compile(r"(?m)^\s*\d+\s*[\.、\)）]\s*")   # 列表序号非硬信息
# 系统内部产物（工具名等）不是事实主张，不算硬信息
_WHITELIST = {"PDF", "IMG", "HR", "OFFER", "APP", "URL", "HTTP", "HTTPS",
              "GET", "MY", "PROGRESS", "LIST", "TICKETS", "KNOWLEDGE",
              "SEARCH", "CREATE", "TICKET", "THE", "AND"}

_SENT_SPLIT = re.compile(r"[。！？!?\n]+|[；;](?=\s*\S)")

# ------------------------------------------------------------------ 红线规则
# 每条规则给出：code / name / advice（回炉时直接喂给模型，告诉它怎么改）
_PROMISE = r"(保证|確保|确保|一定能|肯定会|一定会|必定会|包[过过]|稳了|没问题[,，]?你|放心[,，]?你)"
_OFFER = r"(录用|录取|被录|入职|入职通知|通过面试|拿到 ?offer|发放 ?offer|发 ?offer|签约|签三方|网签成功)"
_DENY = r"(不保证|无法保证|不敢保证|不能保证|不确定|不代表)"
# 日期词分两类，**相对时长（N 个工作日 / N 天内）必须算进来**：
# 第 S5.3 轮实测里模型答"通常在复试结束后 7 个工作日内，会完成录用审批并发出 offer"，
# 而知识库只说"审批通过后制作 offer 发放表"，压根没有 7 个工作日这个数。
# 旧版不认"N 个工作日"（只认 明天/下周/X月X日），这句直接放行——
# 候选人会拿着"7 个工作日"去质问 HR，这是本项目最硬的一条红线。
_DATEWORD = (
    r"(明天|后天|今天|下周|本周|下[一二三四五六日天]|本周[一二三四五六日天]"
    r"|\d{1,2}月\d{1,2}[日号]|\d{1,2}[/-]\d{1,2}"
    # N 个工作日 / N 个自然日 / N 天 / N 周 / N 个月，含中文数字与"左右/以内/之内"后缀
    r"|[\d一二两三四五六七八九十]+\s*个?\s*(?:工作日|自然日|天|周|星期|月)\s*(?:内|以内|之内|左右|之内|后)?"
    r")")

# 触发词同样放宽：真实回答很少说"保证/一定"，最常见的是"通常在…会…""一般要…""预计…"。
# 只认承诺词等于只拦最不像人话的那种说法。
_DATE_CLAIM = r"(保证|确保|一定|肯定|将于|会于|计划于|通常|一般|正常(?:情况)?下|预计|大概|大约|最晚|不超过)"

_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_INTERNAL = re.compile(
    r"(knowledge_search|get_my_progress|create_ticket|list_my_tickets|tool_call|function_call"
    r"|thread_id|candidate_id|agent_steps|\{[\"']?[a-z_]{3,}[\"']?\s*:)", re.I)
# 检索融合模式是内部元数据，模型照抄成「来源：hybrid」回显给用户要拦。
# 写得**很窄**（只认这几个枚举值），否则正常的「来源：知识库」会被误伤——
# 红线一旦误伤就是"给一次改写机会 + 二次降级"，代价很高。
_INTERNAL_SOURCE = re.compile(
    r"(来源|source|来源字段)\s*[:：]\s*(hybrid|dense|keyword|bm25|vector|qdrant)", re.I)

REDLINE_RULES = [
    {"code": "promise_offer", "name": "承诺录用结果",
     "advice": "不得对录用/入职结果作任何承诺，只能陈述流程与当前状态，或说明需以官方通知为准。"},
    {"code": "invent_schedule", "name": "未经依据的确定性排期",
     "advice": "不得凭空给出具体办理/发放时间；无依据时改为说明流程节点并建议等待官方通知。"},
    {"code": "privacy", "name": "泄露个人标识信息",
     "advice": "不得在回答中出现手机号/身份证号/邮箱等个人标识，如需核对请引导候选人自行查看。"},
    {"code": "internal_leak", "name": "泄漏内部实现细节",
     "advice": "不得出现工具名、函数名、JSON、内部字段，全部改用自然语言陈述结果。"},
]

# 寒暄/过渡/引导句不是事实主张，句子级锚点跳过它们，避免把服务用语算成"无依据"。
# 元叙述句尤其关键："你当前处于【X】环节"复述的是查询结果而非知识库条目，
# 强行要求它在原文里找到落点会把完全合规的回答判成编造——第2期首版就踩了这个坑。
_NON_FACTUAL = re.compile(
    r"(你好|您好|谢谢|感谢|不客气|抱歉|不好意思|祝你|希望|请问|如有|还有什么|随时(问|找我)|联系我"
    r"|你当前|你的(当前|状态|进度|环节)|目前|系统(显示|查询)|已为你|已创建|工单|以.{0,6}为准"
    r"|建议|推荐|如果|如需|你可以|请(留意|注意|关注|以|前往|在))")
_MIN_SENT_LEN = 8


# ------------------------------------------------------------------ 工具
def _grams(s: str, n: int = 2):
    s = re.sub(r"[\s，。！？；：、（）《》【】“”，,\.\!\?\-]+", "", s)
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _contain(a: str, b: str) -> float:
    """a 有多少比例的字面依据落在 b 里（不对称相似度：答案句 ⊆ 依据句 时得分高）"""
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga)


def _split_sentences(text: str):
    return [s.strip() for s in _SENT_SPLIT.split(text) if s and s.strip()]


def _extract_items(text: str):
    text = _LIST_MARKER.sub("", text)        # 剥离列表序号，避免误判为数字硬信息
    items = []
    items += [("日期", m) for m in _DATE.findall(text)]
    text_wo_date = _DATE.sub("", text)
    items += [("数字", m) for m in _NUM.findall(text_wo_date)]
    items += [("书名", m) for m in _BOOK.findall(text)]
    items += [("英文", m) for m in set(_ALPHA.findall(text))
              if m.upper() not in _WHITELIST]
    return items


def _supported(item: str, contexts: list, allow_values: list | None = None) -> bool:
    # 白名单：系统自己产生的受托事实（刚建的工单号、本人手机号）不算"编造"
    if any(str(a) and (str(a) == item or item in str(a)) for a in allow_values or []):
        return True
    for ctx in contexts:
        if item in ctx:
            return True
        for ctx_line in re.split(r"[。；;\n]", ctx):
            if not ctx_line:
                continue
            if item in ctx_line:
                return True
            if SequenceMatcher(None, item, ctx_line[: len(item) * 2 + 10]).ratio() >= 0.85:
                return True
    return False


# ------------------------------------------------------------------ 红线扫描
def scan_redlines(answer: str, allow_values: list | None = None,
                  contexts: list | None = None) -> list:
    """红线扫描（纯规则、零成本、可用于任何模式生成的回答）。

    allow_values：本人信息白名单（自己的手机号/姓名不算泄露），由调用方按 cid 传入。
    contexts：依据原文；**出现在引用原文里的个人标识不算泄露**（比如知识库条目写了咨询电话），
              否则会把"照抄知识库联系方式"误判为泄露——这是第2期实现时最容易踩的坑。
    """
    allow = set(allow_values or [])
    ctx_all = "\n".join(contexts or [])
    # 依据比对要抗空格差异：知识库写"7 个工作日"、模型写"7个工作日"是同一件事，
    # 字面包含判断会把它当成"依据里没有"而误判红线。红线误伤的代价很高
    # （一次改写 + 二次仍踩就降级），值得多这一步归一化。
    ctx_norm = re.sub(r"\s+", "", ctx_all)
    hits = []
    seen = set()

    def _add(code, evidence):
        key = (code, evidence)
        if key in seen:
            return
        seen.add(key)
        rule = next(r for r in REDLINE_RULES if r["code"] == code)
        hits.append({"code": code, "name": rule["name"], "advice": rule["advice"],
                     "evidence": evidence[:80]})

    for sent in _split_sentences(answer):
        # ① 越权承诺：承诺词 + 录用词，且没有被否定词裹住
        if re.search(_PROMISE, sent) and re.search(_OFFER, sent) and not re.search(_DENY, sent):
            _add("promise_offer", sent)
        # ② 确定性排期：断言口吻 + 具体时间词，且依据里没有这个时间
        if re.search(_DATE_CLAIM, sent) and re.search(_DATEWORD, sent):
            for d in re.findall(_DATEWORD, sent):
                if re.sub(r"\s+", "", d) not in ctx_norm:
                    _add("invent_schedule", sent)
                    break
        # ③ 个人标识
        for pat, tag in ((_PHONE, "手机号"), (_IDCARD, "身份证号"), (_EMAIL, "邮箱")):
            for m in pat.findall(sent):
                val = m if isinstance(m, str) else m[0]
                if val in allow or val in ctx_all:
                    continue
                _add("privacy", f"{tag} {val[:6]}****")
        # ④ 内部实现泄漏
        m = _INTERNAL.search(sent) or _INTERNAL_SOURCE.search(sent)
        if m:
            _add("internal_leak", m.group(0))

    return hits


# ------------------------------------------------------------------ 句子级引用锚点
def sentence_anchors(answer: str, contexts: list) -> dict:
    """把回答拆句，逐句找最相似的依据句。返回 {checked, grounded_ratio, ungrounded}

    为什么用字面 2-gram 而不是向量：核验要的是"这句话能不能在原条目里找到落点"，
    字面重合度既够用又可解释，还不用再发一次 Embedding 请求（第1期被它坑过 40 分钟）。
    """
    if not contexts:
        return {"checked": [], "grounded_ratio": None, "ungrounded": []}
    checked, grounded, skipped = [], 0, 0
    for s in _split_sentences(answer):
        # 过短 / 寒暄过渡句 / 反问句：不作事实主张，跳过
        if len(s) < _MIN_SENT_LEN or (_NON_FACTUAL.search(s) and len(s) < 30) or s.endswith("？"):
            skipped += 1
            continue
        best, best_ctx = 0.0, ""
        for ctx in contexts:
            for line in re.split(r"[。；;\n]", ctx):
                if len(line) < 4:
                    continue
                sc = _contain(s, line)
                if sc > best:
                    best, best_ctx = sc, line
        ok = best >= config.VERIFY_SENTENCE_PASS
        grounded += 1 if ok else 0
        checked.append({"sentence": s, "score": round(best, 3),
                        "supported": ok, "evidence": best_ctx[:80]})
    total = len(checked)
    return {"checked": checked,
            "grounded_ratio": round(grounded / total, 3) if total else None,
            "ungrounded": [c["sentence"] for c in checked if not c["supported"]],
            "skipped": skipped}


# ------------------------------------------------------------------ LLM Judge
def _judge_model():
    """Judge 用独立模型实例（temperature=0 保证裁判稳定）。

    刻意不走 llm_service.get_chat_model()：那边做了单实例缓存，temperature 参数只在首次生效，
    Judge 与主干线共用会互相干扰（主干要 0.3 的多样性，裁判要 0 的确定性）。
    """
    from langchain_openai import ChatOpenAI
    # timeout 从 20 提到 30：换用 qwen3.8-flash 后实测出现 OpenAITimeoutError，
    # 超时会让 verdict 变成 error（裁判空转，白跑一次调用）；与主干超时对齐
    return ChatOpenAI(model=config.LLM_MODEL, api_key=config.LLM_API_KEY,
                      base_url=config.LLM_BASE_URL, temperature=0,
                      timeout=30, max_retries=0)


_judge_llm = None


def judge_answer(question: str, answer: str, citations: list) -> dict:
    """LLM 语义裁判；不可用时原样返回 called=False，调用方按规则通道结果处理（不放大风险）。"""
    global _judge_llm
    if not config.llm_ready():
        return {"called": False, "verdict": "skip", "reason": "未配置 LLM"}
    try:
        if _judge_llm is None:
            _judge_llm = _judge_model()
        evidence = "\n---\n".join(f"Q：{c.get('question','')}\nA：{c.get('answer','')}"
                                  for c in (citations or [])[:4]) or "（本次未检索到知识库条目）"
        resp = _judge_llm.invoke(prompts.load("judge").format(question=question or "（无）",
                                                    answer=(answer or "")[:1500],
                                                    evidence=evidence[:3000]))
        raw = (getattr(resp, "content", "") or "").strip()
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
        data = json.loads(raw)
        return {"called": True, "verdict": data.get("verdict", "unsupported"),
                "issues": data.get("issues", [])[:5], "note": data.get("note", "")}
    except Exception as e:              # Judge 挂了不能拖垮作答链路，保守放行规则通道结论
        return {"called": True, "verdict": "error", "reason": repr(e)[:160]}


def _judge_enabled(mode: str) -> bool:
    m = (mode or config.AGENT_JUDGE or "auto").lower()
    return m != "off"


# ------------------------------------------------------------------ 主入口
def apply_judge(v: dict, jinfo: dict) -> dict:
    """把 Judge 结论合并进规则核验结果（graph 的两阶段用法调用它）。

    裁定口径（与 verify_answer 内部完全一致）：
        红线 block > 规则 fail（且 Judge 也摇头）> partial > pass；
        Judge 单独说 unsupported 时**只能把 status 抬到规则已经给出的等级**，不能自行判死。
    """
    jinfo = jinfo or {"called": False, "verdict": "skip"}
    rule_status = v.get("rule_status", v.get("status"))
    v["judge"] = jinfo
    if v.get("redlines"):
        v["status"] = "block"
    elif jinfo.get("verdict") in ("unsupported", "off_topic"):
        v["status"] = {"fail": "fail", "partial": "partial"}.get(rule_status, "pass")
    elif jinfo.get("verdict") == "ok" and rule_status == "fail":
        v["status"] = "partial"
    else:
        v["status"] = rule_status          # Judge 没说话（skip/error）时以规则为准
    return v


def verify_answer(answer: str, citations: list, question: str = "",
                  allow_values: list | None = None, judge: str = "auto",
                  use_judge: bool | None = None) -> dict:
    """双通道核验。

    allow_values：本人信息白名单（手机号/姓名），避免"向本人复述本人信息"被判泄露。
    judge：auto（规则卡住才调 LLM）/ on（总是调）/ off（纯规则）。
    """
    contexts = []
    for c in citations or []:
        for k in ("answer", "question"):
            if c.get(k):
                contexts.append(c[k])

    out = {"question": (question or "")[:200]}

    # ---- L1-A 红线（最高优先级） ----
    redlines = scan_redlines(answer or "", allow_values, contexts)
    out["redlines"] = [{"code": h["code"], "name": h["name"],
                        "evidence": h["evidence"], "advice": h["advice"]} for h in redlines]

    # ---- L1-B 硬信息比对 ----
    items = _extract_items(answer or "")
    checked = []
    for kind, it in items:
        supported = _supported(it, contexts, allow_values)
        checked.append({"kind": kind, "value": it, "supported": supported})
    hard_ratio = round(sum(1 for c in checked if c["supported"]) / len(checked), 3) if checked else None
    out["items"] = checked
    out["unsupported"] = [c["value"] for c in checked if not c["supported"]]

    # ---- L1-C 句子级引用锚点 ----
    anch = sentence_anchors(answer or "", contexts)
    sent_ratio = anch["grounded_ratio"]
    out["sentences"] = anch["checked"]
    out["ungrounded"] = anch["ungrounded"]
    out["hard_ratio"] = hard_ratio
    out["sentence_ratio"] = sent_ratio

    # ---- 综合规则结论 ----
    if hard_ratio is None and sent_ratio is None:
        rule_status, ratio = "pass", 1.0
    else:
        parts = [r for r in (hard_ratio, sent_ratio) if r is not None]
        ratio = round(sum(parts) / len(parts), 3)
        if ratio >= config.VERIFY_PASS:
            rule_status = "pass"
        elif ratio >= config.VERIFY_FAIL:
            rule_status = "partial"
        else:
            rule_status = "fail"
    # 句子锚点是弱信号（元叙述/改写句本来就找不到落点）：硬信息对得上时，
    # 单凭它没有依据不能直接判 fail，压到 partial 让 Judge 做语义裁定，避免误杀合规回答。
    if rule_status == "fail" and (hard_ratio is None or hard_ratio >= config.VERIFY_PASS):
        rule_status = "partial"
    out["ratio"] = ratio
    out["rule_status"] = rule_status

    # ---- L2 LLM Judge（按需）----
    mode = "off" if use_judge is False else (judge or "auto")
    # 红线已一票否决（block 不因 Judge 翻案），此时没必要再花一次模型调用
    need = (mode == "on") or (rule_status != "pass" and not redlines)
    jinfo = {"called": False, "verdict": "skip"}
    if _judge_enabled(mode) and need and answer:
        jinfo = judge_answer(question, answer, citations)

    # ---- 终局裁定（口径见 apply_judge 注释：Judge 只能捞回，不能单独判死）----
    out = apply_judge(out, jinfo)
    out["note"] = ("；".join([r["name"] for r in redlines]) or
                   jinfo.get("note") or (", ".join(i.get("reason", "") for i in jinfo.get("issues", []))
                                         if jinfo.get("issues") else "") or
                   {"pass": "通过", "partial": "部分支撑", "fail": "缺乏依据"}[out["status"]])
    return out
