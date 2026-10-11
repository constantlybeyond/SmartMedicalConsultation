"""
Token 计数（上下文预算的基础设施，讲义 §6.6）

设计说明：
- 首选 tiktoken（cl100k_base）。它是 OpenAI 系 BPE 分词器，对 Qwen 并非精确匹配，
  但**对中文普遍高估**（cl100k 每汉字约 1-2 token，Qwen 分词器约 0.6-0.9）——
  用于预算控制时高估是**保守方向**（宁可少装几轮，也不超窗口），可接受。
- tiktoken 首次使用需下载 BPE 文件（其后本地缓存）；下载失败/离线环境 →
  自动降级为启发式估算（CJK ≈ 1 token/字，其余 ≈ 1 token/4 字符），功能不中断。
- 计数只用于预算裁剪与摘要触发，不用于计费，误差方向安全即可。
"""

from functools import lru_cache

from src.common.logger import setup_logger

logger = setup_logger(__name__, "tokens.log")


@lru_cache(maxsize=1)
def _get_encoding():
    """惰性加载 tiktoken 编码器；不可用返回 None（调用方走启发式兜底）。"""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        logger.info("[TOKENS] tiktoken cl100k_base ready")
        return enc
    except Exception as e:
        logger.warning(f"[TOKENS] tiktoken unavailable, fallback to heuristic: {e}")
        return None


def _heuristic(text: str) -> int:
    """启发式估算：CJK ≈ 1 token/字（保守），其余 ≈ 1 token/4 字符。"""
    cjk = 0
    for ch in text:
        o = ord(ch)
        # CJK 统一表意文字 + 扩展A + 中文标点
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0x3000 <= o <= 0x303F \
                or 0xFF00 <= o <= 0xFFEF:
            cjk += 1
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def count_tokens(text: str) -> int:
    """估算一段文本的 token 数（空文本为 0）。"""
    if not text:
        return 0
    enc = _get_encoding()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return _heuristic(text)


def count_turns_tokens(turns: list[dict]) -> int:
    """一组历史轮次的总 token 数（只计 content；role/intent 开销忽略，方向保守）。"""
    return sum(count_tokens(t.get("content", "")) for t in turns)


def trim_turns_to_budget(turns: list[dict], max_tokens: int, min_keep: int = 2) -> list[dict]:
    """Token 级滑动窗口：从最新往前装，装满预算即止（替代固定轮次硬截断）。

    - 保证至少保留最近 min_keep 轮（对话连贯性下限，即使其已超预算）；
    - 保持时间正序返回；被裁掉的更旧轮次由增量摘要兜底（见 history/summarizer.py）。
    """
    if not turns or max_tokens <= 0:
        return turns[-min_keep:] if turns else []
    kept: list[dict] = []
    used = 0
    for t in reversed(turns):  # 从最新往前
        cost = count_tokens(t.get("content", ""))
        if kept and used + cost > max_tokens and len(kept) >= min_keep:
            break
        kept.append(t)
        used += cost
    kept.reverse()
    return kept
