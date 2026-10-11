"""
用户认证路由（讲义 §7）：注册 / 登录 / 当前用户。

安全约定：
- 登录失败统一返回「用户名或密码错误」，不区分两者，防止用户名枚举。
- 登录采用 OAuth2 密码模式表单（username/password 表单字段），
  Swagger /docs 的 Authorize 按钮可直接联调。
- 历史/认证接口用 def 同步路由：FastAPI 自动放入线程池，
  匹配 store 层「每请求新开连接、用完即关」的约定。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from config.settings import settings
from src.auth import security
from src.auth.deps import User, get_current_user
from src.auth.store import DuplicateUserError, user_store
from src.api.models import RegisterRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["用户认证"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="用户注册",
    description="用户名 + 密码注册；密码服务端 bcrypt 哈希，绝不明文存储"
)
def register(req: RegisterRequest):
    """注册新用户（用户名唯一）。"""
    password_hash = security.hash_password(req.password)
    try:
        user = user_store.create_user(req.username, password_hash)
    except DuplicateUserError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已存在，请换一个"
        )
    return UserOut(id=user["id"], username=user["username"], role=user["role"])


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="用户登录",
    description="OAuth2 密码模式（表单字段 username/password），成功返回 JWT"
)
def login(form: OAuth2PasswordRequestForm = Depends()):
    """校验用户名密码，签发 access token。"""
    row = user_store.get_by_username(form.username)
    if row is None or not security.verify_password(form.password, row["password_hash"]):
        # 统一口径，防止通过报错差异枚举用户名
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if row["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被停用"
        )

    user_store.touch_last_login(row["id"])
    token = security.create_access_token(row["id"], row["username"], row["role"])
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut(id=row["id"], username=row["username"], role=row["role"]),
    )


@router.get(
    "/me",
    response_model=UserOut,
    summary="当前登录用户",
    description="校验 token 并返回当前用户信息（前端可用于启动时验证登录态）"
)
def me(current_user: User = Depends(get_current_user)):
    """返回当前用户（依赖已完成鉴权）。"""
    return UserOut(id=current_user.id, username=current_user.username, role=current_user.role)
