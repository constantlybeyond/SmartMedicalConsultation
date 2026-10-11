"""
Observability hook（env-gated, no-op safe）——生产路径与离线评估共用。

优先级：LANGFUSE_* → Langfuse；否则 LANGSMITH_TRACING → LangSmith；否则 no-op。
- trace_span：请求/用例级业务 span，携带 intent / tool_calls / latency 等元数据；
- get_callbacks：Agent 层 LangChain callbacks——
  Langfuse 模式返回 CallbackHandler（补内层 LLM 调用树）；
  LangSmith 模式返回 []（env 开启后 langchain 自动 trace，原生带嵌套调用树）。

所有后端调用都 try/except 包裹，**绝不**因可观测失败而打断请求/评估；
flush 绝不在请求路径上做同步网络调用（LangSmith 靠 env 自动 trace，
Langfuse 后台线程上报）——可观测可以慢，主链路不能等。
"""

import os
import contextlib
import threading
from pathlib import Path

# 项目根 .env 载入 os.environ：observe / langsmith / langfuse SDK 都经 os.getenv 读配置，
# 而 pydantic-settings 只把 .env 注入 Settings 对象、不进 os.environ——不补这步 .env 配置不生效。
_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
except Exception:
    pass

from src.common.logger import setup_logger

logger = setup_logger(__name__, "observe.log")


def _backend():
    """返回 (backend_name, handle)；不可用则 ('noop', None)。"""
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
        try:
            from langfuse import Langfuse  # type: ignore
            return "langfuse", Langfuse()
        except Exception as e:
            logger.warning(f"[OBSERVE] Langfuse 初始化失败，降级 no-op：{e}")
    if os.getenv("LANGSMITH_TRACING", "").lower() in ("1", "true", "yes"):
        try:
            import langsmith  # type: ignore  # 设置该 env 后 langchain 调用会自动 trace
            return "langsmith", langsmith
        except Exception as e:
            logger.warning(f"[OBSERVE] Langsmith 不可用，降级 no-op：{e}")
    return "noop", None


class _Span:
    def __init__(self, backend, handle, name):
        self.backend = backend
        self.handle = handle
        self.name = name
        self.meta = {}

    def update(self, **kw):
        self.meta.update(kw)

    def flush(self):
        """红线：**绝不在请求路径上做同步网络调用**。

        - LangSmith：LANGSMITH_TRACING 开启后 langchain 调用已自动 trace 整棵
          调用树，无需再补发顶层 run——此前同步 `Client().create_run()` 在
          LangSmith 端点不可达时会卡住连接超时+重试（实测阻塞响应 20s+，
          缓存命中请求 38ms 变 24s），且会在控制台产生 Legacy API 告警，故移除。
          业务元数据（intent/耗时）由 api.log 的 [REQUEST] 行承载，不丢。
        - Langfuse：放后台守护线程上报，主链路零等待；失败仅 debug 日志。
        """
        b, h = self.backend, self.handle
        if b == "noop" or h is None or b == "langsmith":
            return
        if b == "langfuse":
            name, meta = self.name, self.meta

            def _bg():
                try:
                    try:
                        h.trace(name=name, metadata=meta)         # langfuse v2
                    except Exception:
                        try:
                            h.create_trace(name=name, metadata=meta)  # langfuse v3
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"[OBSERVE] flush ignored: {e}")

            threading.Thread(target=_bg, name="observe-flush", daemon=True).start()


@contextlib.contextmanager
def trace_span(name: str, **meta):
    """可观测 span 上下文管理器；无后端时为 no-op。"""
    backend, handle = _backend()
    span = _Span(backend, handle, name)
    span.meta.update(meta)
    try:
        yield span
    finally:
        try:
            span.flush()
        except Exception:
            pass


def get_callbacks() -> list:
    """按后端返回 LangChain callbacks；noop / langsmith 返回空列表。

    LangSmith 不需要 callbacks——LANGSMITH_TRACING 开启后 langchain 自动 trace
    整棵调用树；Langfuse 需要显式 CallbackHandler 才有内层调用树。
    """
    backend, handle = _backend()
    if backend == "langfuse" and handle is not None:
        try:
            try:
                from langfuse.langchain import CallbackHandler  # langfuse v3
            except Exception:
                from langfuse.callback import CallbackHandler  # langfuse v2
            return [CallbackHandler()]
        except Exception as e:
            logger.warning(f"[OBSERVE] Langfuse CallbackHandler 不可用，内层调用树降级：{e}")
    return []
