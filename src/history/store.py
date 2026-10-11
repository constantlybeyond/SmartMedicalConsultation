"""
对话历史存储（冷热分层 + 用户归属 + token 预算）
Redis 热缓存（最近 N 轮会话上下文，TTL 24h）+ MySQL 8.0 冷存储（全量历史，唯一事实源）。

设计说明（与讲义 §6.2 / §6.6 / §7 契约一致）：
- 读路径（三级命中）：先读 Redis `sess:{sid}` → 未命中从 MySQL 取最近 N 轮并回填 Redis → 返回。
- 写路径（双写容错）：**先写 MySQL（事实源）再写 Redis**；MySQL 失败则本轮不落 Redis
  （保证"缓存里绝不出现数据库没有的幻影数据"）；Redis 失败仅记日志降级，不影响主链路。
- Redis List 语义：LPUSH 插头（最新在头）+ LTRIM 保留 N 轮 + EXPIRE 24h；
  读出后反序即时间正序。
- 连接管理：Redis 用进程级连接池单例；MySQL 每请求新开、用完即关
  （历史接口为 def 同步路由，FastAPI 自动放入线程池，禁止跨线程共享连接）。
- `session_id == conversation_id`：一个 id 串起前端会话、Redis 热缓存、MySQL 冷存储。

用户归属（§7 用户认证配套）：
- `conversations.user_id` 标记会话归属；所有读写接口强制携带 user_id。
- 首条消息落库时惰性创建会话并绑定 user_id；已存在的会话仅属主可读写，
  他人访问抛 ConversationOwnershipError（路由层统一转 404，不泄露会话存在性）。
- Redis 只是热缓存：进入读/写路径前先在 MySQL 完成归属裁决，
  因此缓存命中也不会跨越归属边界。

Token 预算（§6.6 上下文长度控制）：
- 轮次硬上限 `HOT_TURNS`（settings.history_max_turns）之上再叠加 **token 预算**：
  load_context 返回前用 trim_turns_to_budget 从最旧裁剪（保底最近 2 轮）。
- 超预算的旧轮次不再硬截断丢弃：assistant 轮落库后触发**后台增量摘要**
  （summarizer.py），滚动合并进 `conversations.summary`，并以
  `conversations.summary_msg_id` 作水位——水位之前的消息已摘要化，
  读路径只取水位之后的原文 + 摘要段（原始消息保留，医疗审计价值不丢）。
"""

import json
from datetime import datetime

import mysql.connector
import redis

from config.settings import settings
from src.common.logger import setup_logger
from src.common.tokens import trim_turns_to_budget

logger = setup_logger(__name__, "history.log")

HOT_TURNS = settings.history_max_turns   # 热缓存保留轮次（配置化，默认 12）
HOT_TTL_SECONDS = 24 * 3600              # 热缓存 TTL：24h（滑动续期）
TITLE_MAX_LEN = 24                       # 对话标题截断长度


class ConversationNotFoundError(Exception):
    """会话不存在（路由层转 404）。"""


class ConversationOwnershipError(Exception):
    """会话存在但属于其他用户（路由层同样转 404，不泄露存在性）。"""


def _hot_key(session_id: str) -> str:
    return f"sess:{session_id}"


def _truncate(text: str, limit: int = TITLE_MAX_LEN) -> str:
    """截断对话标题。

    注：切片终点用小写参数——check_contracts 的 Cypher 关系名扫描器
    会把「[ 数字/冒号 + 大写标识符 ]」形式的切片误判为关系名引用。
    """
    return text[0:limit]


def _now() -> datetime:
    return datetime.now()


