"""
FastAPI 认证依赖：从 Bearer token 解析当前用户 + 角色门控。

用法（在路由签名上挂依赖即完成鉴权）：
    @router.get("/xxx")
    def xxx(current_user: User = Depends(get_current_user)): ...

    @router.get("/admin-only")
    def admin_only(current_user: User = Depends(require_role("admin"))): ...
"""

from dataclasses import dataclass

import jwt as pyjwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from src.auth import security
from src.auth.store import user_store

# tokenUrl 指向登录端点（挂载后完整路径 /api/auth/login），
# Swagger /docs 右上角 Authorize 按钮会据此自动接入。
# auto_error=False：无 token 时返回 None，由本模块统一抛 401（错误体格式一致）。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


@dataclass
class User:
    """注入到路由的当前用户对象（只含业务需要的最小字段）。"""
    id: str
    username: str
    role: str
    status: str


def get_current_user(token: str | None = Depends(oauth2_scheme)) -> User:
    """解析并校验 Bearer token，返回当前用户；任何失败统一 401/403。

    校验链：有 token → 签名/有效期有效 → 用户存在 → 账号未被停用。
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未认证：请先登录（token 缺失、无效或已过期）",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise unauthorized

    try:
        payload = security.decode_access_token(token)
    except pyjwt.PyJWTError:
        raise unauthorized

    user_id = payload.get("sub")
    if not user_id:
        raise unauthorized

    row = user_store.get_by_id(user_id)
    if row is None:
        raise unauthorized
    if row["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被停用",
        )
    return User(id=row["id"], username=row["username"], role=row["role"], status=row["status"])


def require_role(role: str):
    """角色门控依赖工厂：如 Depends(require_role("admin"))。"""

    def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role != role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足：需要 {role} 角色",
            )
        return current_user

    return checker
