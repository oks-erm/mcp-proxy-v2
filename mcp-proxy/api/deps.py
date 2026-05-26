"""FastAPI dependencies: get_current_user (JWT), require_admin, CSRF."""

from typing import Optional

from auth.csrf import (  # noqa: F401 (re-exported for routers)
    verify_csrf_for_unsafe_methods,
)
from auth.jwt_handler import get_user_from_token
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import Request
from users.schemas import UserInDB

security = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> UserInDB:
    """Extract and validate JWT from Authorization: Bearer <token>. Raises 401 if missing or invalid."""
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def require_admin(user: UserInDB = Depends(get_current_user)) -> UserInDB:
    """Require current user to have role admin. Raises 403 otherwise."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
