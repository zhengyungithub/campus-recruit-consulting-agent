"""评测 worker：一条 agent 用例跑在**独立进程**里，结果以 dict 返回 / JSON 打到 stdout。

为什么必须独立进程（2026-10-26 定，替代原 threading 护栏）：

    原来的 `join(timeout)` 护栏是**假护栏**——Python 线程无法被强制杀死。
    一旦卡在 socket 读（模型 / 向量请求静默挂起），join 超时后线程还活着，
    主进程只能继续等它自己返回。第3期实测单条用例卡 1417s（23.6 分钟），
    而护栏设的是 120s，说明卡点根本不在护栏覆盖的节点边界上。
    子进程可以被 kill，超时才真正有兜底。

    附带好处（同样踩过的坑）：子进程退出即释放 Qdrant 本地文件锁与 sqlite 连接。
    评测主进程因此能在两条用例之间安全地清理测试数据（tickets / messages），
    不会出现"自己占着锁所以清不掉"的死锁。

两种用法：
    ① 常驻池（默认，推荐）：eval.run_agent_guarded 用 ProcessPoolExecutor 复用同一个
       worker 进程，只付一次 import 开销（实测单进程冷启动 13~22s，复用后每条 ~3s）。
       超时时 kill 该 worker 并懒重建。
    ② 一次性子进程（排查用）：python -u -m agent.eval_worker <payload.json>
       stdout 最后一行固定为 __EVAL_RESULT__{...}，其余行是日志，可忽略。
"""
import json
import os
import sys
import time
from pathlib import Path

RESULT_PREFIX = "__EVAL_RESULT__"


def run_payload(payload: dict) -> dict:
    """执行一次用例；返回结果 dict（含 error 字段表示失败）。

    必须定义在模块顶层：ProcessPoolExecutor 用 pickle 传函数，内层函数无法序列化。
    """
    app_dir = Path(__file__).resolve().parent.parent
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))

    cid = int(payload.get("cid") or 0)
    q = payload.get("q") or ""
    mode = payload.get("mode") or "agent"
    thread_id = payload.get("thread_id") or f"eval-{int(time.time() * 1000)}"
    use_history = bool(payload.get("use_history"))
    purge_vectors = bool(payload.get("purge_vectors"))

    if payload.get("debug"):
        # 排查用：确认子进程看到的历史播种、指代还原是否与父进程一致
        from .store import get_conn
        from . import memory as _mem, coref as _coref
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, role, content FROM messages WHERE candidate_id=? ORDER BY id",
                (cid,)).fetchall()
        try:
            _last = _mem.last_user_question(cid)
            _cr = _coref.resolve(q, prev_question=_last)
        except Exception as e:
            _last, _cr = f"<err {e!r}>", {}
        print(f"[worker-debug] pid={os.getpid()} cid={cid} mode={mode} "
              f"use_history={use_history} messages={len(rows)} last_q={_last!r} "
              f"coref={(_cr.get('question') if _cr.get('resolved') else None)!r} "
              f"anchor={(_cr.get('anchor') if _cr.get('resolved') else None)!r}")
        for r in rows:
            print(f"    {r['id']} {r['role']} {r['content'][:45]}")
        sys.stdout.flush()

    t0 = time.time()
    try:
        if purge_vectors or mode == "purge":
            # 多轮用例会 recall 历史，播种前必须清干净，否则上一条用例的记忆会串进来
            from . import retrieval
            retrieval.purge_chat_memory(cid)
        if mode == "purge":
            # 整轮结束后的收尾：只清向量、不跑图
            return {"answer": "", "citations": [], "tool_trace": [], "verify": {},
                    "plan": {}, "failures": [], "degraded": False, "_purged": True}

        if mode == "fixed":
            from .pipeline import fixed_chat_reply
            from . import verifier as _ver
            answer, _tid, cites = fixed_chat_reply(cid, q, allow_ticket=False)
            v = _ver.verify_answer(answer or "", cites or [], question=q) if cites else {}
            out = {"answer": answer or "", "citations": cites or [], "tool_trace": [],
                   "verify": v, "plan": {}, "failures": [], "degraded": False}
        else:
            from .graph import run_agent
            out = run_agent(cid, q, thread_id=thread_id, use_history=use_history)
    except Exception as e:
        return {"error": repr(e), "latency_ms": int((time.time() - t0) * 1000)}

    if not isinstance(out, dict):
        return {"error": f"返回值类型异常：{type(out).__name__}"}
    out["_worker_ms"] = int((time.time() - t0) * 1000)
    return out


def _emit_result(obj: dict) -> int:
    sys.stdout.write(RESULT_PREFIX + json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    return 0 if not obj.get("error") else 3


def main() -> int:
    # Windows 控制台默认 GBK，中文 print 会 UnicodeEncodeError → 强制 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) < 2:
        return _emit_result({"error": "缺少 payload 参数"})
    try:
        payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except Exception as e:
        return _emit_result({"error": f"payload 解析失败：{e!r}"})
    return _emit_result(run_payload(payload))


if __name__ == "__main__":
    sys.exit(main())
