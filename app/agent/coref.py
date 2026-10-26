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
        newq = f"{anchor}{rest}" if rest else anchor
    else:                                   # 纯疑问谓语：主题前置
        rest = _TAIL.sub("", t.strip())
        newq = f"{anchor}{rest}" if rest else anchor
    return {"question": newq, "original": original, "resolved": True, "anchor": anchor}
