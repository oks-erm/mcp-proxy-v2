"""Issue and verify JWT for REST API. All REST auth is Bearer JWT."""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import config
import jwt
from users.schemas import UserInDB
from users.store import get_user_by_id

logger = logging.getLogger(__name__)


def _get_secret() -> str:
    """JWT signing secret from config (env or single JSON secret in Secret Manager)."""
    if config.JWT_SECRET_KEY:
        return config.JWT_SECRET_KEY
    if config.ENV == "local":
        return os.getenv("JWT_SECRET_KEY", "local-jwt-secret-key")
    raise RuntimeError("JWT_SECRET_KEY not set (configure via env or mcp-proxy-config secret)")


def create_access_token(user_id: str, role: str, email: str) -> str:
    """Create a short-lived access token (Bearer) for REST API."""
    secret = _get_secret()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=config.JWT_ACCESS_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "role": role,
        "email": email,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, secret, algorithm=config.JWT_ALGORITHM)


def create_refresh_token(user_id: str, role: str, email: str) -> str:
    """Create a refresh token (longer-lived)."""
    secret = _get_secret()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=7)
    payload = {
        "sub": user_id,
        "role": role,
        "email": email,
        "iat": now,
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, secret, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and verify JWT. Returns payload dict or None if invalid."""
    try:
        secret = _get_secret()
        payload = jwt.decode(token, secret, algorithms=[config.JWT_ALGORITHM])
        return payload
    except jwt.InvalidTokenError:
        return None


def get_user_from_token(token: str) -> Optional[UserInDB]:
    """Return UserInDB from Bearer token or None. Used by get_current_user dependency."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    return get_user_by_id(user_id)


def get_user_from_refresh_token(token: str) -> Optional[UserInDB]:
    """Validate refresh JWT and return user if active."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "refresh":
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    user = get_user_by_id(user_id)
    if not user or user.status != "active":
        return None
    return user
