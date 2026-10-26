"""评测入口：python -u run_eval.py intent|fixed|agent [limit]

约定（2026-10-26 定，事故复盘后新增）：
    ① 跑命令行评测前**先停 uvicorn**——Qdrant 是本地文件锁，两端同时开必冲突；
    ② 长评测必须带逐条进度打点，并 `python -u ... | tee log` 落盘，
       否则卡住时无法定位到具体用例与阶段（曾有过卡 9 小时无输出的悬案）；
    ③ 一次只跑一个评测进程。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent import eval as ev          # noqa: E402


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "intent"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    # 逐条打点：包一层 _run_one，打印每条用例耗时
    orig = ev._run_one

    def traced(case, m, cid):
        t0 = time.time()
        try:
            return orig(case, m, cid)
        finally:
            print(f"  [{m}] #{case.get('id')} {str(case.get('q'))[:30]} "
                  f"{int((time.time() - t0) * 1000)}ms", flush=True)

    ev._run_one = traced

    t0 = time.time()
    res = ev.run(mode, limit)
    if res.get("error"):
        print("ERROR:", res["error"])
        return 1
    rid = ev.save_run(res)
    s = res["summary"]
    print("-" * 52)
    print(f"mode={s['mode']} run_id=#{rid} total={s['total']} "
          f"passed={s['passed_rate']} 耗时={int(time.time() - t0)}s")
    for k in ("intent_acc", "must_rate", "cite_rate", "tool_rate", "ticket_acc",
              "degraded", "llm_downgraded", "errors", "verify_pass_rate",
              "redline_hits", "coref_hits", "repeat_hits", "judge_calls",
              "judge_bad", "plan_llm", "avg_latency_ms", "p95_latency_ms"):
        print(f"  {k}={s.get(k)}")
    bad = [d for d in res["details"] if not d["passed"]]
    if bad:
        print("失败用例:", [(d.get("id"), str(d.get("q"))[:24]) for d in bad])
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
