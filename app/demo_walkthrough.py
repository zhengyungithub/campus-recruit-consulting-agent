"""演示脚本：走**真实 SSE 接口**跑一条多轮业务对话，把 Agent 的每一步摊开给你看。

用途：给人讲这套系统怎么工作，或者改完链路后肉眼确认行为是否还正常。
和另外两个脚本的分工：
    smoke_notices.py   断言式（要停服务，测内部逻辑，不依赖模型）
    e2e_notices.py     断言式（要服务在跑，测推送的验收口径）
    demo_walkthrough   观察式（要服务在跑，不打分，只把轨迹打出来）

跑法：
    cd app && python demo_walkthrough.py          # 默认用候选人 #3
    cd app && python demo_walkthrough.py 9        # 指定候选人

跑完会把本次新增的 messages 删掉，不留演示痕迹（工单/会话不主动建，若模型建了需手工清）。
"""
import json
import sqlite3
import sys
import urllib.request

from agent.config import DB_PATH

BASE = "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
QUESTIONS = ["复试要准备些什么？", "那 offer 什么时候发？", "如果那天我去不了，能改期吗？"]


def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with OPENER.open(req, timeout=60) as r:
        return json.loads(r.read().decode())


def stream(token, msg):
    req = urllib.request.Request(BASE + "/api/chat/agent/stream",
                                 data=json.dumps({"message": msg}).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    evs, etype = [], None
    with OPENER.open(req, timeout=90) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    d = json.loads(line[5:].strip())
                except Exception:
                    continue
                if isinstance(d, dict):
                    d.setdefault("type", etype)
                evs.append(d)
    return evs


def show(msg, evs):
    print("\n" + "=" * 70)
    print("候选人：", msg)
    print("=" * 70)
    for e in evs:
        t = e.get("type")
        if t == "intent":
            print(f"  [意图] {e.get('intent')} · {e.get('label')} (来源 {e.get('source')}, 置信 {e.get('confidence')}) {e.get('reason') or ''}")
        elif t == "plan":
            steps = [s if isinstance(s, str) else s.get("tool") for s in (e.get("steps") or [])]
            print(f"  [规划] {steps}  由 {e.get('source')} 生成")
        elif t == "tool":
            print(f"  [调用] {e.get('name')} 参数={json.dumps(e.get('args'), ensure_ascii=False)}")
        elif t == "tool_result":
            print(f"  [返回] {e.get('name')} -> {(e.get('summary') or '')[:110]}")
        elif t == "verify":
            print(f"  [核验] {e.get('status')} (ratio={e.get('ratio')}) {e.get('reasons') or ''}")
        elif t in ("memory", "citation"):
            print(f"  [{t}] {json.dumps(e, ensure_ascii=False)[:140]}")
        elif t == "degrade":
            print(f"  [降级] {json.dumps(e, ensure_ascii=False)[:120]}")
    ans = "".join(e.get("text", "") for e in evs if e.get("type") == "delta")
    if ans:
        print("\n  >>> 回答：")
        print("      " + ans.replace("\n", "\n      "))
    fin = [e for e in evs if e.get("type") == "done"]
    if fin:
        print("\n  [完成]", json.dumps(fin[-1], ensure_ascii=False)[:200])


def main():
    cid_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    c = con.execute("SELECT id, name, phone, status FROM candidates WHERE id=?", (cid_arg,)).fetchone()
    if not c:
        print(f"候选人 #{cid_arg} 不存在")
        return 1
    cid, phone, name, st = c["id"], c["phone"], c["name"], c["status"]
    # 起始水位线：跑完只删本次新增的 messages（第一条新增的 id 就是 max_mid+1）
    max_mid = con.execute("SELECT COALESCE(MAX(id),0) m FROM messages").fetchone()["m"]
    print(f"候选人 #{cid} {name}（{phone}）当前状态「{st}」")

    code = call("POST", "/api/login/request_code", body={"phone": phone})["demo_code"]
    tok = call("POST", "/api/login/verify", body={"phone": phone, "code": code})["token"]

    for m in QUESTIONS:
        show(m, stream(tok, m))

    con.execute("DELETE FROM messages WHERE id > ?", (max_mid,))
    con.commit()
    print(f"\n[清理] 删除本次新增的 messages（id > {max_mid}）")
    con.close()
    return 0


if __name__ == "__main__":
    main()
