"""Agent 模块配置：优先级 DB settings（HR 后台可改，运行时生效）> 环境变量/.env > 内置默认。

动态项（AGENT_MODE / LLM_* / EMBED_*）通过模块级 __getattr__（PEP 562）实现：
调用方仍然写 config.LLM_API_KEY / config.AGENT_MODE，但每次读取都会拿到最新值，
后台保存配置后无需重启服务即可生效（模型实例缓存由 llm_service.reset_models() 清除）。
"""
import os
import sqlite3
from pathlib import Path

APP_DIR = Path(__file__).parent.parent


def _load_dotenv():
    env_file = APP_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# ---- 静态配置 ----
AGENT_SHADOW_RATE = float(os.getenv("AGENT_SHADOW_RATE", "0.5"))
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))

# ---- 存储 ----
QDRANT_PATH = APP_DIR / "qdrant_data"
DB_PATH = APP_DIR / "campus_bot.db"
CKPT_PATH = APP_DIR / "checkpoints.db"

# ---- 校验阈值 ----
VERIFY_PASS = 0.85
VERIFY_FAIL = 0.55
# 第2期：句子级引用锚点判定阈值（一句 ≥ 该比例的字面依据能落在某条原文里，算有据）
VERIFY_SENTENCE_PASS = float(os.getenv("VERIFY_SENTENCE_PASS", "0.34"))

# ---- 第1期：Agent 运行预算（任一超限即降级固定管线，保证用户永远拿得到回答）----
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "5"))          # 最多几轮 LLM 推理
AGENT_MAX_TOOL_CALLS = int(os.getenv("AGENT_MAX_TOOL_CALLS", "8"))  # 最多几次工具调用
AGENT_DEADLINE_S = float(os.getenv("AGENT_DEADLINE_S", "30"))      # 单次运行墙钟上限（秒）

# ---- 动态配置：环境变量/.env 作为兜底默认值 ----
_ENV_DEFAULTS = {
    "AGENT_MODE": os.getenv("AGENT_MODE", "off"),
    "LLM_API_KEY": os.getenv("LLM_API_KEY", ""),
    "LLM_BASE_URL": os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "LLM_MODEL": os.getenv("LLM_MODEL", "qwen-plus"),
    "EMBED_MODEL": os.getenv("EMBED_MODEL", "text-embedding-v3"),
    "AGENT_JUDGE": os.getenv("AGENT_JUDGE", "auto"),
}
# config 属性名 -> settings 表 key
_DYNAMIC_KEYS = {
    "AGENT_MODE": "agent_mode",
    "LLM_API_KEY": "llm_api_key",
    "LLM_BASE_URL": "llm_base_url",
    "LLM_MODEL": "llm_model",
    "EMBED_MODEL": "embed_model",
    "AGENT_JUDGE": "agent_judge",
}

# settings 表允许写入的 key 白名单（含独立 Embedding Key）
# agent_judge：第2期 Answer 质检裁判开关 off / auto（规则卡住才调模型）/ on（每答必裁）
# rate_limit_per_hour：S1.4 用户级限流阈值（0 = 不限流）
# ckpt_retention_days：S2.3 检查点保留天数（0 = 不清理）
SETTING_KEYS = {"agent_mode", "llm_api_key", "llm_base_url", "llm_model",
                "embed_api_key", "embed_model", "agent_planner", "agent_judge",
                "rate_limit_per_hour", "ckpt_retention_days"}


def _db_get(db_key: str):
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT value FROM settings WHERE key=?", (db_key,)).fetchone()
        conn.close()
        return row[0] if row and row[0] != "" else None
    except Exception:
        return None


def get_effective(name: str) -> str:
    """读取动态配置的生效值：DB 覆盖 > 环境变量 > 默认。"""
    db_key = _DYNAMIC_KEYS.get(name)
    if db_key:
        v = _db_get(db_key)
        if v is not None:
            return v
    return _ENV_DEFAULTS.get(name, "")


def get_embed_api_key() -> str:
    """Embedding 独立 Key；未配置时回落到 LLM Key（同一 OpenAI 兼容端点）。"""
    v = _db_get("embed_api_key")
    return v if v else get_effective("LLM_API_KEY")


def __getattr__(name: str):
    if name in _DYNAMIC_KEYS:
        return get_effective(name)
    if name == "EMBED_API_KEY":
        return get_embed_api_key()
    raise AttributeError(f"module 'config' has no attribute '{name}'")


def set_setting(key: str, value: str):
    if key not in SETTING_KEYS:
        raise ValueError(f"不支持的配置项: {key}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()


def get_setting(key: str):
    if key not in SETTING_KEYS:
        raise ValueError(f"不支持的配置项: {key}")
    return _db_get(key)


def llm_ready() -> bool:
    return bool(get_effective("LLM_API_KEY"))


def agent_enabled() -> bool:
    return get_effective("AGENT_MODE") in ("shadow", "on") and llm_ready()