class HistoryStore:
    """对话历史管理器：MySQL 冷存储 + Redis 热缓存 + 用户归属校验"""

    def __init__(self):
        self._redis_pool: redis.Redis | None = None
        self._schema_ready = False

    # ============================================================
    # Redis 层（热缓存）
    # ============================================================

    def _get_redis(self) -> redis.Redis:
        """连接池单例（惰性创建）"""
        if self._redis_pool is None:
            self._redis_pool = redis.Redis.from_url(
                settings.redis_url, decode_responses=True
            )
        return self._redis_pool

    def get_hot(self, session_id: str) -> list[dict] | None:
        """读热缓存；Redis 不可用/未命中返回 None（调用方回源 MySQL）。"""
        try:
            items = self._get_redis().lrange(_hot_key(session_id), 0, HOT_TURNS - 1)
        except Exception as e:
            logger.warning(f"[HISTORY] Redis read failed, fallback to MySQL: {e}")
            return None
        if not items:
            return None
        turns = []
        for raw in reversed(items):  # LPUSH 插头 → 反序即时间正序
            try:
                turns.append(json.loads(raw))
            except Exception:
                continue
        return turns or None

    def append_hot(self, session_id: str, turn: dict) -> bool:
        """写热缓存；失败仅记日志返回 False（缓存可缺，不可幻）。"""
        try:
            r = self._get_redis()
            key = _hot_key(session_id)
            r.lpush(key, json.dumps(turn, ensure_ascii=False))
            r.ltrim(key, 0, HOT_TURNS - 1)
            r.expire(key, HOT_TTL_SECONDS)  # 滑动续期
            return True
        except Exception as e:
            logger.warning(f"[HISTORY] Redis write failed, degraded (MySQL is source of truth): {e}")
            return False

    def _backfill_hot(self, session_id: str, turns: list[dict]) -> None:
        """把 MySQL 回源的轮次按时间正序逐条 LPUSH，重建与 append_hot 相同的头部布局。"""
        try:
            r = self._get_redis()
            key = _hot_key(session_id)
            for t in turns:
                r.lpush(key, json.dumps(t, ensure_ascii=False))
            r.expire(key, HOT_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"[HISTORY] Redis backfill failed, degraded: {e}")

    def delete_hot(self, session_id: str) -> None:
        try:
            self._get_redis().delete(_hot_key(session_id))
        except Exception as e:
            logger.warning(f"[HISTORY] Redis delete failed: {e}")

    # ============================================================
    # MySQL 层（冷存储，唯一事实源）
    # ============================================================

    def _bootstrap_conn(self):
        """不带库连接，用于建库建表（首次使用自动执行）。"""
        return mysql.connector.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            charset="utf8mb4",
            collation="utf8mb4_unicode_ci",
        )

    def _conn(self):
        """每请求新开的业务连接（用完即关，禁止跨线程共享）。"""
        if not self._schema_ready:
            self._ensure_schema()
        return mysql.connector.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_database,
            charset="utf8mb4",
            collation="utf8mb4_unicode_ci",
        )

    def _ensure_schema(self) -> None:
        """建库 + 建表（IF NOT EXISTS，幂等）+ 旧表补列迁移。"""
        conn = self._bootstrap_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.mysql_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(f"USE `{settings.mysql_database}`")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id             VARCHAR(64)  PRIMARY KEY,
                    user_id        VARCHAR(64)  NOT NULL DEFAULT '',
                    title          VARCHAR(64)  NOT NULL DEFAULT '',
                    summary        TEXT         NULL,
                    summary_msg_id BIGINT       NOT NULL DEFAULT 0,
                    created_at     DATETIME     NOT NULL,
                    updated_at     DATETIME     NOT NULL,
                    INDEX idx_user_updated (user_id, updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
                    conversation_id VARCHAR(64)  NOT NULL,
                    role            VARCHAR(16)  NOT NULL,
                    content         MEDIUMTEXT   NOT NULL,
                    intent          VARCHAR(32)  NOT NULL DEFAULT '',
                    created_at      DATETIME     NOT NULL,
                    INDEX idx_conv_id (conversation_id, id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            # ---- 旧表补列迁移（幂等）----
            # CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，逐列检测后补上：
            #  · user_id：认证上线前无此列（§7）；旧会话回填 ''（无主，持有者可认领）
            #  · summary / summary_msg_id：token 预算上线前无此列（§6.6），默认空/0 即"未摘要"
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'conversations'",
                (settings.mysql_database,),
            )
            existing_cols = {r[0] for r in cur.fetchall()}
            if "user_id" not in existing_cols:
                logger.info("[HISTORY] Migrating conversations: adding user_id column")
                cur.execute(
                    "ALTER TABLE conversations "
                    "ADD COLUMN user_id VARCHAR(64) NOT NULL DEFAULT '' AFTER id, "
                    "ADD INDEX idx_user_updated (user_id, updated_at)"
                )
            if "summary" not in existing_cols or "summary_msg_id" not in existing_cols:
                logger.info("[HISTORY] Migrating conversations: adding summary columns")
                adds = []
                if "summary" not in existing_cols:
                    adds.append("ADD COLUMN summary TEXT NULL AFTER title")
                if "summary_msg_id" not in existing_cols:
                    adds.append("ADD COLUMN summary_msg_id BIGINT NOT NULL DEFAULT 0 AFTER summary")
                cur.execute("ALTER TABLE conversations " + ", ".join(adds))
            conn.commit()
            cur.close()
            self._schema_ready = True
            logger.info("[HISTORY] MySQL schema ensured (conversations / messages)")
        finally:
            conn.close()

    # ============================================================
    # 归属校验（§7：会话与用户绑定，越权访问一律拒绝）
    # ============================================================

    def get_conversation_owner(self, conversation_id: str) -> str | None:
        """返回会话属主 user_id；会话不存在返回 None。"""
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM conversations WHERE id = %s", (conversation_id,))
            row = cur.fetchone()
            cur.close()
            return row[0] if row else None
        finally:
            conn.close()

    def _claim_orphan(self, conversation_id: str, user_id: str) -> bool:
        """认领认证上线前遗留的无主会话（user_id=''）。

        session_id 为 128 位随机 UUID、不可猜测，持有它（浏览器 localStorage）
        的登录用户即原创建者——沿用认证上线前「持有者即属主」的信任模型，
        让老用户的旧对话平滑迁移到自己账号名下，而不是永久卡死在 404。
        原子操作：UPDATE ... WHERE user_id='' 至多一方认领成功（并发安全）。
        """
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE conversations SET user_id = %s WHERE id = %s AND user_id = ''",
                (user_id, conversation_id),
            )
            conn.commit()
            claimed = cur.rowcount > 0
            cur.close()
            if claimed:
                logger.info(f"[HISTORY] Orphan conversation claimed: {conversation_id} -> user={user_id}")
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _assert_owner(self, conversation_id: str, user_id: str) -> bool:
        """归属裁决：不存在 → NotFoundError；属他人 → OwnershipError；属本人 → True。

        无主会话（认证上线前遗留，user_id=''）→ 由当前用户认领后放行。
        路由层把两种异常统一转 404：对越权者不泄露会话是否存在。
        """
        owner = self.get_conversation_owner(conversation_id)
        if owner is None:
            raise ConversationNotFoundError(conversation_id)
        if owner == "":
            if self._claim_orphan(conversation_id, user_id):
                return True
            owner = self.get_conversation_owner(conversation_id)  # 并发认领落败 → 复看
        if owner != user_id:
            raise ConversationOwnershipError(conversation_id)
        return True

    # ============================================================
    # 摘要水位（§6.6：水位之前的消息已摘要化，读原文从水位之后开始）
    # ============================================================

    def get_summary_meta(self, conversation_id: str) -> tuple[str, int]:
        """返回 (滚动摘要, 已摘要水位消息 id)；会话不存在返回 ("", 0)。"""
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT summary, summary_msg_id FROM conversations WHERE id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
            cur.close()
            if row is None:
                return "", 0
            return (row[0] or ""), int(row[1] or 0)
        finally:
            conn.close()

    def save_summary(self, conversation_id: str, summary: str, water_id: int) -> None:
        """回写滚动摘要与水位，并失效 Redis 热缓存（缓存可能含已被摘要的旧轮，
        下次读路径回源 MySQL 重建，保证"缓存绝不越过水位"）。"""
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE conversations SET summary = %s, summary_msg_id = %s WHERE id = %s",
                (summary, water_id, conversation_id),
            )
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.delete_hot(conversation_id)
        logger.info(
            f"[HISTORY] Summary updated: session={conversation_id} "
            f"water_id={water_id} chars={len(summary)}"
        )

    def get_unsummarized_rows(self, conversation_id: str, after_id: int = 0) -> list[dict]:
        """水位之后的消息行（id 正序，含 id 字段），供摘要器与读路径使用。"""
        conn = self._conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, role, content, intent FROM messages "
                "WHERE conversation_id = %s AND id > %s ORDER BY id ASC",
                (conversation_id, after_id),
            )
            rows = cur.fetchall()
            cur.close()
            return rows
        finally:
            conn.close()

    # ---------- 读 ----------

    def list_conversations(self, user_id: str, limit: int = 50) -> list[dict]:
        """某用户的对话列表，按 updated_at 倒序（LIMIT 分页）。"""
        conn = self._conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s",
                (user_id, limit),
            )
            rows = cur.fetchall()
            cur.close()
            return [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else "",
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_messages(self, conversation_id: str, user_id: str) -> list[dict]:
        """该对话全部消息（属主限定），按写入顺序（id 正序）。"""
        self._assert_owner(conversation_id, user_id)
        conn = self._conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT role, content, intent, created_at FROM messages "
                "WHERE conversation_id = %s ORDER BY id ASC",
                (conversation_id,),
            )
            rows = cur.fetchall()
            cur.close()
            return [
                {
                    "role": r["role"],
                    "content": r["content"],
                    "intent": r["intent"] or "",
                    "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                }
                for r in rows
            ]
        finally:
            conn.close()

    # ---------- 写 ----------

    def insert_message(
        self, conversation_id: str, role: str, content: str, intent: str = "", user_id: str = ""
    ) -> None:
        """写入一条消息；conversation 不存在则惰性创建并绑定 user_id（title = 首条内容截断）。

        归属裁决：会话已存在且属他人 → ConversationOwnershipError（防 session_id 猜测写入）。
        """
        conn = self._conn()
        try:
            cur = conn.cursor()
            now = _now()
            cur.execute("SELECT user_id FROM conversations WHERE id = %s", (conversation_id,))
            row = cur.fetchone()
            if row is None:
                title = _truncate(content)
                try:
                    cur.execute(
                        "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (conversation_id, user_id, title, now, now),
                    )
                except mysql.connector.errors.IntegrityError:
                    # 并发首写竞争：另一请求刚创建了该会话 → 重新裁决归属
                    conn.rollback()
                    cur.execute(
                        "SELECT user_id FROM conversations WHERE id = %s", (conversation_id,)
                    )
                    owner_row = cur.fetchone()
                    if owner_row is None or owner_row[0] != user_id:
                        raise ConversationOwnershipError(conversation_id)
            elif row[0] == "":
                # 认证前遗留的无主会话：由当前用户认领（同事务内原子完成）
                cur.execute(
                    "UPDATE conversations SET user_id = %s WHERE id = %s AND user_id = ''",
                    (user_id, conversation_id),
                )
                if cur.rowcount == 0:
                    raise ConversationOwnershipError(conversation_id)  # 被他人抢先认领
            elif row[0] != user_id:
                raise ConversationOwnershipError(conversation_id)
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, intent, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (conversation_id, role, content, intent or "", now),
            )
            cur.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (now, conversation_id),
            )
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_conversation(self, conversation_id: str, user_id: str) -> None:
        """删除对话及全部消息（属主限定）：清 MySQL 两表 + DEL 对应 Redis key。"""
        self._assert_owner(conversation_id, user_id)
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM messages WHERE conversation_id = %s", (conversation_id,))
            cur.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.delete_hot(conversation_id)

    # ============================================================
    # 统一入口
    # ============================================================

    def persist_turn(
        self, session_id: str, role: str, content: str, intent: str = "", user_id: str = ""
    ) -> bool:
        """双写一轮：先 MySQL（事实源）再 Redis（缓存）。

        MySQL 失败 → 本轮同样不落 Redis（防幻影数据），记错误日志，返回 False；
        MySQL 成功、Redis 失败 → 仅降级（读路径未命中会回源），返回 True。
        归属冲突（他人会话）→ 向上抛出，由路由层处理，不属于"可降级"范畴。
        """
        turn = {"role": role, "content": content}
        if intent:
            turn["intent"] = intent
        try:
            self.insert_message(session_id, role, content, intent, user_id=user_id)
        except (ConversationOwnershipError, ConversationNotFoundError):
            raise
        except Exception as e:
            logger.error(f"[HISTORY] MySQL write failed (source of truth), skip Redis for this turn: {e}")
            return False
        self.append_hot(session_id, turn)

        # §6.6：一轮完整对话（assistant 落库）后，后台检查是否超 token 预算，
        # 超则增量摘要（不阻塞主链路；失败仅降级，下一轮会再次触发）
        if role == "assistant":
            from src.history.summarizer import schedule_summarization
            schedule_summarization(self, session_id)
        return True

    def load_context(self, session_id: str, user_id: str = "") -> list[dict]:
        """读取注入 LLM 的多轮上下文（§6.6 token 预算版）。

        流程：归属裁决 → 摘要水位 → 取水位后原文（Redis 热 / MySQL 回填）
        → 轮次硬上限 → token 预算滑窗 → 首部注入摘要段（如有）。

        归属裁决：会话已存在且属他人 → ConversationOwnershipError；
        会话尚不存在（首轮提问，未落库）→ 正常返回空上下文；
        无主会话（认证前遗留）→ 由当前用户认领后继续。

        返回形如 [{"role":"summary","content":...}, {"role":"user",...}, ...]，
        format_history() 会把 summary 元素渲染为【更早对话摘要】段。
        """
        owner = self.get_conversation_owner(session_id)
        if owner == "":
            if not self._claim_orphan(session_id, user_id):
                owner = self.get_conversation_owner(session_id)  # 并发认领落败 → 复看
                if owner != user_id:
                    raise ConversationOwnershipError(session_id)
        elif owner is not None and owner != user_id:
            raise ConversationOwnershipError(session_id)

        summary, water_id = self.get_summary_meta(session_id)

        if water_id > 0:
            # 水位已推进：Redis 热缓存可能含已被摘要的旧轮 → 不走热缓存，
            # 直读 MySQL 水位之后的原文（长对话场景，读取代价小，正确性优先）
            rows = self.get_unsummarized_rows(session_id, water_id)
            turns = self._rows_to_turns(rows)
        else:
            # 无摘要：原三级命中（Redis → MySQL 回填）
            turns = self.get_hot(session_id)
            if turns is None:
                turns = self._rows_to_turns(self.get_unsummarized_rows(session_id, 0))
                if turns:
                    self._backfill_hot(session_id, turns)
                    logger.info(
                        f"[HISTORY] Context backfilled from MySQL: "
                        f"session={session_id} turns={len(turns)}"
                    )

        if not turns:
            # 仅剩摘要（罕见：全部原文已摘要化）或完全空
            return [{"role": "summary", "content": summary}] if summary else []

        # 轮次硬上限 → token 预算滑窗（保底最近 2 轮，防单轮巨无霸撑爆提示词）
        turns = trim_turns_to_budget(turns[-HOT_TURNS:], settings.history_max_tokens)

        if summary:
            return [{"role": "summary", "content": summary}] + turns
        return turns

    @staticmethod
    def _rows_to_turns(rows: list[dict]) -> list[dict]:
        """消息行 → 上下文轮次（丢弃空字段；id 仅摘要器使用，不进提示词）。"""
        return [
            {k: v for k, v in (
                ("role", r["role"]), ("content", r["content"]), ("intent", r["intent"])
            ) if v}
            for r in rows
        ]


# 全局单例
history_store = HistoryStore()
