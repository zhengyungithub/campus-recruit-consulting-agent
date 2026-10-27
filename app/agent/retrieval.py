"""检索层：Qdrant 本地模式（dense）+ 关键词检索（恒可用），混合融合；无 Key 时自动降级"""
import hashlib
import json
import re
import threading

from . import config, kb
from .store import get_conn

_lock = threading.Lock()
_qlock = threading.Lock()
_client = None
_QVEC_CACHE = {}      # md5(question) -> 问题向量（查重用，避免每次全量重算）

# --- 降级观测（S4.5 门禁用） ---
# 为什么需要：向量检索拿不到时 search() 会静默降级成纯关键词。
# 线上这是对的（模型/向量挂了也要能回答），但在**评测门禁**里是致命的——
# 实测 uvicorn 运行时 Qdrant 文件锁打不开，fixed 模式照样跑出 30/30，
# 而它测的其实是纯关键词路径。门禁拿到这种"假绿灯"比红灯更危险。
# 于是把降级次数显式计数，门禁据此判定"本轮结果不可信"。
_STATS = {"search_calls": 0, "dense_ok": 0, "dense_fallback": 0, "client_error": 0}


def stats(reset: bool = False) -> dict:
    """返回（可选重置）降级观测计数。

    client_error > 0 表示向量库压根没打开（多半被别的进程占着锁）；
    dense_fallback > 0 表示库开了但这次检索的 embedding 请求挂了。
    """
    if reset:
        _STATS.update({k: 0 for k in _STATS})
    return dict(_STATS)
try:
    import jieba
    _STOP = {"的", "了", "吗", "呢", "吧", "啊", "是", "有", "个", "我", "你", "他",
             "能", "可以", "怎么", "怎样", "什么", "请问", "想", "要", "在", "和", "与"}

    def _tokens(s: str):
        return {w for w in jieba.cut(s) if w.strip() and w not in _STOP
                and not re.fullmatch(r"[\W\d]", w)}
except ImportError:
    _tokens = None


def _bigrams(s: str):
    s = re.sub(r"[\s?？!！。，,.的嘛呢吗吧]", "", s.lower())
    return set(s[i:i + 2] for i in range(len(s) - 1)) or ({s} if s else set())


