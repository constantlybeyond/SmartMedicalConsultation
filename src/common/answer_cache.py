"""
答案缓存（三分域，全用户隔离）——生产问诊路径的响应跳过机制
====================================================================
                       Query
                         ↓
                Cacheability Rule（纯正则，微秒级）
                         ↓
         ┌───────────────┴───────────────┐
         ↓                               ↓
   上下文/强个性化                    通用稳定
         ↓                               ↓
   L1 Session 缓存                L1 User 精确
   (su:{user}:{session})                ↓ miss
                                  L2 User 语义
                                        ↓ miss
                                     Agent

| 缓存           | Scope                | 适合场景                     | 跨用户 |
|----------------|----------------------|------------------------------|--------|
| L1 Session     | user_id + session_id | "那平时要注意什么""这个药呢" | ❌     |
| L1 User 精确   | user_id              | 用户重复问相同问题           | ❌     |
| L2 User 语义   | user_id              | 同一用户相似问题             | ❌     |
| Agent          | —                    | 三级全 miss                  | —      |

设计契约（与 TDD「可观测与缓存」章节一致）：
- **医疗数据强制用户隔离**：三把钥匙都含 user_id——L1 Session 键绑定
  (user, session)，L1 User 键与 L2 条目绑定 user。任何一级都绝不跨用户串用。
  同一用户跨会话共享"通用稳定"缓存；"上下文追问"只在其所属会话内复用。
- **Cacheability Rule**（`classify`，纯正则、微秒级、零 LLM/嵌入调用）：
  含指代词（这个/那个/它的…）、接续词开头（那…/还有…）、省略式短问
  （怎么办/严重吗…）→ 判为**上下文/强个性化** → 走 L1 Session；
  其余自包含问题 → **通用稳定** → 走 L1 User 精确 → L2 User 语义。
  误判方向刻意保守：宁可落更低复用域（只损失命中率），绝不跨上下文/跨用户串答。
- **缓存的是完整响应**（answer + intent + disclaimers + ...），命中即合规
  （免责声明随响应一起缓存，医疗红线不丢）。
- **失效靠版本号**：知识库/图谱重建后把 `CACHE_VERSION` +1，三把钥匙与
  L2 文件版本同时失效；L1 TTL 只是兜底。
- **缓存可缺不可幻 + 失败必降级**：所有外部交互（Redis / 嵌入模型 / 磁盘）
  均 try/except，任何缓存故障绝不打断问诊主链路。
- **写入异步**：生成成功后后台线程写对应域，不占请求路径延迟。
- 本项目未做 L3（RAG/Tool 结果缓存）：问诊链路检索开销可控，收益不足以抵消复杂度。
"""

import json
import re
import time
import uuid
import hashlib
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import redis

from config.settings import settings
from config.paths import DATA_DIR
from src.common.logger import setup_logger

logger = setup_logger(__name__, "cache.log")

# ============================================================
# 持久化位置（data/cache/，不入 git）
# ============================================================

CACHE_DIR = DATA_DIR / "cache"
_L2_ENTRIES_FILE = CACHE_DIR / "semantic_cache.json"
_L2_VECTORS_FILE = CACHE_DIR / "semantic_cache.npz"

# 归一化：去空白与常见中英文标点后小写——"头痛挂什么科？"与"头痛挂什么科"同 key
_PUNCT_RE = re.compile(
    r"[\s，。！？；：、~～…,.!?;:\"'“”‘’()（）\[\]{}<>《》【】\-—]+"
)


def _normalize(query: str) -> str:
    return _PUNCT_RE.sub("", (query or "").strip().lower())


# ============================================================
# Cacheability Rule（纯正则，微秒级，零 LLM / 嵌入调用）
# ============================================================
# 目标：把问题路由到正确的缓存域。
# 三类"上下文/强个性化"信号（任一命中 → session 域）：
#   1) 指代词——指向此前对话内容（这个/那个/它的/上述/刚才…）
#   2) 接续词开头——几乎必为话头延续（那…/还有…/另外…/然后…）
#      注：「那」后必须跟典型接续字，避免误伤"那格列奈"等药名
#   3) 省略式短问——没有主语，问的对象只存在于上下文（怎么办/严重吗…）
# 其余视为"通用稳定"（自包含），走 user 域。

