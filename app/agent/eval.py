"""评测回归：用标注集量化 Agent 能力，作为所有改动的门禁。

为什么需要它：
    没有评测集时，改 prompt / 换模型 / 调意图规则只能靠"问两句看看"，
    既无法判断是否变好，也无法发现某类意图被改坏。本模块把主观感受变成可对比的数字。

跑什么（三种力度，成本从低到高）：
    intent  只跑意图识别（规则/知识库/LLM 三层），最便宜，适合每次改规则后秒级验证
    fixed   固定管线整条链路（意图 → 分派 → 检索/查库 → 回答），不建工单、不写业务数据
    agent   完整 LangGraph（intent → agent → act → verify），会真实调工具，用独立评测账号跑完清理

看什么指标：
    意图准确率 / 关键事实命中率 / 引用率 / 转人工判定准确率 / 核验通过率 / P95 延迟
"""
import json
import os
import time
from pathlib import Path
from typing import Optional

from concurrent.futures import TimeoutError as FuturesTimeout

from . import intent as intent_mod
from . import verifier
from .store import get_conn

CASES_PATH = Path(__file__).resolve().parent.parent / "evals" / "cases.jsonl"

# 转人工类意图：用于判定"是否应该建工单"
HANDOFF_INTENTS = {"human_handoff", "out_of_scope"}

# 评测账号：agent 模式会真实建工单，用独立账号跑，跑完清理，不污染业务数据
EVAL_PHONE = "13800000000"
EVAL_NAME = "评测专用账号"


def load_cases(path: Optional[str] = None) -> list:
    p = Path(path or CASES_PATH)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _ensure_eval_candidate() -> int:
    """确保存在评测专用候选人，返回其 id"""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM candidates WHERE phone=?", (EVAL_PHONE,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO candidates(name, phone, school, major, position, site, status, created_at)"
            " VALUES(?,?,'—','—','—','—','已初筛通过',datetime('now','localtime'))",
            (EVAL_NAME, EVAL_PHONE))
        conn.commit()
        return cur.lastrowid


