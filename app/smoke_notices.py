"""S5.3 主动推送冒烟：改状态 -> 收到提醒，且不会重复打扰。

跑法（必须先停 uvicorn，避免与后台扫描线程抢锁）：
    cd app && python smoke_notices.py

覆盖 6 件事，任何一条不过就退出码 1：
  1. 状态变更产生通知，文案正确
  2. 重复扫描不重复推送（幂等）
  3. 同一状态重复保存不打扰
  4. 待办态停滞产生提醒，冷却分桶生效
  5. 未读计数与已读
  6. 跑完把测试数据全部清掉
"""
import sqlite3
import sys
from datetime import datetime, timedelta

from agent import notices
from agent.config import DB_PATH

FAILED = []


def check(cond, label, extra=""):
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(label)
    return cond


def conn_db():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def main():
    notices.bind({
        "待传面试资料": "请尽快上传面试资料（入口见通知短信），逾期会影响后续环节。",
        "已约初面": "请按通知的时间参加初面（线上为腾讯会议，线下为站点现场）。",
        "测评中": "请在截止时间前完成在线测评。",
    })
    notices.ensure_tables()

    con = conn_db()
    # 造一个一次性候选人（手机号用不可能冲突的测试号段）
    phone = "19000000001"
    con.execute("DELETE FROM notices WHERE candidate_id IN (SELECT id FROM candidates WHERE phone=?)",
                (phone,))
    con.execute("DELETE FROM status_history WHERE candidate_id IN (SELECT id FROM candidates WHERE phone=?)",
                (phone,))
    con.execute("DELETE FROM candidates WHERE phone=?", (phone,))
    cur = con.execute(
        "INSERT INTO candidates(name,phone,school,major,position,site,status,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        ("推送测试", phone, "测试大学", "测试专业", "测试岗", "本站", "已初筛通过",
         datetime.now().isoformat(timespec="seconds")))
    cid = cur.lastrowid
    con.execute("INSERT INTO status_history(candidate_id,status,note,created_at) VALUES(?,?,?,?)",
                (cid, "已初筛通过", "名单导入", datetime.now().isoformat(timespec="seconds")))
    con.commit()

    try:
        # 落库后先扫一次：导入本身会产生一条"已初筛通过"通知，把它算作基线，
        # 之后所有断言都按增量算，避免把首条通知误判成回归。
        notices.scan()
        base = len(notices.list_for(cid))
        check(base == 1, "导入产生 1 条首条通知", f"实际 {base}")

        print("\n[1] 状态变更 -> 产生通知")
        con.execute("UPDATE candidates SET status='已约初面' WHERE id=?", (cid,))
        con.execute("INSERT INTO status_history(candidate_id,status,note,created_at) VALUES(?,?,?,?)",
                    (cid, "已约初面", "HR 手动更新", datetime.now().isoformat(timespec="seconds")))
        con.commit()
        notices.scan()
        rows = notices.list_for(cid)
        check(len(rows) == base + 1, "新增 1 条通知", f"实际 {len(rows)}")
        top = rows[0]
        check(top["kind"] == "status_change", "kind=status_change")
        check("已约初面" in top["title"], "标题含新状态", top["title"])
        check("参加初面" in top["body"], "正文带下一步指引", top["body"])
        check("HR 手动更新" not in top["body"], "内部默认备注不外泄")
        check("。。" not in top["body"], "正文无重复句号", top["body"])

        print("\n[2] 重复扫描 -> 不重复推送")
        for _ in range(3):
            notices.scan()
        n2 = len(notices.list_for(cid))
        check(n2 == base + 1, "扫 3 次不重复推送", f"实际 {n2}")

        print("\n[3] 同一状态重复保存 -> 不打扰")
        con.execute("INSERT INTO status_history(candidate_id,status,note,created_at) VALUES(?,?,?,?)",
                    (cid, "已约初面", "HR 手动更新", datetime.now().isoformat(timespec="seconds")))
        con.commit()
        notices.scan()
        n3 = len(notices.list_for(cid))
        check(n3 == base + 1, "状态无实质变化不推", f"实际 {n3}")

        print("\n[4] 待办态停滞 -> 产生提醒，且冷却分桶")
        old = (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds")
        con.execute("UPDATE candidates SET status='待传面试资料' WHERE id=?", (cid,))
        con.execute("INSERT INTO status_history(candidate_id,status,note,created_at) VALUES(?,?,?,?)",
                    (cid, "待传面试资料", "HR 手动更新", old))
        con.commit()
        r = notices.scan()
        stale = [x for x in notices.list_for(cid) if x["kind"] == "stale_todo"]
        check(len(stale) == 1, "产生 1 条停滞提醒", f"实际 {len(stale)}")
        check(stale and "已 4 天" in stale[0]["title"], "标题含停留天数",
              stale[0]["title"] if stale else "")
        check(stale and "尚未上传" in stale[0]["body"], "正文点明卡在哪一步",
              stale[0]["body"] if stale else "")
        # 再把日期往前挪 3 天（第 7 天）应落进下一个桶 -> 新增一条
        older = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
        con.execute("UPDATE status_history SET created_at=? WHERE candidate_id=? AND status='待传面试资料'",
                    (older, cid))
        con.commit()
        notices.scan()
        stale2 = [x for x in notices.list_for(cid) if x["kind"] == "stale_todo"]
        check(len(stale2) == 2, "跨过冷却后再次提醒", f"实际 {len(stale2)}")

        print("\n[5] 未读计数与已读")
        unread = notices.unread_count(cid)
        total = len(notices.list_for(cid))
        check(unread == total, "未读数 = 全部", f"{unread}/{total}")
        nid = notices.list_for(cid)[0]["id"]
        notices.mark_read(cid, ids=[nid])
        check(notices.unread_count(cid) == total - 1, "标记一条后未读 -1")
        notices.mark_read(cid, all_=True)
        check(notices.unread_count(cid) == 0, "全部已读后归零")
    finally:
        print("\n[6] 清理测试数据")
        con.execute("DELETE FROM notices WHERE candidate_id=?", (cid,))
        con.execute("DELETE FROM status_history WHERE candidate_id=?", (cid,))
        con.execute("DELETE FROM candidates WHERE id=?", (cid,))
        con.execute("DELETE FROM messages WHERE candidate_id=?", (cid,))
        con.commit()
        left = con.execute("SELECT COUNT(*) c FROM candidates WHERE phone=?", (phone,)).fetchone()["c"]
        check(left == 0, "测试候选人已清除")
        con.close()

    print("\n" + ("=" * 46))
    if FAILED:
        print(f"冒烟失败 {len(FAILED)} 项：" + "；".join(FAILED))
        return 1
    print("主动推送冒烟全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
