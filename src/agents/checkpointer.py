"""
LangGraph Checkpointer（图执行态持久化）
给 `graph.compile(checkpointer=...)` 提供可中断、可恢复、可回溯的**执行态快照**存储。

**定位边界（重要，别和"记忆"混淆）**：
- **checkpoint 不是事实源**。它是"一次请求内 9 个 Agent 的结构化中间状态（AgentState）"的快照，
  **可丢弃、可重建**——删掉它，系统行为完全不变，只是失去了中断恢复与回溯能力。
- **对话记录的事实源仍是 MySQL**（`src/history/store.py`，冷热双层）。跨轮连续性由**短期记忆**
  （`load_context` → `history` 注入提示词）负责，**不是**靠 checkpoint 把上一轮 AgentState 带过来。
  讲义 day05 §6.7「存储 ≠ 记忆」「三层记忆」的口径与此一致，改动前请先读那一节。
- 为什么不能靠 checkpoint 做跨轮记忆：`AgentState` 有 10 个 `operator.add` 累加字段
  （symptoms/departments/medications/advice/disclaimers/warnings/retrieved_entities/
  retrieved_contexts/linked_entities/messages，见 `state.py`）。同一 thread 上再次 invoke，
  LangGraph 会把新一轮的结果**追加**到上一轮——症状、免责声明全部翻倍。所以本项目采用
  **"一轮一线程 + 轮首清空"**：每个新提问开始时清掉上一轮执行态（见 `begin_turn`）。

**thread_id 规则（R1 单一事实来源：本函数是唯一主人）**：
    thread_id = f"user_{user_id}__session_{session_id}"
必须含 user_id——否则用户 B 只要猜到 A 的 session_id（前端存 localStorage），
就能 resume 到 A 中断的图并读到 A 的医疗中间态。归属裁决仍由 `src/auth/deps.py` 那三道防线兜底，
这里是第一道防线（键本身就不可越权）。

**已知坑（可作为课堂排障案例）**：
1. 挂上 checkpointer 后，**任何** invoke/stream 都必须带 `configurable.thread_id`，
   否则 `ValueError: Checkpointer requires one or more of the following 'configurable' keys: thread_id...`。
   所以 `run()` / `run_stream()` 在不传 thread_id 时自动生成临时线程（评测/脚本路径），别直接调 `app.invoke`。
2. API 层跑在 `run_in_executor` / `asyncio.to_thread` 的**线程池**里，SQLite 连接默认禁止跨线程使用，
   必须 `check_same_thread=False`；`SqliteSaver` 内部自带 `threading.Lock` 串行化游标、
   `setup()` 里已开 `PRAGMA journal_mode=WAL`，因此**全进程共用一个连接对象**即可。
3. checkpoint 文件必须进 `.gitignore`（运行时产物，且含用户医疗问答内容）。
"""

import sys
import threading
import uuid
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import sqlite3

from config.settings import settings
from config.paths import DATA_CHECKPOINTS_DIR
from src.common.logger import setup_logger

logger = setup_logger(__name__, "agents.log")

# 评测/脚本等无用户上下文时的线程 id 前缀（run 结束后由 end_turn 删除，避免库无限增长）
ANON_THREAD_PREFIX = "anon:"

_saver = None
_saver_lock = threading.Lock()


# ============================================================
# thread_id 契约（R1：唯一主人就是本函数）
# ============================================================

def build_thread_id(user_id: int, session_id: str) -> str:
    """构造 LangGraph thread_id —— 用户隔离的执行态线程。

    Args:
        user_id: 认证用户主键（来自 `src/auth/deps.py` 的 User.id，不可由前端伪造）
        session_id: 会话 id（= conversation_id，一个 id 串三层，见讲义 day05 §6.4）

    Returns:
        f"user_{user_id}__session_{session_id}"
    """
    return f"user_{user_id}__session_{session_id}"


def new_anon_thread_id() -> str:
    """为无用户上下文的调用（评测 harness、模块自测、命令行）生成一次性线程 id。"""
    return f"{ANON_THREAD_PREFIX}{uuid.uuid4().hex[:12]}"


def is_anon_thread(thread_id: str) -> bool:
    """是否为一次性临时线程（评测/脚本路径）。"""
    return str(thread_id).startswith(ANON_THREAD_PREFIX)


def make_run_config(thread_id: str, callbacks: list | None = None) -> dict:
    """组装 LangGraph 运行配置：thread_id（必填）+ 可选 tracing callbacks。

    Returns:
        {"configurable": {"thread_id": thread_id}, "callbacks": [...]}
        —— callbacks 为空时不带该键，保持与旧调用完全一致的语义。
    """
    config: dict = {"configurable": {"thread_id": thread_id}}
    if callbacks:
        config["callbacks"] = callbacks
    return config


# ============================================================
# Checkpointer 单例
# ============================================================

