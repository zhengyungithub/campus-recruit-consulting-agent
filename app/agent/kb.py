"""知识库审核（S6.1）：批量来源先入草稿，确认后才对检索可见。

为什么单独给知识库加一道闸
--------------------------
知识库是这个系统里**唯一会放大错误的资产**。Agent 答错一次只影响一个人，
知识库里错一条会影响**所有问到这个问题的候选人**，而且会被引用机制包装成
"有依据"——错得更有说服力。

改动前的状态：文档导入（`doc_import`）整篇分块直接落库、工单回流勾选即生效，
全程没有任何人在模型之外确认过这句话对不对。

审核口径（与状态流转同源的设计原则：不阻断、只留痕 + 关键处设闸）
------------------------------------------------------------------
- **机器批量来源**（`doc_import` 文档分块）→ `draft`，**必须审**。
  一篇文档切出几十条，没人逐条看过，这是风险最高的来源。
- **工单回流**（`ticket#id`）→ `draft`。HR 写的是**这一张单**的答案，
  未必适合泛化成对所有人通用的政策表述（可能夹带"你这个情况特殊"之类的上下文）。
- **手工新增**（HR 在知识库页逐条录入）→ `published`。
  人逐字写的、页面上能看到、也能立刻删，不需要再拦一道。

**草稿对外不可见**：检索、固定管线、向量同步、意图兜底全部只取 published。
草稿只在管理端可见。

`review_status` 用 COALESCE 兜底，是为了兼容加列前的存量数据——
存量条目没有这一列时按 published 处理，不会因为加列就全线下线知识库。
"""
from datetime import datetime

from .store import get_conn

PUBLISHED = "published"
DRAFT = "draft"
REJECTED = "rejected"

# 统一的"对检索可见"条件。**任何新增的知识库读取点都必须用它**，
# 少一处就等于给草稿开了一道后门——这是本模块存在的唯一理由。
PUBLISHED_SQL = "active=1 AND COALESCE(review_status,'published')='published'"


def now():
    return datetime.now().isoformat(timespec="seconds")


def ensure_columns():
    """加列（幂等）。存量数据不带该列时 COALESCE 兜底为 published，无需回填。"""
    with get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(faqs)")}
        if "review_status" not in cols:
            conn.execute(f"ALTER TABLE faqs ADD COLUMN review_status TEXT DEFAULT '{PUBLISHED}'")
        if "reviewer" not in cols:
            conn.execute("ALTER TABLE faqs ADD COLUMN reviewer TEXT DEFAULT ''")
        if "reviewed_at" not in cols:
            conn.execute("ALTER TABLE faqs ADD COLUMN reviewed_at TEXT DEFAULT ''")
        conn.commit()


def review(fid: int, action: str, actor: str = "") -> dict:
    """审核一条：publish（通过）/ reject（拒绝）/ 退回草稿。

    拒绝不是删除——留着给人看"这条为什么没过"，也避免 HR 反复导入同一份文档
    又反复看到同一批草稿。想彻底清掉走 `DELETE /api/admin/faqs/{id}`。
    """
    if action not in ("publish", "reject", "draft"):
        return {"ok": False, "error": "未知审核动作"}
    target = {"publish": PUBLISHED, "reject": REJECTED, "draft": DRAFT}[action]
    with get_conn() as conn:
        row = conn.execute("SELECT id, question FROM faqs WHERE id=?", (fid,)).fetchone()
        if not row:
            return {"ok": False, "error": "条目不存在"}
        conn.execute("UPDATE faqs SET review_status=?, reviewer=?, reviewed_at=? WHERE id=?",
                     (target, actor or "", now(), fid))
        conn.commit()
    return {"ok": True, "id": fid, "review_status": target, "question": row["question"]}


def pending(limit: int = 100):
    """待审核列表（草稿）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, question, answer, category, source, created_at FROM faqs"
            f" WHERE active=1 AND COALESCE(review_status,'published')=? ORDER BY id DESC LIMIT ?",
            (DRAFT, min(max(limit, 1), 500))).fetchall()
        return [dict(r) for r in rows]


def count_by_review():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT COALESCE(review_status,'published') s, COUNT(*) c FROM faqs"
            " WHERE active=1 GROUP BY s").fetchall()
        return {r["s"]: r["c"] for r in rows}
