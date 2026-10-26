"""PII 输入侧脱敏（S1.3）。

背景：项目此前只有**输出侧**红线（`verifier.REDLINES.privacy`，禁止回答里吐号码），
但**输入侧与存储侧是裸的**——候选人发来的手机号、身份证、邮箱会原样落到 `messages` 表
和 Qdrant 向量记忆里，任何能查库 / 读向量 payload 的人都能看到。

**关键约束：只脱敏"写进去的副本"，不动当次请求的原文。**
    工具调用需要真实输入（HR 后台按号码查人、工单正文要给 HR 看），
    所以掩码只发生在两个写入点：写 `messages` 表、写向量记忆。
    graph 内部流动的文本仍是原文，检索与工具行为不受影响。

覆盖范围刻意保守：手机号 / 身份证（15 位与 18 位）/ 邮箱三类。
    银行卡号、住址这类要语义才能判定的不做——**规则误杀的代价
    （把用户原话改得无法处理）高于漏脱敏的代价**。
    POC 阶段先堵最高频的三类，其余留给后续的数据分级方案。
"""
import re

# 手机号：1[3-9] 开头 11 位；前后不允许再跟数字，避免从长数字串里切出一段
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

# 身份证 18 位：6 位地址码 + 4 位年（19xx/20xx）+ 月 + 日 + 3 位顺序 + 1 位校验（可为 X）
_ID18 = re.compile(
    r"(?<!\d)(\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)")

# 身份证 15 位（老式）：6 位地址 + 2 位年 + 月日 + 3 位顺序，无校验位
_ID15 = re.compile(
    r"(?<!\d)(\d{6}\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)")

_EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def mask_phone(s: str) -> str:
    """13812341201 → 138****1201（保留前 3 后 4，够人工核对又不构成完整号码）"""
    return s[:3] + "****" + s[-4:]


def mask_id(s: str) -> str:
    """身份证保留前 6（地址码，排查有用）与后 4，中间全掩"""
    return s[:6] + "*" * (len(s) - 10) + s[-4:]


def mask_email(local: str, domain: str) -> str:
    """zheng@example.com → z***@example.com（域名保留，便于识别同一组织）"""
    return f"{local[:1] or '*'}***@{domain}"


def mask_pii(text: str):
    """对写入副本做掩码。

    返回 `(脱敏文本, 命中类型列表)`。命中类型供单测与审计用，
    不落库——**记录"本次脱敏了什么"本身也是一种敏感信息**。
    """
    if not text:
        return text, []
    hits = []

    def _id18(m):
        hits.append("id_card")
        return mask_id(m.group(1))

    def _id15(m):
        hits.append("id_card")
        return mask_id(m.group(1))

    def _phone(m):
        hits.append("phone")
        return mask_phone(m.group(1))

    def _email(m):
        hits.append("email")
        return mask_email(m.group(1), m.group(2))

    # 顺序有讲究：先身份证、再手机号。
    # 18 位身份证里能切出"看起来像手机号"的 11 位片段，若先跑手机号规则
    # 会把身份证拦腰截断，掩码后既不像号也不像证，人工核对时会误判。
    out = _ID18.sub(_id18, text)
    out = _ID15.sub(_id15, out)
    out = _PHONE.sub(_phone, out)
    out = _EMAIL.sub(_email, out)
    return out, hits
