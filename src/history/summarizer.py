"""
增量摘要器（token 级上下文控制，讲义 §6.6）

职责：当会话未摘要原文的总 token 超出预算（settings.history_max_tokens）时，
把最旧的一批轮次滚动合并进 conversations.summary，并推进水位
conversations.summary_msg_id。被摘要的原始消息**不删除**（医疗审计价值），
只是不再进入 LLM 上下文窗口。

触发与执行模型：
- 由 store.persist_turn 在 assistant 轮落库后调用 schedule_summarization；
- 后台守护线程执行，**不阻塞问诊主链路**（摘要是优化项，失败仅降级，
  下一轮会再次触发；读路径的 token 滑窗始终兜底提示词不超预算）；
- 每会话一把锁：并发触发只跑一个任务，其余直接跳过。

医疗安全设计：摘要提示词把「过敏史 / 慢性病史 / 正在用药 / 急症信号 /
用户声明的禁忌」列为最高优先级保留项——宁可摘要长一点，不可丢安全信息。
"""

import threading

from langchain_core.messages import HumanMessage, SystemMessage

from config.settings import settings
from src.common.llm import get_llm
from src.common.logger import setup_logger
from src.common.tokens import count_turns_tokens

logger = setup_logger(__name__, "history.log")


SYSTEM_PROMPT = """你是医疗问诊记录的归档助手，负责把多轮对话滚动合并成一份简明摘要，
供后续轮次作为背景上下文使用。原则：
1. 最高优先级保留医疗安全信息：过敏史、慢性病史、正在使用的药物、急症/危险信号、
   用户明确声明的禁忌与特殊情况（如妊娠、肝肾功能异常）；
2. 保留问诊主线：主诉、症状演变（时间/部位/程度）、已给出的检查与用药建议、就诊科室建议；
3. 删除寒暄、重复表述与与问诊无关的内容；
4. 客观转述，绝不添加对话中没有的信息；不要出现"用户问/助手答"这类对话体，
   直接陈述事实。"""

USER_TEMPLATE = """【已有摘要】
{old}

【新增对话】
{new}

请把已有摘要与新增对话合并为一份更新后的摘要，不超过 {max_chars} 字。
只输出摘要正文，不要任何前缀、解释或标题。"""


# ============================================================
# 会话级并发控制
# ============================================================

_session_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def schedule_summarization(store, session_id: str) -> None:
    """触发一次后台增量摘要检查；同会话已有任务在跑则直接跳过。"""
    with _locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
    if not lock.acquire(blocking=False):
        return  # 该会话已有摘要任务在跑（本轮检查跳过，下一轮会再触发）

    def job():
        try:
            _maybe_summarize(store, session_id)
        except Exception as e:
            # 摘要失败不影响主链路：原文仍全量在 MySQL，读路径滑窗兜底
            logger.warning(f"[SUMMARY] Background summarization failed (degraded): {e}")
        finally:
            lock.release()

    threading.Thread(
        target=job, daemon=True, name=f"summarize-{session_id[:8]}"
    ).start()


# ============================================================
# 核心逻辑
# ============================================================

def _maybe_summarize(store, session_id: str) -> None:
    """检查预算 → 切分（保留最近 N 轮原文）→ 增量摘要 → 回写水位。"""
    summary, water_id = store.get_summary_meta(session_id)
    rows = store.get_unsummarized_rows(session_id, water_id)
    turns = store._rows_to_turns(rows)

    min_keep = settings.history_summary_min_keep
    # 触发条件：轮次多于保底数 且 未摘要原文总 token 已超预算
    if len(turns) <= min_keep or count_turns_tokens(turns) <= settings.history_max_tokens:
        return

    # 切分：最近 min_keep 轮继续以原文进上下文，更早的全部卷入摘要
    overflow = turns[:-min_keep]
    overflow_ids = [r["id"] for r in rows[:-min_keep]]
    if not overflow:
        return

    new_summary = summarize_incremental(summary, overflow)
    if not new_summary:
        logger.warning(f"[SUMMARY] Empty summary returned, skip: session={session_id}")
        return

    store.save_summary(session_id, new_summary, max(overflow_ids))


def summarize_incremental(old_summary: str, new_turns: list[dict]) -> str:
    """调用 LLM 把旧摘要 + 新增轮次合并为新摘要（失败抛出，由调用方降级）。"""
    from src.common.memory import format_history  # 延迟导入，避免模块循环

    target_chars = max(200, settings.history_summary_max_chars // 2)
    prompt = USER_TEMPLATE.format(
        old=old_summary or "（暂无，这是首次摘要）",
        new=format_history(new_turns),
        max_chars=target_chars,
    )
    llm = get_llm(temperature=0.1)  # 摘要为结构化任务，低温度
    resp = llm.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
    text = (resp.content or "").strip()
    # 硬截断保险丝：提示词已限制字数，此为失控生成的最后防线
    return text[: settings.history_summary_max_chars]