def _purge_eval_candidate(cid: int):
    """清理评测账号产生的工单与消息。

    刻意**不清 Qdrant 向量记忆**：它是本地库且带文件锁，每条用例都去 scroll/delete
    会造成严重的锁竞争（第3期实测单条被拖到 22 分钟）。向量记忆只在整轮结束后统一清，
    单轮用例本来就不 recall 历史，不会串味（真正需要干净上下文的多轮用例单独清）。
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM tickets WHERE candidate_id=?", (cid,))
        conn.execute("DELETE FROM messages WHERE candidate_id=?", (cid,))
        conn.commit()


# 向量记忆清理**不提供本进程版本**（原 `_purge_eval_vectors` 已删）：
# Qdrant 本地模式是文件锁，父进程一旦取过 client 就会一直占着，
# 之后每条用例的子进程都打不开库（第3期被它坑到 22 分钟/条）。
# 统一由 agent/eval_worker.py 在子进程内执行，见 run_agent_guarded(purge_vectors=...)。


def _record_message(cid: int, role: str, content: str):
    """模拟 service 层落库：Agent 自身不写 messages，历史时序由调用方负责"""
    from datetime import datetime
    with get_conn() as conn:
        conn.execute("INSERT INTO messages(candidate_id,role,content,created_at) VALUES(?,?,?,?)",
                     (cid, role, content, datetime.now().isoformat(timespec="seconds")))
        conn.commit()


def _seed_history(cid: int, prev: dict):
    """第3期：给多轮用例铺垫上一轮对话（要测"追问不答非所问"，先得有上下文）"""
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        if prev.get("user"):
            conn.execute("INSERT INTO messages(candidate_id,role,content,created_at) VALUES(?,?,?,?)",
                         (cid, "user", prev["user"], now))
        if prev.get("bot"):
            conn.execute("INSERT INTO messages(candidate_id,role,content,created_at) VALUES(?,?,?,?)",
                         (cid, "bot", prev["bot"], now))
        conn.commit()


def _self_ids(cid: int) -> list:
    """本人标识白名单：候选人的手机号/姓名用于隐私红线豁免（复述自己的信息不算泄露）"""
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT name, phone FROM candidates WHERE id=?", (cid,)).fetchone()
        if row:
            return [v for v in (row["name"], row["phone"]) if v]
    except Exception:
        pass
    return []


def run_agent_guarded(cid: int, q: str, case_id, timeout: int = 120,
                      use_history: bool = False, purge_vectors: bool = False,
                      mode: str = "agent") -> dict:
    """给单条用例加**真**超时护栏：卡住的用例被 kill，而不是拖垮整轮评测。

    为什么必须是子进程而不是线程（2026-10-26 改）：
        第1期用 threading + join(timeout) 做护栏，那是**假护栏**——Python 线程
        杀不掉。卡在 socket 读（模型/向量请求静默挂起）时，join 超时后线程仍在，
        主进程只能继续等。第3期实测单条卡 1417s，而护栏是 120s，
        证明卡点根本不在护栏覆盖的节点边界上。子进程可被 kill，护栏才成立。

    为什么用**常驻进程池**而不是每条用例起一个新进程：
        langchain / langgraph / qdrant 的 import 冷启动实测 13~22s，
        42 条用例光启动就要 10 分钟，比跑本身还慢。用 ProcessPoolExecutor 复用
        同一个 worker，只付一次启动成本，之后每条 ~3s。

    mode=fixed / purge 同样走子进程：管线里也有 Embedding 请求，一样会挂；
    purge 走子进程是为了让 Qdrant 锁的持有者始终是 worker 而不是评测主进程。
    """
    payload = {"cid": cid, "q": q, "mode": mode, "use_history": use_history,
               "purge_vectors": purge_vectors,
               "debug": bool(os.getenv("EVAL_DEBUG")),
               "thread_id": f"eval-{case_id}-{int(time.time() * 1000)}"}

    # EVAL_INLINE=1：跳过子进程，直接在当前进程跑（仅用于排查——
    # 定位"问题来自被测代码还是进程隔离"时，用它做 A/B 对比。默认不要开，
    # 开了就失去了超时可 kill 的兜底）
    if os.getenv("EVAL_INLINE") == "1":
        try:
            return _absorb_retrieval(_worker_run(payload))
        except Exception as e:
            return {"answer": "", "error": repr(e)}

    try:
        ex = _get_executor()
        fut = ex.submit(_worker_run, payload)
        r = fut.result(timeout=timeout)
    except FuturesTimeout:
        # 关键：future 超时后线程还在跑，必须连进程一起杀掉，否则评测会一直挂着
        _kill_executor()
        return {"answer": "", "error": f"超时（>{timeout}s）已终止 worker 进程"}
    except Exception as e:
        # worker 进程崩溃（BrokenProcessPool）等，重建一次再试，仍失败才算失败
        _kill_executor()
        return {"answer": "", "error": f"worker 执行失败：{e!r}"}
    if not isinstance(r, dict):
        return {"answer": "", "error": f"worker 返回类型异常：{type(r).__name__}"}
    if r.get("error"):
        return {"answer": "", "error": r["error"]}
    return _absorb_retrieval(r) or {"answer": "", "error": "无返回"}


# ---- 降级观测汇总（S4.5 门禁用）----
#
# 为什么需要：向量检索拿不到时 search() 会静默降级成纯关键词，用例照样能过——
# 于是门禁会拿到一份"全绿但根本没测到向量检索"的结果。
# 实测：uvicorn 运行时 Qdrant 文件锁打不开，fixed 模式仍跑出 30/30。
# 这种**假绿灯比红灯危险**，所以把降级次数显式汇总进 summary，门禁据此判定可信度。
#
# intent / fixed 内联跑在父进程（读 retrieval.stats()）；
# agent 跑在常驻 worker 子进程（统计随结果回传，见 eval_worker）。两侧相加才是全貌。
_RETR_ACC: dict = {}


def _absorb_retrieval(r: dict) -> dict:
    """把 worker 回报的降级计数并入累加器（原样返回结果 dict）。"""
    if isinstance(r, dict) and isinstance(r.get("_retrieval"), dict):
        for k, v in r["_retrieval"].items():
            _RETR_ACC[k] = _RETR_ACC.get(k, 0) + int(v or 0)
    return r


def _retrieval_summary(reset: bool = False) -> dict:
    out = dict(_RETR_ACC)
    if reset:
        _RETR_ACC.clear()
    try:
        from . import retrieval
        for k, v in retrieval.stats(reset=reset).items():
            out[k] = out.get(k, 0) + int(v or 0)
    except Exception:
        pass
    return out


# ---- 常驻 worker 进程池（懒创建 / 超时可杀）----
_EXECUTOR = None


def _worker_run(payload: dict) -> dict:
    from .eval_worker import run_payload
    return run_payload(payload)


def _get_executor():
    global _EXECUTOR
    if _EXECUTOR is None:
        import multiprocessing as _mp
        from concurrent.futures import ProcessPoolExecutor
        # Windows 无 fork，必须显式 spawn：子进程重新 import，父进程的 sqlite/
        # qdrant 状态不会被继承（这正是我们要的隔离）
        _EXECUTOR = ProcessPoolExecutor(max_workers=1, mp_context=_mp.get_context("spawn"))
    return _EXECUTOR


def _kill_executor():
    """杀掉常驻 worker（超时 / broken 时用），下次用例会懒重建。"""
    global _EXECUTOR
    ex, _EXECUTOR = _EXECUTOR, None
    if ex is None:
        return
    import os
    import signal
    for p in list(getattr(ex, "_processes", {}).values() or []):
        try:
            os.kill(p.pid, signal.SIGTERM)   # Windows 下等价于 TerminateProcess
        except Exception:
            pass
    try:
        ex.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


def shutdown_workers():
    """正常收尾：关掉常驻 worker（评测跑完 / 进程退出前调用）。"""
    _kill_executor()


def _run_one(case: dict, mode: str, cid: int) -> dict:
    """跑单条用例，返回逐条明细（含通过/失败原因）"""
    q = case["q"]
    t0 = time.time()
    rec = {"id": case.get("id"), "q": q, "expect_intent": case.get("intent"), "mode": mode}

    it = intent_mod.classify(q, cid)
    rec["actual_intent"] = it["intent"]
    rec["intent_source"] = it.get("source")
    rec["intent_confidence"] = it.get("confidence")
    rec["intent_ok"] = it["intent"] == case.get("intent")

    answer, cites, ticket_id, called_tools = "", [], None, []
    if mode == "fixed":
        from .pipeline import fixed_chat_reply
        # allow_ticket=False：评测不产生真实工单（转人工与否用意图判定校验）
        answer, ticket_id, cites = fixed_chat_reply(cid, q, allow_ticket=False)
    elif mode == "agent":
        # 注意：**不要**在这里 import graph——父进程一旦加载 langchain 链路，
        # 后面每条用例的子进程就要各付一次 15s 冷启动。图只在 worker 子进程里跑。
        from . import handling
        # 复刻线上时序：service.sse_chat 会先记录用户本轮消息，再让 Agent 跑。
        # 多轮用例依赖这一点才取得到"上一轮问了什么"（last_user_question 取的是倒数第二条）。
        prev = case.get("prev")
        seeded_ticket = None
        _purge_eval_candidate(cid)          # 每条用例从干净状态起步，杜绝跨用例串味
        # 注意：向量记忆的清理**不能在父进程做**——
        # Qdrant 本地模式是文件锁，父进程一旦取过 client 就会一直占着，
        # 后面每条用例的子进程都打不开库（第3期被这个锁坑过 22 分钟/条）。
        # 统一交给 worker 在子进程内清（purge_vectors）。
        if prev:
            # 历史播种：把上一轮问答写进 messages，coref 指代还原与 repeat 重复识别
            # 都靠它取"上一轮问了什么"。删了它多轮用例会全军覆没——
            # 2026-10-26 清理死代码 `_purge_eval_vectors` 时曾误删此行，
            # 表现为 coref_hits=0 / repeat_hits=0 且所有 prev 用例判负（见 Handoff §4.4）。
            _seed_history(cid, prev)
        # S4.2：播种"办理中"会话（上一轮已建单、槽位待补）必须放在 _purge_eval_candidate **之后**——
        # 否则刚建的工单会被整轮清理一起删掉（实测出现过 note 为空、新单计数为 -1）
        if case.get("session"):
            from .store import get_conn
            from datetime import datetime as _dt
            with get_conn() as conn:
                cur = conn.execute(
                    "INSERT INTO tickets(candidate_id,question,context,created_at) VALUES(?,?,?,?)",
                    (cid, case["session"].get("ticket_question", "（上一轮办理请求）"), "[]",
                     _dt.now().isoformat(timespec="seconds")))
                seeded_ticket = cur.lastrowid
            handling.open_session(cid, case["session"]["kind"], seeded_ticket)
        _record_message(cid, "user", q)
        use_hist = bool(prev)               # 单轮用例仍完全隔离（第1期踩过历史污染）
        try:
            r = run_agent_guarded(cid, q, case.get("id"), timeout=120,
                                  use_history=use_hist, purge_vectors=bool(prev),
                                  mode="agent")
        except Exception as e:
            r = {"answer": "", "error": repr(e)}
        if r.get("error"):
            rec["error"] = r["error"]
        answer, cites = r.get("answer", ""), r.get("citations", [])
        rec["verify"] = r.get("verify", {}).get("status")
        # 第1期新增观测：实际工具序列 / 计划来源 / 是否降级 / 失败类型（只在 agent 模式有值）
        called_tools = [t["tool"] for t in (r.get("tool_trace") or [])]
        rec["tools"] = called_tools
        # 第3期：指代还原后的意图纠偏发生在图里，评测必须以图最终采用的意图为准，
        # 否则会出现"图里走对了、指标却按旧口径判错"的假失败（#34/#35 就是这样）
        actual = r.get("intent")
        if actual:
            rec["actual_intent"] = actual
            rec["intent_ok"] = actual == case.get("intent")
        rec["plan"] = {"source": (r.get("plan") or {}).get("source"),
                       "steps": [s["tool"] for s in (r.get("plan") or {}).get("steps", [])]}
        rec["degraded"] = bool(r.get("degraded"))
        rec["failures"] = [f.get("kind") for f in (r.get("failures") or [])]
        # 模型不可用导致的降级不是 Agent 的能力问题，单独计量，避免污染通过率归因
        rec["llm_down"] = bool(r.get("degraded")) and "llm_error" in rec["failures"]
        rec["unsupported"] = (r.get("verify") or {}).get("unsupported") or []
        # 第2期：图中已做完整双通道核验，红线/ Judge 结论直接取图的结果
        rec["redlines"] = [x["code"] for x in ((r.get("verify") or {}).get("redlines") or [])]
        rec["judge"] = (r.get("verify") or {}).get("judge", {}).get("verdict")
        rec["ungrounded"] = ((r.get("verify") or {}).get("ungrounded") or [])[:3]
        # 第3期观测：指代还原是否命中、是否被识别为重复提问
        rec["resolved"] = r.get("resolved_question") if r.get("resolved_question") != q else None
        rec["referent"] = r.get("referent") or None
        rec["repeat"] = bool(r.get("repeat"))
    rec["answer"] = (answer or "")[:300]
    rec["citations"] = len(cites or [])

    # 关键事实命中 / 引用率：只有真正生成了回答的模式下才校验
    # （intent 模式不跑回答链路，不能用它们判定失败，否则通过率会被严重低估）
    must, miss, bad = [], [], []
    if mode != "intent":
        must = expand_must(case.get("must"))
        # 同义组内任一命中即算过：断言的是"这个信息有没有传达到"，不是"模型用了哪个词"
        miss = ["/".join(g) for g in must if not any(k in (answer or "") for k in g)]
        rec["must_hit"] = (len(must) - len(miss)) / len(must) if must else None
        rec["must_miss"] = miss
        bad = [k for k in (case.get("must_not") or []) if k in (answer or "")]
        rec["must_not_violated"] = bad
        rec["cite_ok"] = bool(cites) if case.get("cite") else None
    else:
        rec["must_hit"] = None
        rec["must_miss"] = []
        rec["must_not_violated"] = []
        rec["cite_ok"] = None

    # 第1期：工具序列校验（只有 agent 模式真的调了工具，其余模式不适用）
    need_tools = case.get("tools") or []
    missing_tools = [t for t in need_tools if t not in called_tools] if mode == "agent" else []
    rec["tool_ok"] = (not missing_tools) if (need_tools and mode == "agent") else None
    # S4.4 观测：本条是否触发过"规则补执行"（模型没自己调必做工具）。
    # 这是"执行确定性"的直接度量——数值越高说明越多回答是靠框架兜住才拿到依据的。
    rec["forced_steps"] = (r.get("forced_steps") or 0) if mode == "agent" else 0

    # 转人工判定：意图是否落在人工/超范围
    rec["ticket_ok"] = (it["intent"] in HANDOFF_INTENTS) == bool(case.get("ticket"))

    # S4.2：多轮办理用例——补充的信息必须回填到"上一轮那张工单"，且不能另开新单
    if mode == "agent" and seeded_ticket:
        from .store import get_conn as _gc
        with _gc() as conn:
            row = conn.execute("SELECT context FROM tickets WHERE id=?", (seeded_ticket,)).fetchone()
            note = (row["context"] if row else "") or ""
            total = conn.execute("SELECT COUNT(*) FROM tickets WHERE candidate_id=?", (cid,)).fetchone()[0]
            conn.execute("DELETE FROM tickets WHERE id=?", (seeded_ticket,))
            conn.execute("DELETE FROM tickets WHERE candidate_id=? AND id!=?", (cid, seeded_ticket))
            conn.execute("DELETE FROM handling_sessions WHERE candidate_id=?", (cid,))
        rec["handling_note"] = note[:120]
        want = case.get("expect_slot") or ""
        # 没写 expect_slot 的用例只校验"有没有重复建单"，不做回填断言
        rec["handling_ok"] = (want in note) if want else None
        rec["new_tickets"] = total - 1      # 除播种那张之外又建了几张（>0 就是重复建单）

    # 固定管线的双通道核验（agent 模式在图中已校验）
    if mode == "fixed" and cites:
        v = verifier.verify_answer(answer, cites, question=q)
        rec["verify"] = v["status"]
        rec["verify_ratio"] = v["ratio"]
        rec["redlines"] = [x["code"] for x in v.get("redlines", [])]
        rec["judge"] = v.get("judge", {}).get("verdict")
    # 红线扫描是纯规则、零成本：无论有没有引用都跑，作为全模式的强制门禁
    if mode != "intent" and not rec.get("redlines"):
        rl = verifier.scan_redlines(answer or "", allow_values=_self_ids(cid),
                                    contexts=[c.get("answer", "") for c in (cites or [])])
        rec["redlines"] = [x["code"] for x in rl]

    rec["latency_ms"] = int((time.time() - t0) * 1000)

    reasons = []
    if not rec["intent_ok"]:
        reasons.append(f"意图期望 {rec['expect_intent']} 实际 {rec['actual_intent']}")
    if miss:
        reasons.append("缺关键事实：" + "、".join(miss))
    if bad:
        reasons.append("出现禁用表述：" + "、".join(bad))
    if rec["cite_ok"] is False:
        reasons.append("应引用知识库但无引用")
    if missing_tools:
        reasons.append("工具序列缺失：" + "、".join(missing_tools))
    if rec["ticket_ok"] is False:
        reasons.append("转人工判定不符")
    # 第3期：多轮用例必须真的把指代还原出来（否则后续用例就不能证明能力确实生效）
    if case.get("expect_resolved") and not rec.get("referent"):
        reasons.append("未做指代还原")
    if "expect_repeat" in case and bool(rec.get("repeat")) != bool(case["expect_repeat"]):
        reasons.append("重复提问识别不符")
    if rec.get("verify") in ("fail", "block"):
        reasons.append("答案核验未通过（" + rec["verify"] + "）")
    if rec.get("redlines"):
        leak = [c for c in rec["redlines"] if c == "internal_leak"]
        others = [c for c in rec["redlines"] if c != "internal_leak"]
        bits = []
        if others:
            bits.append("触发合规红线：" + "、".join(others))
        if leak:
            # 单独标注：这类失败是模型把工具名/内部 JSON 回显出来，属模型行为，不是检索回归
            bits.append("回显内部实现（internal_leak，模型行为问题）")
        reasons.append("；".join(bits))
    # S4.2：槽位没回填 / 又开了新单，都算状态机没闭环
    if rec.get("handling_ok") is False:
        reasons.append("补充信息未回填工单（期望含：" + str(case.get("expect_slot")) + "）")
    if rec.get("new_tickets", 0) > 0:
        reasons.append(f"重复建单 {rec['new_tickets']} 张（应复用上一轮工单）")
    if rec.get("error"):
        reasons.append("执行异常：" + rec["error"][:120])
    rec["passed"] = not reasons
    rec["reasons"] = reasons
    return rec


def expand_must(items) -> list:
    """把 must 标注统一成「同义组列表」。

    背景（2026-10-26）：原 must 是精确字符串匹配，而模型是自由作答的——
    期望「重新上传」、模型说「重新提交」就被判缺关键事实。42 条用例连续 4 轮
    得 40/36/34/33，波动 ±4 条，主因就是这个：**把"措辞"当成了"事实"**。

    现在 must 支持两种写法，向后兼容：
        ["初筛", "测评"]                    → 单字词，等价于两个单元素组（保持原语义）
        [["重新上传","重新提交","再次上传"]] → 同义组，组内任一命中即算过
    约定：同义组的**第一个元素是原始断言**，新增的只是可接受的等价说法，
    保证放宽措辞的同时不漂移语义。
    """
    out = []
    for it in (items or []):
        if isinstance(it, (list, tuple)):
            g = [str(x) for x in it if x]
            if g:
                out.append(g)
        elif it:
            out.append([str(it)])
    return out


def _vote(case: dict, mode: str, cid: int, repeats: int, first: dict) -> dict:
    """多数投票：失败用例再跑几次，按多数裁决，并标记 flaky。

    为什么需要：单次通过率随模型采样波动（42 条实测 40/36/34/33，±4 条）。
    拿"这一次没答好"当回归门禁会误杀正确改动——第4期就差点把一次正确的
    检索修复判成回归。多数投票把口径从"这次答对没有"换成"它是不是稳定答不出来"，
    后者才是真回归。

    只对**失败用例**复跑：通过的用例再跑两遍纯属浪费（42 条 × 4s 的进程开销）。
    """
    recs = [first]
    for i in range(repeats - 1):
        r = _run_one(case, mode, cid)
        r["attempt"] = i + 2
        recs.append(r)
    votes = [bool(r["passed"]) for r in recs]
    need = repeats // 2 + 1
    ok = sum(votes) >= need
    # 代表记录：通过取第一条通过的（reasons 为空），失败取第一条（原因最全）
    pick = dict(next((r for r in recs if r["passed"]), recs[0])) if ok else dict(recs[0])
    pick["votes"] = votes
    pick["vote_passed"] = sum(votes)
    pick["attempts"] = repeats
    pick["flaky"] = len(set(votes)) > 1
    pick["first_passed"] = bool(first["passed"])
    if ok and not all(votes):
        pick["reasons"] = list(pick.get("reasons") or []) + \
            [f"多数通过（{sum(votes)}/{repeats}，其余轮次判负）"]
    return pick


def _pct(a: int, b: int) -> float:
    return round(a / b, 3) if b else 0.0


def run(mode: str = "intent", limit: int = 0, cid: Optional[int] = None,
        repeats: int = 1, only: Optional[list] = None) -> dict:
    """跑一轮评测，返回 {summary, details}

    repeats>1 时开启多数投票：只对失败用例复跑，按多数裁决（默认 3 次取 2）。
    口径说明见 `_vote`——这是把"单次通过率"升级成"稳定通过率"。
    only=[id,...]：只跑指定用例，排查单条问题时用（不改评测集、不留痕）。
    """
    cases = load_cases()
    # 用例可用 modes 限定只在部分模式下跑（如复合问句只在 agent 模式有意义）
    cases = [c for c in cases if not c.get("modes") or mode in c["modes"]]
    if only:
        cases = [c for c in cases if c.get("id") in only]
    if limit and limit > 0:
        cases = cases[:limit]
    if not cases:
        return {"error": "评测集为空：app/evals/cases.jsonl"}

    if mode == "agent":
        cid = cid or _ensure_eval_candidate()
    else:
        cid = cid or 1

    _retrieval_summary(reset=True)      # 清掉上一轮残留的降级计数
    details = []
    for c in cases:
        rec = _run_one(c, mode, cid)
        rec.setdefault("attempt", 1)
        if repeats > 1 and not rec["passed"]:
            rec = _vote(c, mode, cid, repeats, rec)
        details.append(rec)
    if mode == "agent":
        _purge_eval_candidate(cid)
        # 向量记忆交给子进程清：父进程一旦取过 Qdrant client 就会长期占着本地文件锁，
        # 之后每条用例的子进程都打不开库。
        try:
            run_agent_guarded(cid, "", "cleanup", timeout=60, mode="purge")
        except Exception:
            pass
        shutdown_workers()          # 释放常驻 worker，避免残留进程占着 Qdrant 锁

    n = len(details)
    must_vals = [d["must_hit"] for d in details if d["must_hit"] is not None]
    cite_vals = [d["cite_ok"] for d in details if d["cite_ok"] is not None]
    tool_vals = [d["tool_ok"] for d in details if d["tool_ok"] is not None]
    v_vals = [d.get("verify") for d in details if d.get("verify")]
    lat = sorted(d["latency_ms"] for d in details)

    per_intent = {}
    for d in details:
        k = d["expect_intent"]
        s = per_intent.setdefault(k, {"total": 0, "ok": 0})
        s["total"] += 1
        s["ok"] += 1 if d["intent_ok"] else 0

    summary = {
        "mode": mode, "total": n,
        "intent_acc": _pct(sum(1 for d in details if d["intent_ok"]), n),
        "must_rate": round(sum(must_vals) / len(must_vals), 3) if must_vals else None,
        "cite_rate": _pct(sum(1 for v in cite_vals if v), len(cite_vals)) if cite_vals else None,
        "tool_rate": _pct(sum(1 for v in tool_vals if v), len(tool_vals)) if tool_vals else None,
        "ticket_acc": _pct(sum(1 for d in details if d["ticket_ok"]), n),
        # 第1期观测：计划是否被 LLM 改写、有多少走到降级兜底
        "plan_llm": sum(1 for d in details if (d.get("plan") or {}).get("source") == "llm"),
        "degraded": sum(1 for d in details if d.get("degraded")),
        "llm_downgraded": sum(1 for d in details if d.get("llm_down")),   # 模型不可用而兜底
        "errors": sum(1 for d in details if d.get("error")),
        "verify_pass_rate": _pct(sum(1 for v in v_vals if v == "pass"), len(v_vals)) if v_vals else None,
        # 第2期观测：红线拦截次数 / LLM Judge 调用与裁定分布
        "redline_hits": sum(1 for d in details if d.get("redlines")),
        # 单列 internal_leak（模型把工具名/内部 JSON 回显给用户）：
        # 它是**模型行为问题**而非检索回归，混在 redline_hits 里会让"检索是不是坏了"
        # 这个问题看不清。统计分列，但判定仍算失败——回显内部实现对用户是真缺陷。
        "internal_leak_hits": sum(1 for d in details
                                  if "internal_leak" in (d.get("redlines") or [])),
        "redline_hits_excl_leak": sum(
            1 for d in details
            if [c for c in (d.get("redlines") or []) if c != "internal_leak"]),
        # 波动观测：single = 首次跑的通过率，passed = 多数投票后的稳定通过率。
        # 两者差值就是"模型抖动"带来的假失败量，差值大说明该轮数据不宜单独当门禁。
        "single_rate": _pct(sum(1 for d in details
                                if d.get("first_passed", d["passed"])), n),
        "retry_used": sum(1 for d in details if (d.get("attempts") or 1) > 1),
        "flaky_cases": [{"id": d.get("id"), "votes": d.get("votes")}
                        for d in details if d.get("flaky")],
        # 第3期观测：追问指代还原命中数与重复提问识别数
        "coref_hits": sum(1 for d in details if d.get("referent")),
        "repeat_hits": sum(1 for d in details if d.get("repeat")),
        # S4.4：框架代为执行的次数 / 涉及用例数——衡量"执行靠不靠模型心情"
        "forced_steps": sum(d.get("forced_steps") or 0 for d in details),
        "forced_cases": [d.get("id") for d in details if (d.get("forced_steps") or 0) > 0],
        "judge_calls": sum(1 for d in details if (d.get("judge") or "skip") not in ("skip", "")),
        "judge_bad": sum(1 for d in details
                         if d.get("judge") in ("unsupported", "off_topic")),
        "passed_rate": _pct(sum(1 for d in details if d["passed"]), n),
        "avg_latency_ms": int(sum(lat) / n),
        "p95_latency_ms": lat[int(n * 0.95) - 1] if n >= 2 else lat[0],
        "per_intent": per_intent,
        # S4.5：检索降级观测（门禁据此判定"本轮结果可不可信"）。
        # client_error > 0 = 向量库压根没打开（多半被 uvicorn 占着锁），
        # 此时用例走的是纯关键词路径，通过率再高也不代表真实能力。
        "retrieval": _retrieval_summary(reset=True),
    }
    return {"summary": summary, "details": details}


# ---------------------------------------------------------------- 结果落库
def save_run(result: dict) -> int:
    import json as _json
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO eval_runs(mode, total, metrics, details, created_at) VALUES(?,?,?,?,datetime('now','localtime'))",
            (result["summary"]["mode"], result["summary"]["total"],
             _json.dumps(result["summary"], ensure_ascii=False),
             _json.dumps(result["details"], ensure_ascii=False)))
        conn.commit()
        return cur.lastrowid


def latest_runs(limit: int = 10) -> list:
    import json as _json
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, mode, total, metrics, created_at FROM eval_runs"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            m = _json.loads(r["metrics"] or "{}")
        except Exception:
            m = {}
        out.append({"id": r["id"], "mode": r["mode"], "total": r["total"],
                    "created_at": r["created_at"], "metrics": m})
    return out


def run_detail(run_id: int) -> dict:
    import json as _json
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return {}
    return {"id": r["id"], "mode": r["mode"], "created_at": r["created_at"],
            "summary": _json.loads(r["metrics"] or "{}"),
            "details": _json.loads(r["details"] or "[]")}


# ---------------------------------------------------------------- 线上低分样本回流
def promote_run_to_case(run_id: int, overrides: Optional[dict] = None) -> dict:
    """把一条线上低分运行固化成评测用例，追加进 cases.jsonl。

    标注来源优先级：调用方传入的 overrides > 该次运行的实际结果。
    must / must_not 一律留空——自动生成的"关键词断言"会把模型当时的措辞当成标准答案，
    反而制造假失败；这两项留给人工补。
    """
    ov = overrides or {}
    with get_conn() as conn:
        r = conn.execute(
            "SELECT id, candidate_id, question, mode, answer, verify_status, "
            "verify_ratio, trajectory FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        raise ValueError(f"运行记录不存在：#{run_id}")
    d = dict(r)

    # 从轨迹取回当时的意图与工具序列，作为"当时认定的正确口径"
    intent = d.get("intent") or ""
    tools, redlines, cites, judge = [], [], 0, ""
    try:
        evs = json.loads(d.get("trajectory") or "[]")
    except Exception:
        evs = []
    for e in evs if isinstance(evs, list) else []:
        t = e.get("type")
        if t == "intent" and not intent:
            intent = e.get("intent") or ""
        elif t == "tool" and e.get("name"):
            tools.append(e["name"])
        elif t == "citation":
            cites = max(cites, len(e.get("items") or []))
        elif t == "verify":
            redlines = (e.get("redlines") or []) or redlines
        elif t == "verify_final":
            redlines = (e.get("redlines") or []) or redlines
            judge = e.get("judge") or judge

    seen, uniq = set(), []
    for t in tools:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    tools = uniq

    cases = load_cases()
    next_id = max([c.get("id", 0) for c in cases], default=0) + 1
    case = {
        "id": next_id,
        "q": (d.get("question") or "").strip(),
        "intent": ov.get("intent") or intent or "kb_qa",
        "cite": bool(ov.get("cite", cites > 0)),
        "ticket": bool(ov.get("ticket", "create_ticket" in tools)),
        "must": list(ov.get("must") or []),
        "must_not": list(ov.get("must_not") or []),
        "tools": tools,
        "modes": list(ov.get("modes") or ["agent"]),
        # 来源标记：便于日后区分"人工设计的用例"与"线上回流用例"
        "source": ov.get("source") or f"promote:agent_run#{run_id}",
    }
    # 自动带上"当时为什么算低分"，人工补断言时才知道该盯什么。
    # 只用 run 里现存的事实描述，不重新跑一遍去"验证"（那样会把已修好的问题洗白）。
    if ov.get("note"):
        case["note"] = ov["note"]
    else:
        why = []
        if d.get("verify_status") == "block" or redlines:
            why.append("命中红线" + ("：" + "、".join(str(x) for x in redlines) if redlines else ""))
        if d.get("verify_status") in ("fail", "partial"):
            why.append(f"核验 {d.get('verify_status')}（支撑度 {d.get('verify_ratio')}）")
        if judge in ("unsupported", "off_topic"):
            why.append("裁判判定" + ("存疑" if judge == "unsupported" else "偏离主题"))
        if judge == "error":
            why.append("裁判调用异常")
        if not tools:
            why.append("未调用任何工具")
        case["note"] = "线上回流｜" + ("；".join(why) or "低分")

    # 写前备份：cases.jsonl 是评测基准，无 git 保护，出错要能还原
    bak = CASES_PATH.with_suffix(".jsonl.bak")
    try:
        import shutil
        shutil.copyfile(CASES_PATH, bak)
    except Exception:
        pass
    with CASES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(case, ensure_ascii=False) + "\n")
    return {"case": case, "total": len(cases) + 1}
