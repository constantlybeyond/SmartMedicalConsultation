"""
用户存储（MySQL 冷存储，唯一事实源）
users 表 + 注册/查询。连接管理约定与 history.store 完全一致：
每请求新开连接、用完即关（历史/认证接口为 def 同步路由，
FastAPI 自动放入线程池，禁止跨线程共享连接）。
"""

import uuid
from datetime import datetime

import mysql.connector

from config.settings import settings
from src.common.logger import setup_logger

logger = setup_logger(__name__, "auth.log")


class DuplicateUserError(Exception):
    """用户名已存在（注册时抛出，路由层转 409）。"""


class UserStore:
    """用户管理器：users 表 CRUD"""

    def __init__(self):
        self._schema_ready = False

    # ============================================================
    # 连接管理（与 history.store 同款）
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
        """建库 + users 表（IF NOT EXISTS，幂等）。与历史表共用同一个库。"""
        conn = self._bootstrap_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.mysql_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(f"USE `{settings.mysql_database}`")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            VARCHAR(64)  PRIMARY KEY,
                    username      VARCHAR(32)  NOT NULL,
                    password_hash VARCHAR(128) NOT NULL,
                    role          VARCHAR(16)  NOT NULL DEFAULT 'user',
                    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
                    created_at    DATETIME     NOT NULL,
                    last_login_at DATETIME     NULL,
                    UNIQUE KEY uk_username (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            conn.commit()
            cur.close()
            self._schema_ready = True
            logger.info("[AUTH] MySQL schema ensured (users)")
        finally:
            conn.close()

    # ============================================================
    # 写
    # ============================================================

    def create_user(self, username: str, password_hash: str, role: str = "user") -> dict:
        """注册新用户；用户名冲突抛 DuplicateUserError。"""
        user_id = uuid.uuid4().hex
        now = datetime.now()
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (id, username, password_hash, role, status, created_at) "
                "VALUES (%s, %s, %s, %s, 'active', %s)",
                (user_id, username, password_hash, role, now),
            )
            conn.commit()
            cur.close()
        except mysql.connector.errors.IntegrityError:
            conn.rollback()
            raise DuplicateUserError(username)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "id": user_id,
            "username": username,
            "role": role,
            "status": "active",
            "created_at": now,
            "last_login_at": None,
        }

    def touch_last_login(self, user_id: str) -> None:
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET last_login_at = %s WHERE id = %s",
                (datetime.now(), user_id),
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()

    # ============================================================
    # 读
    # ============================================================

    def _row_to_user(self, r: dict) -> dict:
        return {
            "id": r["id"],
            "username": r["username"],
            "password_hash": r["password_hash"],
            "role": r["role"],
            "status": r["status"],
            "created_at": r["created_at"],
            "last_login_at": r["last_login_at"],
        }

    def get_by_username(self, username: str) -> dict | None:
        conn = self._conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, username, password_hash, role, status, created_at, last_login_at "
                "FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
            cur.close()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()

    def get_by_id(self, user_id: str) -> dict | None:
        conn = self._conn()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, username, password_hash, role, status, created_at, last_login_at "
                "FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            cur.close()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()


# 全局单例
user_store = UserStore()
