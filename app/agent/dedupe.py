"""知识库沉淀查重（S2.1 / S2.2）

背景：工单回复勾选「沉淀为知识库」后原来是直接 `INSERT INTO faqs`，没有任何查重。
前车之鉴是 FAQ 曾被初始化逻辑重复播种 3 份（15 → 45 条），同一条知识在检索结果里
出现三次，把引用质量直接拖了下去。所以入库前必须先查一遍。

为什么判据取 dense 与关键词的较大值、而不是加权平均：
    漏判重复 → 知识库里躺着多条同义条目，长期污染检索；
    多提示一次「可能重复」→ HR 多点一下确认。
两者代价不对等，所以取保守侧。

分三档：
    ≥ DUP_LEVEL  判为已存在，跳过入库并回传已有条目 id
    ≥ WARN_LEVEL 提示人工确认（前端展示相似条目，HR 可编辑后强制入库）
    < WARN_LEVEL 正常入库
"""
import re

from . import retrieval

# 阈值是**实测标定**的，不是拍的。2026-10-26 用 text-embedding-v3 跑了 5 组改写 +
# 5 组同主题不同意图 + 3 组完全无关，得到：
#     同义改写        0.79 ~ 0.99（平均 0.90）
#     同主题不同意图  0.77 ~ 0.82
#     完全无关        0.42 ~ 0.60
# 结论：**「同义改写」和「同主题不同意图」在向量空间里是重叠的，没有干净的分界**。
# 所以自动跳过只敢放在最高置信区（≥0.92），其余一律交给 HR 确认——
# 把模糊区判定成"请人看一眼"不会出错，自动跳过判错则会丢掉一条新知识。
DUP_LEVEL = 0.92        # 语义相似度 ≥ 此值 → 自动跳过
DUP_KW_LEVEL = 0.95     # 关键词相似度 ≥ 此值（字面几乎一致）→ 自动跳过
WARN_LEVEL = 0.78       # ≥ 此值 → 提示人工确认

# 预览时归一化问题表述：去掉口头禅与冗余标点，保留语义主干
_LEAD = re.compile(r"^(请问|我想问一下|想问下|问一下|帮我查一下|咨询一下|你好[，,、]?|hi[，,、]?)+")
_TAIL = re.compile(r"[?？。．.!！\s]+$")
_MULTI = re.compile(r"\s{2,}")


def normalize_question(q: str) -> str:
    """把工单原话整理成更像知识库条目的问法（纯规则，不调模型）。

    只做「去口头禅 + 收尾标点统一 + 压空白」三件保守的事，
    不改写措辞——改写会引入模型措辞，反而制造假条目。
    """
    s = (q or "").strip()
    s = _LEAD.sub("", s).strip()
    s = _MULTI.sub(" ", s)
    s = _TAIL.sub("", s).strip()
    if not s:
        return (q or "").strip()
    # 原本没有疑问语气词的，补一个问号，保持条目形态一致
    if not re.search(r"[?？吗呢嘛啊]$", s) and len(s) <= 40:
        s += "？"
    return s


def check(question: str, answer: str = "") -> dict:
    """查重结论。

    返回：
        level      ok / warn / dup
        sim        最高相似度（0~1，dense 与关键词取大）
        top        最相似条目 dict（含 id / question / via）或 None
        candidates 相似候选列表（最多 3 条，供前端展示）
        message    可直接展示给 HR 的中文提示
    """
    q = (question or "").strip()
    if not q:
        return {"level": "ok", "sim": 0.0, "top": None, "candidates": [],
                "message": ""}

    hits = retrieval.similar_faqs(q, limit=3)
    if not hits:
        return {"level": "ok", "sim": 0.0, "top": None, "candidates": [],
                "message": ""}

    top = hits[0]
    sim = float(top["sim"])
    dense = max([h.get("sim_dense", 0.0) for h in hits] or [0.0])
    kw = max([h.get("sim_kw", 0.0) for h in hits] or [0.0])

    # 自动跳过只在高置信区：语义极像，或字面几乎完全一样
    if dense >= DUP_LEVEL or kw >= DUP_KW_LEVEL:
        level, msg = "dup", (
            f"知识库中已存在相似条目 #{top['id']}「{top['question']}」"
            f"（相似度 {sim:.2f}），已跳过本次沉淀")
    elif sim >= WARN_LEVEL:
        level, msg = "warn", (
            f"知识库中已有相近条目 #{top['id']}「{top['question']}」"
            f"（相似度 {sim:.2f}），请确认是否仍需入库")
    else:
        level, msg = "ok", ""

    return {"level": level, "sim": round(sim, 4), "top": top,
            "candidates": hits[:3], "message": msg}


def preview(question: str, answer: str = "", source: str = "manual") -> dict:
    """入库前预览：给出归一化后的问题表述 + 查重结论，不写库。"""
    return {"question": normalize_question(question),
            "raw_question": (question or "").strip(),
            "answer": (answer or "").strip(),
            "source": source,
            "dedupe": check(question, answer)}