_ANAPHORA_RE = re.compile(
    r"这个|那个|这种|那种|这些|那些|它的|它们|上述|上面|前面|之前说|刚才|刚刚|你说的|该病|此病|这种病|那种病|这种情况|那种情况"
)
_LEADING_CONJ_RE = re.compile(
    r"^(那(么|个|该|要|能|需|平时|怎么|吃|用|注意|还|应|可以|算|多久|挂|看)|还有|另外|然后|接着|继续|所以|但是|而且)"
)
_ELLIPTICAL_RE = re.compile(
    r"^(怎么办|咋办|严重吗|要紧吗|吃什么|用什么|注意什么|忌什么|多久能好|能治好吗|会复发吗|挂什么科|吃什么药|怎么治|需要检查吗)$"
)

# 缓存域
SCOPE_SESSION = "session"   # 上下文/强个性化 → L1 Session
SCOPE_USER = "user"         # 通用稳定     → L1 User 精确 → L2 User 语义


def classify(query: str) -> str:
    """Cacheability Rule（最小延迟：一次归一化 + 三次正则匹配，微秒级）。

    返回缓存域：
    - "session"：上下文/强个性化（指代词/接续开头/省略短问）→ 仅 L1 Session；
    - "user"   ：自包含、通用稳定 → L1 User 精确 → L2 User 语义。
    """
    norm = _normalize(query)
    if not norm:
        return SCOPE_SESSION  # 空问题不进任何缓存（后续 lookup 也会早退）
    if _ANAPHORA_RE.search(norm):
        return SCOPE_SESSION
    if _LEADING_CONJ_RE.search(norm):
        return SCOPE_SESSION
    if _ELLIPTICAL_RE.match(norm):
        return SCOPE_SESSION
    return SCOPE_USER


# 兼容旧名（语义：是否自包含/可进 user 域）
def is_cacheable(query: str) -> bool:
    """自包含（可进 user 域）判定；上下文依赖返回 False。保留作向后兼容。"""
    return classify(query) == SCOPE_USER


# ============================================================
# L1：Redis 精确缓存（三域钥匙都含 user_id，强制用户隔离）
# ============================================================

def _l1_key_user(user_id: str, norm: str) -> str:
    """L1 User 精确键：同一用户所有会话共享，绝不跨用户。"""
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()
    return f"ans:{settings.cache_version}:u:{user_id}:{digest}"


def _l1_key_session(user_id: str, session_id: str, norm: str) -> str:
    """L1 Session 键：绑定 (user, session)——上下文追问只在所属会话内复用。"""
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()
    return f"ans:{settings.cache_version}:su:{user_id}:{session_id}:{digest}"


_redis_pool: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    """连接池单例（惰性创建，与 history store 同款约定）。"""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_pool


def _l1_get(key: str) -> dict | None:
    try:
        raw = _get_redis().get(key)
    except Exception as e:
        logger.warning(f"[CACHE] L1 read failed, degraded: {e}")
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _l1_set(key: str, payload: dict) -> None:
    try:
        _get_redis().set(
            key,
            json.dumps(payload, ensure_ascii=False),
            ex=settings.cache_l1_ttl_seconds,
        )
    except Exception as e:
        logger.warning(f"[CACHE] L1 write failed, degraded: {e}")


# ============================================================
# L2：语义缓存（BGE-M3 向量 + 余弦，按 user 过滤，内存索引 + 磁盘持久化）
# ============================================================

_lock = threading.Lock()
_entries: list[dict] = []       # [{"q","u":user_id,"resp","ts"}]
_vectors: np.ndarray | None = None  # float32 (N, 1024)，行已归一化 → 内积即余弦
_l2_loaded = False


def _embed(text: str) -> np.ndarray | None:
    """复用进程级共享检索器的 BGE-M3（API 启动时已预热，这里零额外加载）。

    嵌入不可用（模型缺失/索引未构建等）→ 返回 None，L2 整体降级关闭。
    """
    try:
        from src.vector_store.search import get_shared_retriever
        model = get_shared_retriever()._get_embedding_model()
        vec = model.encode([text], normalize_embeddings=True).astype("float32")
        return vec[0]
    except Exception as e:
        logger.warning(f"[CACHE] embed failed, L2 degraded: {e}")
        return None


