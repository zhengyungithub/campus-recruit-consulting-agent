"""模型接入：OpenAI 兼容协议（DashScope/通义等），按需惰性创建并缓存"""
import threading

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from . import config

_lock = threading.Lock()
_chat = None
_embed = None


def get_chat_model(temperature: float = 0.3):
    """每个调用场景可传不同 temperature；同一进程内缓存实例。"""
    global _chat
    if not config.llm_ready():
        raise RuntimeError("LLM_API_KEY 未配置")
    with _lock:
        if _chat is None:
            _chat = ChatOpenAI(
                model=config.LLM_MODEL,
                api_key=config.LLM_API_KEY,
                base_url=config.LLM_BASE_URL,
                temperature=temperature,
                timeout=30,
                max_retries=1,          # 一次 OHLC 重试即止：长重试会让整条链路看起来"卡死"
            )
        return _chat


def get_embeddings():
    """text-embedding-v3 (1024维)。
    关键坑：必须 check_embedding_ctx_length=False，否则 DashScope 报 400。
    Embedding 可配置独立 API Key（EMBED_API_KEY），未配置时回落到 LLM Key。
    """
    global _embed
    if not config.llm_ready():
        raise RuntimeError("LLM_API_KEY 未配置")
    with _lock:
        if _embed is None:
            _embed = OpenAIEmbeddings(
                model=config.EMBED_MODEL,
                dimensions=config.EMBED_DIM,
                api_key=config.EMBED_API_KEY,
                base_url=config.LLM_BASE_URL,
                check_embedding_ctx_length=False,
                # 必须设置 timeout：默认无超时，网络挂起时整轮请求会永久阻塞
                # （第1期评测实测出现过单条用例卡近 40 分钟的情况）
                timeout=20,
                max_retries=1,
            )
        return _embed


def reset_models():
    """配置变更后清空模型实例缓存，下次调用按新配置重建（运行时热更新）。"""
    global _chat, _embed
    with _lock:
        _chat = None
        _embed = None