def get_checkpointer():
    """获取进程级 SqliteSaver 单例（禁用时返回 None → graph.compile() 回到无持久化行为）。

    返回 None 时 `src/agents/graph.py` 走 `compile()` 老路径，整套代码可无损降级，
    这也是一个可讲的工程习惯：**新能力配开关，开关关闭即回旧行为**。
    """
    global _saver
    if not settings.checkpoint_enabled:
        return None
    if _saver is not None:
        return _saver

    with _saver_lock:
        if _saver is None:
            from langgraph.checkpoint.sqlite import SqliteSaver

            db_path = Path(settings.checkpoint_db_path)
            if not db_path.is_absolute():
                db_path = _project_root / db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)

            # 线程池共享连接：check_same_thread=False（见模块头「已知坑 2」）
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            _saver = SqliteSaver(conn)
            logger.info(f"[Checkpoint] SqliteSaver ready at {db_path}")
    return _saver


def describe_thread(thread_id: str) -> str:
    """供日志/自测使用的简短描述：文件路径 + 该线程是否存在快照。"""
    saver = get_checkpointer()
    if saver is None:
        return "disabled"
    try:
        tup = saver.get_tuple(make_run_config(thread_id))
        return f"{'hit' if tup else 'empty'} @ {Path(settings.checkpoint_db_path).as_posix()}"
    except Exception as e:  # noqa: BLE001 - 描述性函数不得抛错影响主链路
        logger.warning(f"[Checkpoint] describe_thread failed: {e}")
        return f"error: {e}"


# ============================================================
# 轮次生命周期
# ============================================================

def begin_turn(thread_id: str) -> bool:
    """新一轮提问开始时清空该线程上一轮的**执行态**快照。

    为什么必须清：`AgentState` 的累加字段（`operator.add`）会在同 thread 二次 invoke 时
    把上一轮的 symptoms/disclaimers/messages **追加**进来（实测翻倍），既是数据错误也是医疗风险
    （上一轮的建议会混进这一轮答案）。

    为什么安全：删的只是执行态快照，对话记录在 MySQL/Redis（事实源，不动）。
    代价：失去跨轮 time-travel（每轮独立线程快照），这是**刻意的取舍**，
    与「工作记忆只活在单次请求内」的既有设计口径保持一致。

    ⚠️ HITL（人在回路，后续迭代）**不能**调用本函数：resume 要接着跑中断时的那份快照。
       约定：新提问 → begin_turn；恢复中断 → 直接 `Command(resume=...)`，不清。

    Returns:
        True 表示确有旧快照被清除（便于日志与自测断言）。
    """
    saver = get_checkpointer()
    if saver is None:
        return False
    try:
        existed = saver.get_tuple(make_run_config(thread_id)) is not None
        if existed:
            saver.delete_thread(thread_id)
            logger.info(f"[Checkpoint] begin_turn: cleared stale state for {thread_id}")
        return existed
    except Exception as e:  # noqa: BLE001 - 执行态清理失败不可阻断问诊主链路
        logger.warning(f"[Checkpoint] begin_turn failed for {thread_id}: {e}")
        return False


def end_turn(thread_id: str) -> None:
    """请求结束：一次性线程（评测/脚本）用完即删，避免 checkpoint 库无界增长。"""
    if not is_anon_thread(thread_id):
        return
    saver = get_checkpointer()
    if saver is None:
        return
    try:
        saver.delete_thread(thread_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Checkpoint] end_turn failed for {thread_id}: {e}")


def prune_threads(thread_id_prefix: str = ANON_THREAD_PREFIX) -> int:
    """按前缀批量清理残留线程（运维钩子/自测用；正常路径下 end_turn 已即时清理）。

    Returns:
        实际删除的 thread 数
    """
    saver = get_checkpointer()
    if saver is None:
        return 0
    try:
        # 只用公开 API（saver.list + delete_thread），不碰 SqliteSaver 的内部表结构，
        # 否则升级 langgraph-checkpoint 版本就会静默失效。
        tids: set[str] = set()
        for tup in saver.list(None):
            tid = str((tup.config.get("configurable", {}) or {}).get("thread_id", ""))
            if tid.startswith(thread_id_prefix):
                tids.add(tid)
        for tid in tids:
            saver.delete_thread(tid)
        if tids:
            logger.info(f"[Checkpoint] pruned {len(tids)} thread(s) with prefix '{thread_id_prefix}'")
        return len(tids)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Checkpoint] prune failed: {e}")
        return 0


# ============================================================
# Main: Self-test（零 LLM、零外部依赖，可在无 API Key 环境跑）
#   验收命令：python src/agents/checkpointer.py
#   证明四件事：
#     1) thread_id 契约（含 user_id 隔离，R1 唯一主人）
#     2) checkpoint 落盘 —— 关连接再重开，状态仍在（=「重启不丢」）
#     3) 累加事故的真实复现 + begin_turn 修复它（本轮症状不混入上一轮）
#     4) 评测路径（无 thread_id）不留垃圾行
# ============================================================

