"""S5.3 主动推送 · HTTP 端到端验证（走真实接口，不碰内部函数）。

跑法（**服务需处于运行状态**，与 smoke_notices.py 相反）：
    cd app && python e2e_notices.py

验收口径就是排期里写的那句：改一个候选人状态 -> 该候选人收到提醒。
脚本会挑一个真实候选人，改状态后以该候选人身份拉取通知，断言能收到；
跑完把状态、历史、通知全部还原，不留测试数据。
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

from agent.config import DB_PATH, resolve_admin_password

BASE = "http://127.0.0.1:8000"
FAILED = []


def check(cond, label, extra=""):
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(label)
    return cond


def call(method, path, token=None, body=None, safe=False):
    """safe=True 时不抛异常，把 4xx 回传成 dict——用于断言"这个操作应该被拒绝"。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    # 环境里注入了 http_proxy，访问本地服务必须绕过，否则会被代理拦下
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if safe:
            try:
                return {**json.loads(e.read().decode()), "ok": False, "code": e.code}
            except Exception:
                return {"ok": False, "code": e.code, "detail": str(e)}
        raise RuntimeError(f"{method} {path} -> {e.code} {e.read().decode()[:200]}")


def main():
    # 挑一个真实候选人，记下原始状态以便还原
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    c = con.execute("SELECT id, phone, name, status FROM candidates ORDER BY id LIMIT 1").fetchone()
    if not c:
        print("库里没有候选人，先导入一个再跑")
        return 1
    cid, phone, name, old_status = c["id"], c["phone"], c["name"], c["status"]
    print(f"用候选人 #{cid} {name}（{phone}），原状态「{old_status}」")

    try:
        print("\n[1] 管理员登录")
        adm = call("POST", "/api/admin/login", body={
            "username": os.getenv("ADMIN_USER", "admin"),
            "password": resolve_admin_password(generate=False)})
        atok = adm["token"]
        check(adm.get("role") in ("super", "hr"), "登录成功", adm.get("role"))

        print("\n[2] HR 改状态（验收动作）")
        new_status = "已约复试" if old_status != "已约复试" else "初面通过"
        # S6.1：跨越多个环节属于"跳步"，接口要求填原因（否则 400）。
        # 这里顺带验证带原因能通过——不带原因会被拒，见下面的 [2b]。
        r = call("PATCH", f"/api/admin/candidates/{cid}/status", token=atok,
                 body={"status": new_status, "note": "端到端验收", "reason": "端到端验收"})
        check(r.get("ok"), "状态更新接口返回 ok")

        print("\n[2b] S6.1 流转校验：非法回退应被拒")
        bad = call("PATCH", f"/api/admin/candidates/{cid}/status", token=atok, safe=True,
                   body={"status": "已初筛通过", "reason": "试试能不能倒回去"})
        check(not bad.get("ok") and "不允许" in (bad.get("detail") or ""),
              "白名单外的倒退被拒绝", str(bad)[:80])

        print("\n[3] 候选人登录并拉取通知")
        code = call("POST", "/api/login/request_code", body={"phone": phone})["demo_code"]
        ctok = call("POST", "/api/login/verify", body={"phone": phone, "code": code})["token"]
        res = call("GET", "/api/notices", token=ctok)
        items = res["notices"]
        hit = [n for n in items if new_status in n["title"]]
        check(len(hit) == 1, f"收到「{new_status}」的提醒", f"共 {len(items)} 条")
        check(bool(items) and items[0]["read_at"] is None, "新通知为未读")
        check(res["unread"] == len([n for n in items if not n["read_at"]]),
              "未读数与列表一致", str(res["unread"]))
        if hit:
            check("端到端验收" in hit[0]["body"], "HR 填写的备注回显给候选人", hit[0]["body"])
            print(f"       标题：{hit[0]['title']}")
            print(f"       正文：{hit[0]['body']}")

        print("\n[4] 标记已读")
        ids = [n["id"] for n in items]
        r = call("POST", "/api/notices/read", token=ctok, body={"ids": ids})
        check(r["unread"] == 0, "全部已读后未读归零", str(r["unread"]))

        print("\n[5] 管理端推送记录")
        ar = call("GET", "/api/admin/notices?limit=5", token=atok)
        check(ar["stats"]["total"] >= 1, "推送记录有数据", json.dumps(ar["stats"], ensure_ascii=False))
        check(bool(ar["notices"]) and ar["notices"][0].get("name") is not None,
              "记录里能看出推给了谁")
        check("config" in ar and ar["config"]["notice_enabled"] == 1, "回显推送配置")

        print("\n[6] 手动扫描接口")
        sr = call("POST", "/api/admin/notices/scan", token=atok)
        check(sr.get("ok"), "手动扫描返回 ok", json.dumps(sr.get("result"), ensure_ascii=False))
        check(sr["result"].get("error") is None, "扫描无异常",
              str(sr["result"].get("error")))
    finally:
        print("\n[7] 还原测试数据")
        con.execute("DELETE FROM notices WHERE candidate_id=?", (cid,))
        con.execute("DELETE FROM status_history WHERE candidate_id=? AND note='端到端验收'", (cid,))
        con.execute("UPDATE candidates SET status=? WHERE id=?", (old_status, cid))
        con.commit()
        left = con.execute("SELECT COUNT(*) c FROM notices WHERE candidate_id=?", (cid,)).fetchone()["c"]
        now_status = con.execute("SELECT status FROM candidates WHERE id=?", (cid,)).fetchone()["status"]
        check(left == 0, "通知已清除")
        check(now_status == old_status, "候选人状态已还原", now_status)
        con.close()

    print("\n" + ("=" * 46))
    if FAILED:
        print(f"端到端失败 {len(FAILED)} 项：" + "；".join(FAILED))
        return 1
    print("主动推送端到端全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
