"""
认证安全工具：密码哈希（bcrypt）+ JWT 签发/校验（HS256）

设计说明（与讲义 §7 一致）：
- 密码只做单向哈希，绝不明文/可逆存储。bcrypt 输入上限 72 字节，中文密码
  （UTF-8 每字 3 字节）可能超限被静默截断，故先做 SHA-256 预哈希（定长 32 字节）
  再进 bcrypt——业界通行做法，等价强度、规避截断。
- JWT 采用 HS256 对称签名：单体服务自签自验，无需引入非对称密钥管理。
- jwt_secret 强烈建议在 .env 配置固定随机串；留空则每次启动随机生成
  （所有已签发 token 在重启后失效，仅适合开发环境）。
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from config.settings import settings
from src.common.logger import setup_logger

logger = setup_logger(__name__, "auth.log")

# bcrypt 工作因子：12 轮 ≈ 单次 200-300ms，足以抵御离线爆破，
# 登录接口又天然低频（失败可再叠加限流），体验无感。
_BCRYPT_ROUNDS = 12


# ============================================================
# JWT 密钥解析（进程级稳定）
# ============================================================

def _resolve_jwt_secret() -> str:
    if settings.jwt_secret:
        return settings.jwt_secret
    generated = secrets.token_urlsafe(48)
    logger.warning(
        "[AUTH] JWT_SECRET 未配置：已生成本进程随机密钥。"
        "服务重启后所有登录态失效（仅适合开发）。"
        "请在 .env 设置 JWT_SECRET（python -c \"import secrets;print(secrets.token_urlsafe(48))\" 可生成）"
    )
    return generated


_JWT_SECRET = _resolve_jwt_secret()


# ============================================================
# 密码哈希
# ============================================================

def _prehash(plain: str) -> bytes:
    """SHA-256 预哈希：规避 bcrypt 72 字节截断（中文密码可超 72 字节）。"""
    return hashlib.sha256(plain.encode("utf-8")).digest()


def hash_password(plain: str) -> str:
    """生成密码哈希（含盐，每次结果不同）。"""
    return bcrypt.hashpw(_prehash(plain), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码；任何异常（哈希格式损坏等）一律按校验失败处理。"""
    try:
        return bcrypt.checkpw(_prehash(plain), hashed.encode("utf-8"))
    except Exception:
        return False


# ============================================================
# JWT 签发 / 校验
# ============================================================

def create_access_token(user_id: str, username: str, role: str) -> str:
    """签发 access token。sub=用户 ID；iat/exp 用 UTC（JWT 规范）。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """校验签名与有效期，返回 payload；失败抛 jwt.PyJWTError（调用方转 401）。"""
    return jwt.decode(token, _JWT_SECRET, algorithms=[settings.jwt_algorithm])
