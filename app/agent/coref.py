"""追问指代还原（第3期）：把"那这个要多久"还原成"体检资料要多久出结果"。

为什么单独做这一步：
    多轮对话里，用户第二轮几乎不会重复主题词。下游的意图识别、知识库检索、
    planner 都拿到一句没有主语的话，只能靠猜——表现为答非所问，或者胡乱检索一通。
    与其在 prompt 里反复强调"请结合上下文"，不如在进入各决策点之前，
    先用规则把指代补回成一个自足的问题（零延迟、零成本、可解释）。

为什么用规则而不是让模型改写：
    ① 每轮多一次 LLM 调用，延迟和成本都翻倍；
    ② 改写结果不可控，模型可能顺手把问题也改了，评测没法归因；
    ③ 这类追问模式高度固定，规则覆盖性价比最高。模型留给真正模糊的句子。

覆盖范围：
    ① 指代词开头：那/这/它/再/还有/然后/另外
    ② 纯疑问谓语（没有宾语）：多久/什么时候/要多久/怎么办/还要准备什么吗
    ③ 极短追问：≤6 字且不含实体词
"""
import re

# 以指代/承接词开头（可连续剥离："那这个要多久" → "要多久"）
_REF_HEAD = re.compile(r"^(还有呢|那么这个|那个|这种|这类|这个|它这个|然后|另外|此外"
                      r"|那|这|它|再|还有|以及|接着|同时)+")
# 句尾残留的语气词
_TAIL = re.compile(r"[呢啊吗吧呀哦啦，,。]+[？?！!]*$|[？?！!]+$")
# 整句只有疑问谓语，没有承载主题的词（"还要多久？""什么时候能弄好"）
_BARE_QUESTION = re.compile(
    r"^(多久|要多久|还要多久|需要多久|多长时间|什么时候|啥时候|几号|几天|怎么办|怎么处理"
    r"|如何弄|怎么弄|怎么弄啊|要做吗|可以吗|行吗|了吗|完成了吗|通过了吗|弄好了吗"
    r"|我呢|为什么|为啥|咋办|咋弄)[？?。!！\s]*$")
# 主题词最小长度：短于此认为没主题信息
_MIN_SELF_SUFFICIENT = 7
# 判"剥掉指代词后是否还缺主题"用的疑问词表。
# 判据不是长度而是**疑问词前面有没有实体**：
#   "offer 什么时候发" —— 疑问词前面有 "offer"，主题自带，不用拼
#   "要多久能出结果"   —— 疑问词贴着句首，说明在问上一轮那件事，必须拼
# 只看长度会翻车：门禁 #34「那这个要多久能出结果」剥完是"要多久能出结果"（够长），
# 但它仍然没有主题，第一版就因此把 #34 判挂。
_QWORD_AT = re.compile(r"(什么|哪些|哪个|哪里|怎么|如何|多久|多长时间|什么时候|啥时候"
                       r"|几号|几天|为什么|为啥|能不能|可以吗)")
# 疑问词前至少要有这么多字符，才认为它前面挂着实体主题
_MIN_TOPIC_PREFIX = 4
# 剥完句首指代后，句中若仍留这些词，说明指的仍是上一轮的事，必须还原
_RESIDUAL_REF = re.compile(r"(这个|那个|它|这些|那些|该|此|上述|上面|刚才|前面)")
# 这些词即使在句里也不算有主题 assures
_FILLER = re.compile(r"^(你好|您好|谢谢|好的|嗯|哦|明白|知道啦|ok|OK|收到)")


def needs_resolution(q: str) -> bool:
    """判断这句话是否需要还原。宁可少改：有主题的整句一律不动。"""
    t = (q or "").strip()
    if not t or _FILLER.match(t):
        return False
    if _BARE_QUESTION.match(t):
        return True
    if _REF_HEAD.match(t):
        return True
    # 极短且不含实质内容
    return len(t) <= 6 and not _FILLER.match(t)


def resolve(question: str, prev_question: str = "", prev_topic: str = "") -> dict:
    """把追问还原成自足问题。

    prev_topic 优先（已抽好的实体主题），没有就用上一轮问题现场抽。
    返回 {question, original, resolved, anchor}
    """
    from .memory import extract_topic

    original = (question or "").strip()
    if not needs_resolution(original):
        return {"question": original, "original": original, "resolved": False, "anchor": ""}

    anchor = (prev_topic or "").strip() or extract_topic(prev_question)
    if not anchor:
        return {"question": original, "original": original, "resolved": False, "anchor": ""}

    t = original
    m = _REF_HEAD.match(t)
    if m:                                   # 剥掉指代词，用主题接上
        rest = _TAIL.sub("", t[m.end():].strip())
        # 剥掉指代词后剩下的话本身已经是一个完整问题，就不要再拼上一轮主题。
        # 反例（S5.3 实测）：上一轮"复试要准备些什么？"，这一轮"那 offer 什么时候发？"
        # ——"那"只是口语转折，不是指代；硬拼成"复试要准备些offer 什么时候发"去检索，
        # 命中被稀释、意图被判成 kb_qa，最终模型拿不到"你当前在已约复试"这个关键事实。
        qm = _QWORD_AT.search(rest)
        self_sufficient = (len(rest) >= _MIN_SELF_SUFFICIENT
                           and not _RESIDUAL_REF.search(rest)
                           and qm is not None and qm.start() >= _MIN_TOPIC_PREFIX)
        if self_sufficient:
            return {"question": rest, "original": original, "resolved": False,
                    "anchor": anchor}
        newq = f"{anchor}{rest}" if rest else anchor
    else:                                   # 纯疑问谓语：主题前置
        rest = _TAIL.sub("", t.strip())
        newq = f"{anchor}{rest}" if rest else anchor
    return {"question": newq, "original": original, "resolved": True, "anchor": anchor}