def text_similarity(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    score = 2 * len(A & B) / (len(A) + len(B)) if A and B else 0.0
    if a in b or b in a:
        score = max(score, 0.85)
    if _tokens:
        ta, tb = _tokens(a), _tokens(b)
        if ta:
            matched, weight = 0, 0.0
            for t in ta:
                if t in tb:
                    matched += 1; weight += 1.0
                elif any((t in s or s in t) for s in tb
                         if len(min((s, t), key=len)) > 1):
                    matched += 1; weight += 0.5
            if matched >= 2:
                score = max(score, weight / len(ta))
    return score


def _active_faqs():
    """对检索可见的知识库条目。S6.1：草稿（未审核）不进检索——
    一条未经确认的知识会放大到所有问到它的候选人，宁可先不答。"""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT id, question, answer, category FROM faqs WHERE {kb.PUBLISHED_SQL}").fetchall()


def _keyword_search(query: str, k: int = 4):
    scored = [(text_similarity(query, f["question"]), f) for f in _active_faqs()]
    scored.sort(key=lambda x: -x[0])
    return [{"id": f["id"], "question": f["question"], "answer": f["answer"],
             "category": f["category"], "score": round(s, 4), "source": "keyword"}
            for s, f in scored[:k] if s >= 0.15]


def question_vectors(questions):
    """批量取问题向量；按 md5 缓存，内容不变不重复请求。

    查重时库里有多少条就得 embed 多少条，不缓存的话每次沉淀都要全量重算。
    """
    from .llm_service import get_embeddings
    todo, out = [], {}
    with _qlock:
        for q in questions:
            key = hashlib.md5((q or "").encode()).hexdigest()
            if key in _QVEC_CACHE:
                out[q] = _QVEC_CACHE[key]
            else:
                todo.append((q, key))
    if todo:
        emb = get_embeddings()
        texts = [t[0] for t in todo]
        vecs = []
        for i in range(0, len(texts), 10):          # DashScope 单批上限 10 条
            vecs.extend(emb.embed_documents(texts[i:i + 10]))
        with _qlock:
            for (q, key), v in zip(todo, vecs):
                _QVEC_CACHE[key] = v
                out[q] = v
    return out


def similar_faqs(query: str, limit: int = 5):
    """查重专用检索：问题对问题的语义相似度 + 关键词相似度，取较大值。

    **为什么不直接查 kb_entries**：
    那里面存的是「问题 + 答案」的向量。实测「面试要准备什么资料？」
    对「面试需要带哪些材料？+ 答案」只有 0.78，而对纯问题向量是 0.95——
    答案混在向量里会把问题之间的语义稀释掉。查重问的是「这两条问题是不是一回事」，
    自然该比问题。

    **为什么不走 Qdrant**：kb_entries 只增不删，FAQ 删了向量还在
    （实测 17 条 FAQ 对应 52 个点），查重会撞上早已删除的幽灵条目。
    这里直接读 faqs 表，结果永远和数据库一致。代价是每次要 embed 一遍问题
    （有缓存，且沉淀是低频人工操作，可接受）。

    dense 不可用（无 Key / 服务异常）时自动退化为纯关键词：查重变宽松，但绝不失败。
    """
    q = (query or "").strip()
    if not q:
        return []
    faqs = _active_faqs()
    if not faqs:
        return []

    merged = {}
    try:
        import numpy as np
        qv = np.asarray(question_vectors([q])[q], dtype="float32")
        qn = float(np.linalg.norm(qv)) or 1.0
        vecs = question_vectors([f["question"] for f in faqs])
        for f in faqs:
            v = vecs.get(f["question"])
            if v is None:
                continue
            fv = np.asarray(v, dtype="float32")
            fn = float(np.linalg.norm(fv)) or 1.0
            merged[f["id"]] = {"id": f["id"], "question": f["question"],
                                "answer": f["answer"], "category": f["category"],
                                "sim_dense": round(float(qv.dot(fv) / (qn * fn)), 4),
                                "sim_kw": 0.0, "via": "dense"}
    except Exception:
        pass                                         # dense 失败静默降级

    for f in faqs:
        s = round(text_similarity(q, f["question"]), 4)
        cur = merged.get(f["id"])
        if cur is None:
            merged[f["id"]] = {"id": f["id"], "question": f["question"],
                               "answer": f["answer"], "category": f["category"],
                               "sim_dense": 0.0, "sim_kw": s, "via": "keyword"}
        else:
            cur["sim_kw"] = s
            if s > cur["sim_dense"]:
                cur["via"] = "keyword"

    for r in merged.values():
        r["sim"] = round(max(r["sim_dense"], r["sim_kw"]), 4)
    return [r for r in sorted(merged.values(), key=lambda r: -r["sim"])[:limit]
            if r["sim"] >= 0.15]


def _client_or_none():
    global _client
    if not config.llm_ready():
        return None
    if _client is None:
        with _lock:
            if _client is None:
                try:
                    from qdrant_client import QdrantClient
                    from qdrant_client.models import Distance, VectorParams
                    c = QdrantClient(path=str(config.QDRANT_PATH))
                    for coll, dim in (("kb_entries", config.EMBED_DIM),
                                      ("chat_history", config.EMBED_DIM)):
                        if not c.collection_exists(coll):
                            c.create_collection(
                                coll, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
                    _client = c
                except Exception:
                    # 最常见成因：Qdrant 本地模式是**文件锁**，另一个进程（uvicorn）已独占。
                    # 这里必须吞掉并返回 None——所有调用方本来就判了 `c is None`，
                    # 抛出去只会让整条问答链路 500。降级次数记进 stats，
                    # 交给门禁判断"本轮结果可不可信"。
                    _STATS["client_error"] += 1
                    return None
    return _client


def rebuild_index():
    """Embedding 模型/Key 变更后全量重建：删集合（下次按新配置自动重建）并重灌 FAQ。
    注意：chat_history 中的对话向量记忆会被清空（POC 可接受）。"""
    c = _client_or_none()
    if c is None:
        return False
    global _client
    for coll in ("kb_entries", "chat_history"):
        try:
            c.delete_collection(coll)
        except Exception:
            pass
    _client = None          # 下次访问按新配置重建集合
    try:
        sync_faqs()
        return True
    except Exception:
        return False


def sync_faqs():
    """把 faqs 表同步进 Qdrant（内容 hash 变化才重新 embedding）；无 Key 时跳过。

    同步是**双向**的：除了新增/变更，还要删掉数据库里已经没有的条目。
    原实现只 upsert 不 delete，于是 FAQ 删掉后向量还留在集合里——
    实测 20 条 FAQ 对 52 个点——检索会命中早已删除的幽灵条目，查重更是直接撞上它。
    """
    c = _client_or_none()
    if c is None:
        return 0
    from qdrant_client.models import PointStruct
    with get_conn() as conn:
        faqs = conn.execute(
            f"SELECT id, question, answer, category FROM faqs WHERE {kb.PUBLISHED_SQL}").fetchall()
    valid_ids = {f["id"] for f in faqs}
    try:
        known, _ = c.scroll("kb_entries", limit=100000, with_payload=False)
        stale = [p.id for p in known if p.id not in valid_ids]
        if stale:
            c.delete("kb_entries", points_selector=stale)
    except Exception:
        pass                                          # 清理失败不阻断主流程
    from .llm_service import get_embeddings
    texts, todo = [], []
    for f in faqs:
        text = f["question"] + "\n" + f["answer"]
        h = hashlib.md5(text.encode()).hexdigest()
        try:
            existed = c.retrieve("kb_entries", ids=[f["id"]], with_payload=True)
        except Exception:
            existed = []
        if not existed or (existed[0].payload or {}).get("hash") != h:
            todo.append((f["id"], text, h, f["question"], f["answer"], f["category"]))
    if not todo:
        return 0
    # DashScope embedding 单批上限 10 条，分批请求
    texts = [t[1] for t in todo]
    vecs = []
    emb = get_embeddings()
    for i in range(0, len(texts), 10):
        vecs.extend(emb.embed_documents(texts[i:i + 10]))
    c.upsert("kb_entries", [PointStruct(
        id=rid, vector=vec,
        payload={"hash": h, "question": q, "answer": a, "category": cat})
        for (rid, _t, h, q, a, cat), vec in zip(todo, vecs)])
    return len(todo)


def search(query: str, k: int = 4):
    """混合检索：dense + keyword 融合（dense 缺席时纯关键词，保证永不失败）"""
    _STATS["search_calls"] += 1
    results = {r["id"]: {**r, "hybrid": r["score"]} for r in _keyword_search(query, k)}
    c = _client_or_none()
    if c is not None and c.count("kb_entries", exact=True).count > 0:
        try:
            from .llm_service import get_embeddings
            vec = get_embeddings().embed_query(query)
            hits = c.query_points("kb_entries", query=vec, limit=k).points
            for h in hits:
                p = h.payload or {}
                # ⚠️ 原实现写的是 `1.0 - h.score`，注释称"qdrant 返回的是距离"——**这是错的**。
                # 实测完全一致文本的 score = 1.0000，说明 COSINE 下 qdrant 返回的就是余弦相似度。
                # 那个换算把相似度反向成了距离，导致 hybrid 融合里越相似的条目得分越低，
                # 排序直接反了（差匹配会排在完美匹配前面）。2026-10-26 修正。
                dense = float(h.score or 0.0)
                if h.id in results:
                    results[h.id]["hybrid"] = round(0.6 * dense + 0.4 * results[h.id]["score"], 4)
                    results[h.id]["source"] = "hybrid"
                else:
                    results[h.id] = {"id": h.id, "question": p.get("question", ""),
                                     "answer": p.get("answer", ""), "category": p.get("category", ""),
                                     "score": round(dense, 4), "hybrid": round(dense, 4),
                                     "source": "dense"}
            _STATS["dense_ok"] += 1
        except Exception:
            _STATS["dense_fallback"] += 1            # 不再无声无息：门禁要看得到
    out = sorted(results.values(), key=lambda r: -r["hybrid"])[:k]
    return [r for r in out if r["hybrid"] >= 0.15]


def store_chat_memory(cid: int, question: str, answer: str):
    c = _client_or_none()
    if c is None:
        return
    try:
        from qdrant_client.models import PointStruct
        from .llm_service import get_embeddings
        import time
        pid = int(time.time() * 1000) % (1 << 62)
        vec = get_embeddings().embed_query(question + "\n" + answer[:500])
        c.upsert("chat_history", [PointStruct(
            id=pid, vector=vec,
            payload={"candidate_id": cid, "question": question, "answer": answer[:500]})])
    except Exception:
        pass


def recall_history(cid: int, query: str, k: int = 2):
    c = _client_or_none()
    if c is None:
        return []
    try:
        from .llm_service import get_embeddings
        vec = get_embeddings().embed_query(query)
        hits = c.query_points("chat_history", query=vec, limit=k * 3).points
        out = [{"question": h.payload.get("question"), "answer": h.payload.get("answer")}
               for h in hits if (h.payload or {}).get("candidate_id") == cid][:k]
        return out
    except Exception:
        return []


def purge_chat_memory(cid: int) -> int:
    """删除某个候选人的全部对话向量记忆（评测/清数据用）。成功返回删除条数。"""
    c = _client_or_none()
    if c is None:
        return 0
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        flt = Filter(must=[FieldCondition(key="candidate_id", match=MatchValue(value=cid))])
        pts, _ = c.scroll("chat_history", scroll_filter=flt, limit=1000)
        ids = [p.id for p in pts]
        if ids:
            c.delete("chat_history", points_selector=ids)
        return len(ids)
    except Exception:
        return 0


def status():
    c = _client_or_none()
    if c is None:
        return {"dense": False, "kb_points": 0, "chat_points": 0}
    try:
        return {"dense": True,
                "kb_points": c.count("kb_entries", exact=True).count,
                "chat_points": c.count("chat_history", exact=True).count}
    except Exception:
        return {"dense": False, "kb_points": 0, "chat_points": 0}
