"""提示词外置与版本管理（第 4 期 S1.1）

为什么外置：改提示词是本项目的**最高频变更**，此前 SYSTEM_PROMPT 硬编码在 graph.py、
plan_prompt 在 planner.py、JUDGE_PROMPT 在 verifier.py。三处分散带来的问题是：
改完之后无法从 `agent_runs` 里区分这条轨迹是哪一版提示词产生的，
评测对比也就无从归因——到底是提示词变好了，还是模型抽风。

版本规则：
- 按 name 各自独立递增（1, 2, 3...），用 content_hash 判定是否真的变了。
- **内容未变时重启不产生新版本**（避免每次重启刷一堆无意义版本）。
- 运行时把三个提示词的版本号组成指纹写进 agent_runs：`sys2-plan1-judge3`。

回滚：`rollback(name, version)` 会把历史内容**写回文件**再重新同步，
保证回滚后的状态能跨重启保持，而不是只在内存里临时生效。
"""
import hashlib
import os
from datetime import datetime

from ..store import get_conn

_DIR = os.path.dirname(os.path.abspath(__file__))
NAMES = ("system", "plan_llm", "judge")

_content_cache = {}
_version_cache = {}


def path_of(name: str) -> str:
    if name not in NAMES:
        raise ValueError(f"未知提示词: {name}，可选 {NAMES}")
    return os.path.join(_DIR, name + ".txt")


def load(name: str) -> str:
    """读取提示词原文（带进程内缓存）。"""
    if name not in _content_cache:
        with open(path_of(name), encoding="utf-8") as f:
            _content_cache[name] = f.read()
    return _content_cache[name]


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def ensure_table():
    with get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS prompt_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            version INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0)""")


def sync() -> str:
    """启动时调用：内容变了就插新版本并置为 active，没变就复用当前版本。

    返回提示词指纹，如 `sys2-plan1-judge3`。
    """
    ensure_table()
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        for name in NAMES:
            content = load(name)
            digest = _sha1(content)
            row = conn.execute(
                "SELECT version, content_hash FROM prompt_versions "
                "WHERE name=? AND active=1 ORDER BY version DESC LIMIT 1",
                (name,)).fetchone()
            if row and row["content_hash"] == digest:
                _version_cache[name] = row["version"]
                continue
            nxt = (row["version"] if row else 0) + 1
            conn.execute("UPDATE prompt_versions SET active=0 WHERE name=?", (name,))
            conn.execute("INSERT INTO prompt_versions"
                         "(name,version,content_hash,content,created_at,active) "
                         "VALUES(?,?,?,?,?,1)",
                         (name, nxt, digest, content, now))
            _version_cache[name] = nxt
    return fingerprint()


def _active_version(name: str) -> int:
    """优先取进程内缓存；未 sync 的进程回查数据库（评测/脚本进程也能拿到正确版本）。"""
    v = _version_cache.get(name)
    if v:
        return v
    with get_conn() as conn:
        row = conn.execute(
            "SELECT version FROM prompt_versions WHERE name=? AND active=1 "
            "ORDER BY version DESC LIMIT 1", (name,)).fetchone()
    v = row["version"] if row else 0
    _version_cache[name] = v
    return v


def fingerprint() -> str:
    """当前生效的提示词版本指纹，写进 agent_runs 用于归因。"""
    return "sys{s}-plan{p}-judge{j}".format(
        s=_active_version("system"),
        p=_active_version("plan_llm"),
        j=_active_version("judge"))


def current() -> dict:
    """各提示词当前生效版本，供管理端展示。"""
    return {n: _active_version(n) for n in NAMES}


def history(name: str = None, limit: int = 50) -> list:
    with get_conn() as conn:
        if name:
            rows = conn.execute(
                "SELECT id,name,version,content_hash,created_at,active "
                "FROM prompt_versions WHERE name=? ORDER BY version DESC LIMIT ?",
                (name, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,name,version,content_hash,created_at,active "
                "FROM prompt_versions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def rollback(name: str, version: int) -> str:
    """把指定历史版本的内容写回文件并重新同步，返回新指纹。"""
    path_of(name)  # 先校验 name 合法
    with get_conn() as conn:
        row = conn.execute("SELECT content FROM prompt_versions WHERE name=? AND version=?",
                           (name, version)).fetchone()
    if not row:
        raise ValueError(f"版本不存在：{name} v{version}")
    with open(path_of(name), "w", encoding="utf-8") as f:
        f.write(row["content"])
    _content_cache.pop(name, None)
    return sync()
