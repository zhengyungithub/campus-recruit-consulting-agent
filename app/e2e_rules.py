"""S6.1 业务规则 · HTTP 端到端验证（服务需处于运行状态）。

    cd app && python e2e_rules.py

覆盖四项新增规则，每项都断言"该拦的拦住、该放的放行"，而不只是"接口没报错"：
    1. 状态流转校验 + 操作审计
    2. 工单回复主动推送（含幂等）
    3. 知识库审核（草稿对外不可见，通过后可见）
    4. 工单 SLA（认领 / 加急 / 逾期判定）

跑完清理所有测试数据（工单 / 通知 / 知识条目 / 状态历史），并把候选人状态还原。
"""
import json
import os
import sqlite3
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
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if safe:
            try:
                return {**json.loads(e.read().decode()), "ok": False, "code": e.code}
            except Exception:
                return {"ok": False, "code": e.code, "detail": str(e)}
        raise RuntimeError(f"{method} {path} -> {e.code} {e.read().decode()[:200]}")


def q(sql, args=()):
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return rows


def main():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    adm = call("POST", "/api/admin/login", safe=True, body={
        "username": os.getenv("ADMIN_USER", "admin"),
        "password": resolve_admin_password(generate=False)})
    atok = adm["token"]

    # 先清可能的残留：上次异常退出时清理段没走到 commit，会留下测试数据
    con.execute("DELETE FROM faqs WHERE question LIKE '端到端验收%'")
    con.execute("DELETE FROM tickets WHERE question LIKE '%端到端验收%'")
    con.commit()

    # 挑一个非终态候选人
    c = con.execute("SELECT id, phone, name, status FROM candidates"
                    " WHERE status NOT IN ('已归档') ORDER BY id LIMIT 1").fetchone()
    cid, phone, old_status = c["id"], c["phone"], c["status"]
    print(f"用候选人 #{cid} {c['name']}（{phone}），原状态「{old_status}」")

    # ---------------------------------------------------------------- 1 流转 + 审计
    print("\n[1] 状态流转校验")
    target = "已发offer"                      # 距「已初筛通过」等多步，必然是跳步
    bad = call("PATCH", f"/api/admin/candidates/{cid}/status", token=atok, safe=True,
               body={"status": target})
    check(bad.get("code") == 400 and "需填写原因" in (bad.get("detail") or ""),
          "跳步不填原因被拒", str(bad.get("detail"))[:40])

    ok = call("PATCH", f"/api/admin/candidates/{cid}/status", token=atok,
              body={"status": target, "reason": "端到端验收-破格"})
    check(ok.get("ok") and ok.get("code") == "jump", "跳步填原因放行", str(ok)[:70])

    row = q("SELECT operator, reason, status FROM status_history"
            " WHERE candidate_id=? ORDER BY id DESC LIMIT 1", (cid,))[0]
    check(row["operator"] == "admin", "审计：记下了操作人", row["operator"])
    check(row["reason"] == "端到端验收-破格", "审计：记下了原因", row["reason"])

    # 终态：先归档，再试图改回去
    call("PATCH", f"/api/admin/candidates/{cid}/status", token=atok,
         body={"status": "已归档", "reason": "端到端验收"})
    back = call("PATCH", f"/api/admin/candidates/{cid}/status", token=atok, safe=True,
                body={"status": "已约初面", "reason": "想改回去"})
    check(back.get("code") == 400 and "终态" in (back.get("detail") or ""),
          "终态不可再改", str(back.get("detail"))[:40])

    logs = call("GET", "/api/admin/audit?limit=20", token=atok)["logs"]
    denied = [l for l in logs if l["result"] == "denied"]
    check(len(denied) >= 2, "被拒绝的尝试也进了审计", f"{len(denied)} 条 denied")
    check(any(l["actor"] == "admin" for l in logs), "审计含操作人")

    # ---------------------------------------------------------------- 2 工单回复推送
    print("\n[2] 工单回复主动推送")
    con.execute("INSERT INTO tickets(candidate_id,question,status,created_at)"
                " VALUES(?,?, 'pending', datetime('now'))",
                (cid, "【端到端验收】能改到下周吗"))
    con.commit()
    tid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    # 回复里故意带手机号：推送是"一眼扫过"的场景，摘要必须脱敏
    call("POST", f"/api/admin/tickets/{tid}/reply", token=atok,
         body={"answer": "联系人 13812345678，已帮你改到下周三下午。"})
    ns = q("SELECT kind, title, body FROM notices WHERE candidate_id=? AND kind='ticket_reply'", (cid,))
    check(len(ns) == 1, "候选人收到工单回复提醒", f"{len(ns)} 条")
    check(bool(ns) and f"#{tid}" in ns[0]["title"], "提醒里带工单号", ns[0]["title"] if ns else "")
    check(bool(ns) and "完整回复" in ns[0]["body"], "只放摘要并引导去【我的反馈】",
          ns[0]["body"][:60] if ns else "")
    check(bool(ns) and "13812345678" not in ns[0]["body"], "摘要里的手机号被掩码",
          ns[0]["body"][:70] if ns else "")
    check(bool(ns) and "(" not in ns[0]["body"], "摘要不是元组/JSON 泄漏",
          ns[0]["body"][:70] if ns else "")

    call("POST", f"/api/admin/tickets/{tid}/reply", token=atok,
         body={"answer": "补充：请携带身份证原件。"})
    n2 = q("SELECT COUNT(*) c FROM notices WHERE candidate_id=? AND kind='ticket_reply'", (cid,))[0]["c"]
    check(n2 == 1, "同一工单补充回复不重复打扰", f"{n2} 条")

    # ---------------------------------------------------------------- 3 知识库审核
    print("\n[3] 知识库审核")
    from agent.pipeline import _faq_search
    q_text = "端到端验收专用条目：办理周期是几个工作日"
    a_text = "端到端验收专用答案：通常为五个工作日，以实际通知为准。"
    r = call("POST", "/api/admin/faqs/bulk", token=atok,
             body={"items": [{"question": q_text, "answer": a_text}],
                   "source": "doc_import", "force": True})
    check(r.get("added") == 1, "批量来源入库成功", str(r)[:60])
    fid = q("SELECT id FROM faqs WHERE question=? ORDER BY id DESC LIMIT 1", (q_text,))[0]["id"]
    rs = q("SELECT review_status FROM faqs WHERE id=?", (fid,))[0]["review_status"]
    check(rs == "draft", "doc_import 来源入草稿待审", rs)
    check(not any(q_text in h["question"] for h in _faq_search(q_text)),
          "草稿对检索不可见（关键：错知识不会放大）")
    pend = call("GET", "/api/admin/faqs?review=draft", token=atok)
    check(any(f["id"] == fid for f in pend["faqs"]), "待审核列表能看到它")

    rv = call("POST", f"/api/admin/faqs/{fid}/review", token=atok, body={"action": "publish"})
    check(rv.get("review_status") == "published", "审核通过", str(rv)[:60])
    check(any(q_text in h["question"] for h in _faq_search(q_text)),
          "通过后对检索可见")

    # ---------------------------------------------------------------- 4 工单 SLA
    print("\n[4] 工单 SLA 与负责人")
    con.execute("INSERT INTO tickets(candidate_id,question,status,created_at)"
                " VALUES(?,?, 'pending', datetime('now','-30 hours'))",
                (cid, "【端到端验收】逾期用例"))
    con.commit()
    tid2 = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    cl = call("POST", f"/api/admin/tickets/{tid2}/claim", token=atok, body={"priority": "urgent"})
    check(cl.get("ok") and cl.get("assignee") == "admin", "认领记下负责人", str(cl)[:60])
    t2 = [t for t in call("GET", "/api/admin/tickets", token=atok)["tickets"]
          if t["id"] == tid2][0]
    check(t2["status"] == "handling", "认领后进入处理中", t2["status"])
    check(t2["priority_label"] == "加急" and t2["sla_hours"] == 8, "加急单 SLA 更短",
          f"{t2['priority_label']} / {t2['sla_hours']}h")
    check(t2["overdue"] and t2["overdue_hours"] > 0, "挂了 30 小时判定逾期",
          f"逾期 {t2['overdue_hours']}h")
    st = call("GET", "/api/admin/tickets", token=atok)["stats"]
    check(st["overdue"] >= 1 and st["handling"] >= 1, "看板统计含逾期与处理中", str(st)[:80])

    # ---------------------------------------------------------------- 清理
    print("\n[5] 清理测试数据")
    for sql, args in [
        ("DELETE FROM tickets WHERE question LIKE '%端到端验收%'", ()),
        ("DELETE FROM notices WHERE candidate_id=?", (cid,)),
        ("DELETE FROM faqs WHERE question=?", (q_text,)),
        ("DELETE FROM status_history WHERE candidate_id=? AND reason LIKE '端到端验收%'", (cid,)),
        ("UPDATE candidates SET status=? WHERE id=?", (old_status, cid)),
        ("DELETE FROM audit_log WHERE target=? OR action LIKE 'faq_%'", (f"candidate#{cid}",)),
    ]:
        con.execute(sql, args)
        con.commit()          # 逐条提交：断言失败中途退出时，已清的部分不会回滚
    print("    已清理工单 / 通知 / 知识条目 / 状态历史 / 审计；状态还原为", old_status)
    con.close()

    print("\n" + "=" * 46)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        raise SystemExit(1)
    print("S6.1 业务规则端到端全部通过")


if __name__ == "__main__":
    main()
