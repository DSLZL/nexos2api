"""管理界面访问控制。

若 ADMIN_PASSWORD 环境变量非空，所有 /admin/* 和 /api/stats/* 端点
要求请求携带 Authorization: Bearer <password> 头。
未配置时无需认证（适合本地/内网部署）。
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import ADMIN_PASSWORD

_bearer = HTTPBearer(auto_error=False)


def require_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """FastAPI 依赖：校验 Bearer token。ADMIN_PASSWORD 未设置时直接放行。"""
    if not ADMIN_PASSWORD:
        return
    if creds is None or creds.credentials != ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权：请提供正确的 ADMIN_PASSWORD",
            headers={"WWW-Authenticate": "Bearer"},
        )
