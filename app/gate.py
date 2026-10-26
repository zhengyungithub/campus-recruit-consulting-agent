"""回归门禁（S4.5）：改了 Agent 就自动跑评测，退化就拦住。

为什么需要（§1 原则② 的自动化版本）：
    "记得跑评测"这件事靠不住。本项目的教训足够多——误删多轮历史播种、
    dense 打分方向反了、内部字段泄漏——**每一次都是靠"刚好跑了一轮"才发现的**。
    门禁的作用是把"刚好"变成"必然"。

两档：
    快门禁（默认）  intent + fixed，约 8 秒，**不需要停服务**
                    （固定管线走 SQL 关键词检索，不碰 Qdrant，实测确认）
    全量门禁 --full 再加 agent（42 条 / repeats=3，约 2.5 分钟），
                    **必须先停 uvicorn**——Qdrant 本地模式是文件锁

⚠️ 门禁最危险的失效不是误报，而是**假绿灯**：
    agent 模式在 uvicorn 运行时照样能跑完 42 条并给出 95% 通过率，
    但 `client_error>0` 说明向量库压根没打开，测的其实是纯关键词路径。
    所以 agent 档位强制校验降级计数，宁可判"环境冲突"也不给结论。

用法：
    python gate.py                 快门禁（提交前）
    python gate.py --full          全量门禁（合并前；先停 uvicorn）
    python gate.py --print         只看当前基线
    python gate.py --update        用本轮结果刷新基线（新增用例 / 接受退化后）
    python gate.py --full --update 跑全量并把结果设为新基线

退出码（git hook 依赖它决定是否放行）：
    0  通过
    2  回归（低于基线，或用例集变化需要先更新基线）
    3  环境冲突（agent 档位下向量库被占）——门禁拒绝给出结论
    1  执行错误
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BASELINE_PATH = APP_DIR / "evals" / "gate_baseline.json"

EX_OK, EX_ERR, EX_REGRESSION, EX_ENV = 0, 1, 2, 3

# tolerance：更新基线时给通过率留的余量。
#   intent / fixed 是确定性逻辑，掉一条就是回归，不留余量；
#   agent 有模型抖动（实测同代码多次跑能差 4 条），留 5% 免得每次误报。
DEFAULT_BASELINE = {
    "_doc": "回归门禁基线。min_passed_rate 是下限（低于即回归）；known_fail 是已接受的"
            "稳定失败用例（在白名单内只提示、不算回归）；require_dense 为真时，"
            "向量库打不开直接判环境冲突而非放行。用 python gate.py --update 刷新。",
    "modes": {
        "intent": {"total": 30, "min_passed_rate": 1.0, "known_fail": [],
                   "tolerance": 0.0, "repeats": 1, "require_dense": False},
        "fixed": {"total": 30, "min_passed_rate": 1.0, "known_fail": [],
                  "tolerance": 0.0, "repeats": 1, "require_dense": False},
        "agent": {"total": 42, "min_passed_rate": 0.90, "known_fail": [],
                  "tolerance": 0.05, "repeats": 3, "require_dense": True},
    },
}


# ---------------------------------------------------------------- 基线读写
def load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return json.loads(json.dumps(DEFAULT_BASELINE))     # 深拷贝，避免污染默认值
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ 基线文件损坏（{e}），回退到内置默认基线")
        return json.loads(json.dumps(DEFAULT_BASELINE))


def save_baseline(bl: dict, commit: str = ""):
    from datetime import datetime
    bl["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if commit:
        bl["commit"] = commit
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(bl, ensure_ascii=False, indent=2), encoding="utf-8")


def _git_head() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=str(APP_DIR.parent), capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 跑一个模式
def run_mode(mode: str, repeats: int) -> dict:
    """在**独立子进程**里跑一个模式的评测，返回 {run_id, summary, failed_ids, rc}

    为什么每个模式都开独立进程（不只是为了隔离崩溃）：
        Qdrant 本地模式是文件锁，谁先打开谁独占。若 gate 主进程或上一个模式的进程
        还持有 client，agent 档位的 worker 子进程就全部打不开库，
        只能静默降级成关键词检索——正是门禁最该防住的假绿灯。
    每个模式跑完进程即退出，锁随之释放。
    """
    fd, path = tempfile.mkstemp(suffix=".json", prefix=f"gate_{mode}_")
    import os
    os.close(fd)
    cmd = [sys.executable, "-u", "run_eval.py", mode, "--json", path]
    if repeats > 1:
        cmd += ["--repeats", str(repeats)]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(APP_DIR), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=1200)
    except subprocess.TimeoutExpired:
        return {"error": f"{mode} 超时（>1200s）"}
    finally:
        pass
    elapsed = time.time() - t0
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return {"error": f"{mode} 未产出结果（rc={proc.returncode}）：{e}"
                         + (f"｜{' / '.join(tail)}" if tail else "")}
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
    data["elapsed_s"] = round(elapsed, 1)
    return data


# ---------------------------------------------------------------- 判定
def check_mode(mode: str, res: dict, base: dict) -> list:
    """返回问题列表（空 = 通过）。每项 {"level": "regression"/"env"/"warn", "msg": [...]}"""
    problems = []
    s = res["summary"]
    total, passed_rate = s.get("total", 0), s.get("passed_rate", 0.0)
    failed = res.get("failed_ids") or []
    add = lambda lvl, lines: problems.append({"level": lvl, "msg": lines})

    # ① 用例集变化：通过率不再可比，必须先更新基线再谈回归
    if total != base.get("total"):
        add("regression", [
            f"评测集条数变化：基线 {base.get('total')} 条 → 本次 {total} 条。"
            f"通过率已不可比，请先跑 `python gate.py --update` 刷新基线。"])
        return problems          # 后续判定无意义，直接返回

    # ② 环境冲突：向量库没打开 → 结果不代表真实能力
    ret = s.get("retrieval") or {}
    if base.get("require_dense") and (ret.get("client_error") or 0) > 0:
        add("env", [
            f"向量库未能打开（client_error={ret['client_error']}），本轮 {total} 条用例"
            f"走的是**纯关键词路径**。",
            "最常见原因：uvicorn 还在跑，Qdrant 本地文件锁被它占着。",
            "门禁拒绝给出结论——这种通过率不说明任何问题。请先停掉服务再跑全量门禁。"])

    # ③ 执行错误：模型/工具抛异常，与"答得对不对"是两回事，必须单独拦
    if s.get("errors"):
        add("regression", [f"有 {s['errors']} 条用例执行异常（超时 / 崩溃），"
                           f"不是能力问题，但必须查清。"])

    # ④ 通过率低于基线
    floor = base.get("min_passed_rate", 0.0)
    if passed_rate < floor:
        add("regression", [f"通过率 {passed_rate:.3f} < 基线 {floor:.3f}"
                           f"（{total - len(failed)}/{total}）"])

    # ⑤ 新增失败用例：白名单内的只提示，白名单外的是回归
    known = set(base.get("known_fail") or [])
    fresh = [i for i in failed if i not in known]
    if fresh:
        details = res.get("details") or []
        lines = [f"新增失败用例 {len(fresh)} 条（基线白名单外）：{fresh}"]
        for i in fresh[:6]:
            d = next((x for x in details if x.get("id") == i), None)
            if not d:
                continue
            q = str(d.get("q") or "")[:26]
            why = "；".join(str(r) for r in (d.get("reasons") or [])[:2])[:100]
            lines.append(f"   #{i} {q}  · {why}")
        add("regression", lines)
    elif failed:
        add("warn", [f"{len(failed)} 条失败均在基线白名单内（已知问题）：{failed}"])

    return problems


# ---------------------------------------------------------------- 输出
def _mark(problems: list) -> str:
    if any(p["level"] == "env" for p in problems):
        return "🚫"
    if any(p["level"] == "regression" for p in problems):
        return "❌"
    if problems:
        return "⚠️ "
    return "✅"


def main() -> int:
    argv = sys.argv[1:]
    full = "--full" in argv
    update = "--update" in argv
    if "--print" in argv:
        print(json.dumps(load_baseline(), ensure_ascii=False, indent=2))
        return EX_OK

    modes = ("intent", "fixed", "agent") if full else ("intent", "fixed")
    bl = load_baseline()
    if update:
        bl = load_baseline() if BASELINE_PATH.exists() else json.loads(json.dumps(DEFAULT_BASELINE))

    print("═" * 54)
    print(f"回归门禁 · {'全量（intent+fixed+agent）' if full else '快检（intent+fixed）'}"
          + (" · 更新基线" if update else ""))
    if full:
        print("提示：全量门禁需先停 uvicorn，否则向量库打不开（门禁会拒绝给结论）")
    if update:
        # 澄清：更新基线时"新增失败"是预期信号，本轮失败会写进白名单而非拦住提交
        print("更新基线模式：本轮失败用例将写入白名单，不再作为回归判据")
    print("═" * 54)

    results, all_problems = {}, {}
    env_blocked = False
    for idx, mode in enumerate(modes, 1):
        cfg = (bl.get("modes") or {}).get(mode) or DEFAULT_BASELINE["modes"][mode]
        print(f"[{idx}/{len(modes)}] {mode:<6} … ", end="", flush=True)
        res = run_mode(mode, int(cfg.get("repeats") or 1))
        if res.get("error"):
            print(f"失败：{res['error']}")
            all_problems[mode] = [{"level": "regression", "msg": [res["error"]]}]
            results[mode] = None
            continue
        s = res["summary"]
        problems = check_mode(mode, res, cfg)
        all_problems[mode] = problems
        results[mode] = res
        print(f"{s['total'] - len(res.get('failed_ids') or [])}/{s['total']} "
              f"({s['passed_rate'] * 100:.1f}%)  {res.get('elapsed_s')}s  "
              f"{_mark(problems)}  run #{res.get('run_id')}")
        if any(p["level"] == "env" for p in problems):
            env_blocked = True

    # ---- 更新基线 ----
    if update:
        for mode in modes:
            res = results.get(mode)
            if not res:
                continue
            cfg = bl.setdefault("modes", {}).setdefault(mode, {})
            s = res["summary"]
            tol = float(cfg.get("tolerance") or 0.0)
            cfg.update({
                "total": s.get("total"),
                "min_passed_rate": round(max(0.0, float(s.get("passed_rate") or 0) - tol), 3),
                "known_fail": res.get("failed_ids") or [],
                "repeats": cfg.get("repeats", 1),
                "require_dense": cfg.get("require_dense", False),
            })
        save_baseline(bl, _git_head())
        print("-" * 54)
        print(f"基线已更新 → {BASELINE_PATH.name}（commit {bl.get('commit') or '-'}）")
        for mode in modes:
            cfg = bl["modes"][mode]
            print(f"  {mode:<6} 下限 {cfg['min_passed_rate']:.3f}  白名单 {cfg['known_fail']}")
        return EX_OK

    # ---- 汇总 ----
    print("═" * 54)
    exit_code = EX_OK
    for mode in modes:
        for p in all_problems.get(mode) or []:
            icon = {"env": "🚫", "regression": "❌", "warn": "⚠️ "}[p["level"]]
            print(f"{icon} [{mode}] {p['msg'][0]}")
            for line in p["msg"][1:]:
                print(f"    {line}")
            if p["level"] == "regression":
                exit_code = EX_REGRESSION
            elif p["level"] == "env" and exit_code == EX_OK:
                exit_code = EX_ENV

    if exit_code == EX_OK:
        summary_bits = []
        for mode in modes:
            res = results.get(mode)
            if res:
                s = res["summary"]
                summary_bits.append(f"{mode} {s['total'] - len(res.get('failed_ids') or [])}"
                                    f"/{s['total']}")
        print(f"✅ 门禁通过（{'、'.join(summary_bits)}）")
    elif exit_code == EX_ENV:
        print("🚫 环境冲突，门禁不下结论。停掉 uvicorn 后重跑：python gate.py --full")
    else:
        print("❌ 门禁未通过。若本次退化是有意为之，跑 "
              "`python gate.py --full --update` 把当前结果设为新基线。")
    return exit_code


if __name__ == "__main__":
    try:
        for _s in (sys.stdout, sys.stderr):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