if __name__ == "__main__":
    import tempfile
    import operator
    from typing import TypedDict, Annotated

    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.sqlite import SqliteSaver

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 64)
    print("CHECKPOINT SELF-TEST")
    print("=" * 64)

    failures = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        print(f"  {'✅ PASS' if passed else '❌ FAIL'} {name}" + (f"  {detail}" if detail else ""))
        if not passed:
            failures.append(name)

    # ---- 1) thread_id 契约 ----
    print("\n[1] thread_id 契约")
    tid = build_thread_id(7, "sess-abc")
    check("格式 user_{uid}__{session}", tid == "user_7__session_sess-abc", tid)
    check("含 user_id（用户隔离第一道防线）", "user_7" in tid)
    check("不同用户同 session_id 不同线程",
          build_thread_id(1, "x") != build_thread_id(2, "x"))
    check("anon 线程可识别", is_anon_thread(new_anon_thread_id()) and not is_anon_thread(tid))
    cfg = make_run_config(tid, callbacks=["__sentinel__"])
    check("make_run_config 带 configurable.thread_id",
          cfg["configurable"]["thread_id"] == tid and cfg["callbacks"] == ["__sentinel__"])
    check("无 callbacks 时不塞空键", "callbacks" not in make_run_config(tid))

    # ---- 用最小图 + 临时库跑，避免 import src.agents.graph（那会构造 9 个 Agent、需要 API Key）----
    class MiniState(TypedDict, total=False):
        turn: int
        symptoms: Annotated[list[str], operator.add]   # 复刻本项目的累加语义

    def _node(state: MiniState) -> dict:
        return {"symptoms": ["头痛"], "turn": state.get("turn", 0) + 1}

    def _mini_app(saver):
        g = StateGraph(MiniState)
        g.add_node("n", _node)
        g.add_edge(START, "n")
        g.add_edge("n", END)
        return g.compile(checkpointer=saver)

    tmpdir = tempfile.mkdtemp(prefix="smc_ckpt_")
    db_file = str(Path(tmpdir) / "ckpt.sqlite")

    # ---- 2) 落盘持久化（模拟"重启不丢"）----
    print("\n[2] 执行态落盘 + 重启不丢")
    conn1 = sqlite3.connect(db_file, check_same_thread=False)
    app1 = _mini_app(SqliteSaver(conn1))
    r1 = app1.invoke({"symptoms": []}, config={"configurable": {"thread_id": tid}})
    check("首轮产出", r1["symptoms"] == ["头痛"] and r1["turn"] == 1, str(r1))
    conn1.close()   # 进程结束

    conn2 = sqlite3.connect(db_file, check_same_thread=False)
    saver2 = SqliteSaver(conn2)
    tup = saver2.get_tuple({"configurable": {"thread_id": tid}})
    check("重开连接后仍能读回快照（重启不丢）", tup is not None)
    check("快照内容是上一轮执行态",
          tup is not None and tup.checkpoint["channel_values"].get("symptoms") == ["头痛"])

    # ---- 3) 累加事故复现 + begin_turn 语义 ----
    print("\n[3] 累加事故复现与轮首清空")
    app2 = _mini_app(saver2)
    dirty = app2.invoke({"symptoms": []}, config={"configurable": {"thread_id": tid}})
    check("不清就会翻倍（这正是必须 begin_turn 的证据）",
          dirty["symptoms"] == ["头痛", "头痛"], str(dirty["symptoms"]))
    saver2.delete_thread(tid)          # 等价于 begin_turn 的底层动作
    clean = app2.invoke({"symptoms": []}, config={"configurable": {"thread_id": tid}})
    check("清空后本轮状态干净",
          clean["symptoms"] == ["头痛"] and clean["turn"] == 1, str(clean))

    # ---- 4) get_checkpointer 单例 / 开关 / 主链路一致性 ----
    print("\n[4] 进程级单例与配置契约")
    s1, s2 = get_checkpointer(), get_checkpointer()
    check("get_checkpointer 单例", s1 is s2 and s1 is not None)
    check("配置项存在（checkpoint_enabled / checkpoint_db_path）",
          isinstance(settings.checkpoint_enabled, bool) and bool(settings.checkpoint_db_path))
    check("库文件落在 data/checkpoints/ 下",
          DATA_CHECKPOINTS_DIR.exists()
          and str(Path(settings.checkpoint_db_path).resolve()).startswith(str(DATA_CHECKPOINTS_DIR)))
    n_anon = prune_threads()
    check("评测路径线程可被清理（默认已即时清理，无残留）", n_anon == 0, f"pruned={n_anon}")

    # ---- 5) 真实图是否真的挂上了 checkpointer（有 API Key 时才跑）----
    print("\n[5] 主链路挂载检查（可选，需 MODEL_API_KEY）")
    try:
        from src.agents.graph import app as real_app
        check("graph.compile() 已挂 checkpointer",
              getattr(real_app, "checkpointer", None) is not None,
              describe_thread(build_thread_id(999, "probe")))
    except Exception as e:  # noqa: BLE001
        print(f"  ⏭  SKIP 未构造真实图（{type(e).__name__}: {str(e)[:80]}）")

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 64)
    if failures:
        print(f"[FAILED] {len(failures)} 项未通过：{failures}")
        sys.exit(1)
    print("[SUCCESS] Checkpoint 契约全部通过")
    print("=" * 64)
