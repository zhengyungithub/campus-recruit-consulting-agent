"""评测入口：python -u run_eval.py intent|fixed|agent [limit] [--repeats N] [--only ids]

约定（2026-10-26 定，事故复盘后新增）：
    ① 跑命令行评测前**先停 uvicorn**——Qdrant 是本地文件锁，两端同时开必冲突；
    ② 长评测必须带逐条进度打点，并 `python -u ... | tee log` 落盘，
       否则卡住时无法定位到具体用例与阶段（曾有过卡 9 小时无输出的悬案）；
    ③ 一次只跑一个评测进程；
    ④ agent 模式每条用例跑在**独立子进程**里（见 agent/eval_worker.py），
       超时可被真正 kill——线程护栏杀不掉挂起的 socket 读，实测卡过 23 分钟；
    ⑤ `--repeats 3` 开启多数投票，用于区分"真回归"与"模型抖动"（详见 §10）。

用法：
    python -u run_eval.py intent                 # 30 条意图，秒级
    python -u run_eval.py fixed                  # 30 条固定管线
    python -u run_eval.py agent                  # 42 条完整图，单轮
    python -u run_eval.py agent 0 --repeats 3    # 同上 + 失败用例复跑取多数（门禁推荐）
    python -u run_eval.py agent 0 --only 14,21    # 只跑指定用例（排查用）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent import eval as ev          # noqa: E402

# 退出码：0 = 全通过；2 = 有用例失败；1 = 执行错误（评测集为空等）
EX_OK, EX_ERR, EX_FAIL = 0, 1, 2


def parse_args(argv: list):
    mode = argv[1] if len(argv) > 1 else "intent"
    limit, repeats, only = 0, 1, None
    i = 2
    while i < len(argv):
        a = argv[i]
        if a == "--repeats" and i + 1 < len(argv):
            repeats = max(1, int(argv[i + 1])); i += 2
        elif a == "--only" and i + 1 < len(argv):
            only = [int(x) for x in argv[i + 1].replace("，", ",").split(",") if x.strip()]
            i += 2
        elif a.lstrip("-").isdigit():
            limit = int(a); i += 1
        else:
            i += 1
    return mode, limit, repeats, only


def main():
    mode, limit, repeats, only = parse_args(sys.argv)

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
    res = ev.run(mode, limit, repeats=repeats, only=only)
    if res.get("error"):
        print("ERROR:", res["error"])
        return EX_ERR
    rid = ev.save_run(res)
    s = res["summary"]
    print("-" * 52)
    print(f"mode={s['mode']} run_id=#{rid} total={s['total']} "
          f"passed={s['passed_rate']} 耗时={int(time.time() - t0)}s")
    for k in ("single_rate", "intent_acc", "must_rate", "cite_rate", "tool_rate",
              "ticket_acc", "degraded", "llm_downgraded", "errors",
              "verify_pass_rate", "redline_hits", "internal_leak_hits",
              "redline_hits_excl_leak", "coref_hits", "repeat_hits",
              "judge_calls", "judge_bad", "plan_llm",
              "avg_latency_ms", "p95_latency_ms"):
        print(f"  {k}={s.get(k)}")
    if s.get("retry_used"):
        print(f"  多数投票：{s['retry_used']} 条用例触发复跑；flaky="
              f"{[c['id'] for c in (s.get('flaky_cases') or [])]}")
    bad = [d for d in res["details"] if not d["passed"]]
    if bad:
        print("失败用例:", [(d.get("id"), str(d.get("q"))[:24]) for d in bad])
        for d in bad:
            for r in (d.get("reasons") or [])[:3]:
                print(f"    #{d.get('id')} {r[:110]}")
    return EX_OK if not bad else EX_FAIL


if __name__ == "__main__":
    sys.exit(main())