def _l2_ensure_loaded() -> None:
    """首次使用时从磁盘载入；版本不匹配视为整体失效（空缓存起步）。

    旧格式（无 "u" 用户字段的历史条目）就地丢弃——无主条目不符合用户隔离。
    """
    global _l2_loaded, _vectors
    if _l2_loaded:
        return
    _l2_loaded = True
    try:
        meta = json.loads(_L2_ENTRIES_FILE.read_text(encoding="utf-8"))
        if meta.get("version") != settings.cache_version:
            logger.info("[CACHE] L2 version mismatch, starting empty (知识库版本已更新)")
            return
        entries = [e for e in meta.get("entries", []) if e.get("u")]
        if entries and _L2_VECTORS_FILE.exists():
            vecs = np.load(_L2_VECTORS_FILE)["vectors"]
            n = min(len(entries), int(vecs.shape[0]))
            _entries[:] = entries[0:n]
            _vectors = vecs[0:n]
            logger.info(f"[CACHE] L2 loaded: {len(_entries)} entries from disk")
    except FileNotFoundError:
        pass  # 首次运行，空缓存起步
    except Exception as e:
        logger.warning(f"[CACHE] L2 load failed, starting empty: {e}")


def _l2_persist_locked() -> None:
    """原子落盘（先写 .tmp 再 replace；调用方须持锁）。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_json = CACHE_DIR / (_L2_ENTRIES_FILE.name + ".tmp")
        tmp_json.write_text(
            json.dumps(
                {"version": settings.cache_version, "entries": _entries},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp_json.replace(_L2_ENTRIES_FILE)
        if _vectors is not None and len(_vectors):
            # 临时文件必须以 .npz 结尾：numpy 否则会自动补后缀，写偏导致 replace 失败
            tmp_npz = CACHE_DIR / (_L2_VECTORS_FILE.stem + "_tmp.npz")
            np.savez_compressed(tmp_npz, vectors=_vectors)
            tmp_npz.replace(_L2_VECTORS_FILE)
    except Exception as e:
        logger.warning(f"[CACHE] L2 persist failed, memory-only degraded: {e}")


def _l2_lookup(vec: np.ndarray, user_id: str) -> tuple[dict, float] | None:
    with _lock:
        _l2_ensure_loaded()
        if _vectors is None or len(_entries) == 0:
            return None
        # 用户隔离：只在本用户的条目里算相似度
        idxs = [i for i, e in enumerate(_entries) if e.get("u") == user_id]
        if not idxs:
            return None
        sims = _vectors[idxs] @ vec
        j = int(np.argmax(sims))
        score = float(sims[j])
        if score >= settings.cache_similarity_threshold:
            return _entries[idxs[j]]["resp"], score
        return None


def _l2_store(norm: str, user_id: str, vec: np.ndarray, payload: dict) -> None:
    global _vectors
    with _lock:
        _l2_ensure_loaded()
        # 同用户同归一化问句已存在 → 更新而非重复插入
        for i, e in enumerate(_entries):
            if e.get("u") == user_id and e["q"] == norm:
                e["resp"] = payload
                e["ts"] = datetime.now().isoformat()
                if _vectors is not None:
                    _vectors[i] = vec
                _l2_persist_locked()
                return
        _entries.append({
            "q": norm, "u": user_id, "resp": payload,
            "ts": datetime.now().isoformat(),
        })
        _vectors = (
            np.vstack([_vectors, vec[None, :]])
            if _vectors is not None and len(_vectors)
            else vec[None, :]
        )
        if len(_entries) > settings.cache_l2_max_entries:  # 容量上限：最旧淘汰（FIFO）
            _entries.pop(0)
            _vectors = _vectors[1:]
        _l2_persist_locked()


# ============================================================
# 对外接口：lookup / store_async（按 Cacheability Rule 路由到对应域）
# ============================================================

def lookup(
    query: str, *, user_id: str, session_id: str
) -> tuple[str, dict] | None:
    """查缓存。返回 (层级, 完整响应 payload)；未命中/关闭/空问返回 None。

    路由（与写入对称）：
    - 上下文/强个性化（session 域）：只查 L1 Session（su:{user}:{session}），
      命中即返；不命中直接走 Agent（绝不做语义匹配，防跨上下文串答）。
    - 通用稳定（user 域）：L1 User 精确（u:{user}）→ miss 再 L2 User 语义。
    """
    if not settings.cache_enabled or not user_id:
        return None
    norm = _normalize(query)
    if not norm:
        return None

    scope = classify(query)
    t0 = time.time()

    if scope == SCOPE_SESSION:
        payload = _l1_get(_l1_key_session(user_id, session_id or "", norm))
        if payload:
            logger.info(
                f"[CACHE] L1-session hit: '{norm[:40]}' user={user_id[0:8]} "
                f"({int((time.time() - t0) * 1000)}ms)"
            )
            return "L1", payload
        return None  # session 域无兜底语义匹配，直接走 Agent

    # SCOPE_USER
    payload = _l1_get(_l1_key_user(user_id, norm))
    if payload:
        logger.info(
            f"[CACHE] L1-user hit: '{norm[:40]}' user={user_id[0:8]} "
            f"({int((time.time() - t0) * 1000)}ms)"
        )
        return "L1", payload

    vec = _embed(query)
    if vec is None:
        return None
    hit = _l2_lookup(vec, user_id)
    if hit:
        payload, score = hit
        logger.info(
            f"[CACHE] L2-user hit: '{norm[:40]}' user={user_id[0:8]} sim={score:.3f} "
            f"({int((time.time() - t0) * 1000)}ms)"
        )
        return "L2", payload
    return None


def store_async(
    query: str, payload: dict, *, user_id: str, session_id: str
) -> None:
    """生成成功后的异步写入（后台守护线程，失败仅记日志）。

    payload = ConsultationResponse.model_dump()（含免责声明的完整响应）。
    按 Cacheability Rule 写入对应域（与 lookup 对称）：
    - session 域 → 只写 L1 Session 键；
    - user 域   → 写 L1 User 键 + L2 语义。
    """
    if not settings.cache_enabled or not user_id:
        return
    norm = _normalize(query)
    if not norm or not payload.get("answer"):
        return

    scope = classify(query)

    def _bg():
        try:
            if scope == SCOPE_SESSION:
                _l1_set(_l1_key_session(user_id, session_id or "", norm), payload)
            else:
                _l1_set(_l1_key_user(user_id, norm), payload)
                vec = _embed(query)
                if vec is not None:
                    _l2_store(norm, user_id, vec, payload)
        except Exception as e:
            logger.warning(f"[CACHE] background write failed (ignored): {e}")

    threading.Thread(target=_bg, name="answer-cache-writer", daemon=True).start()


# ============================================================
# Main: Self-test（不依赖 API 进程）
# ============================================================

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("ANSWER CACHE SELF-TEST (三分域，全用户隔离)")
    print("=" * 60)

    print("[0] Cacheability Rule 分域：")
    cases = [
        ("我最近头痛、头晕，应该挂什么科？", "user"),
        ("感冒吃什么药好", "user"),
        ("那格列奈的副作用", "user"),      # 药名，不可误判为接续词
        ("那平时要注意什么", "session"),
        ("这个药呢", "session"),
        ("它的副作用大吗", "session"),
        ("怎么办", "session"),
    ]
    for q, expect in cases:
        got = classify(q)
        mark = "OK " if got == expect else "FAIL"
        print(f"    [{mark}] '{q}' -> {got}")

    uid = f"selftest-{uuid.uuid4().hex[:6]}"
    s1 = f"sess-{uuid.uuid4().hex[:6]}"
    s2 = f"sess-{uuid.uuid4().hex[:6]}"
    demo = {"answer": "（自测占位答案）", "intent": "test", "disclaimers": ["仅供测试"]}

    # session 域：同会话命中、跨会话不串
    qf = "那平时要注意什么"
    print(f"\n[1] session 域 store(session={s1[0:6]}...) '{qf}'")
    store_async(qf, demo, user_id=uid, session_id=s1)
    time.sleep(2)
    print(f"[2] 同会话再问 -> {lookup(qf, user_id=uid, session_id=s1)}")
    print(f"[3] 换会话再问（应 None，不跨会话）-> {lookup(qf, user_id=uid, session_id=s2)}")

    # user 域：跨会话共享、跨用户隔离 + 语义
    q1 = "我最近头痛、头晕，应该挂什么科？"
    print(f"\n[4] user 域 store '{q1}'")
    store_async(q1, demo, user_id=uid, session_id=s1)
    time.sleep(3)
    print(f"[5] 同用户跨会话精确 -> {lookup(q1, user_id=uid, session_id=s2)}")
    print(f"[6] 相似问法(同用户, 期望 L2) -> {lookup('头痛头晕该挂什么科', user_id=uid, session_id=s2)}")
    print(f"[7] 其他用户同问（应 None，用户隔离）-> {lookup(q1, user_id='another-user', session_id=s2)}")
    print("[SUCCESS] self-test completed")
