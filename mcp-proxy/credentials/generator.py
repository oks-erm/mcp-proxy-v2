"""Per-user MCP API keys: issue, hash storage, and resolve user from raw key."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

import config
from cryptography.fernet import Fernet, InvalidToken
from google.cloud.firestore import FieldFilter
from users import store as users_store
from users.schemas import UserInDB

logger = logging.getLogger(__name__)


def _hash_raw_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _user_key_fernet() -> Fernet:
    """Fernet instance for encrypting MCP user keys stored in Firestore (not for auth)."""
    explicit = (config.MCP_USER_KEY_ENCRYPTION_KEY or "").strip()
    if explicit:
        return Fernet(explicit.encode("ascii"))
    jwt_secret = (config.JWT_SECRET_KEY or "").strip()
    if not jwt_secret:
        raise RuntimeError("JWT_SECRET_KEY or MCP_USER_KEY_ENCRYPTION_KEY is required to store per-user MCP API keys")
    digest = hashlib.sha256(f"{jwt_secret}|mcp-proxy-user-key-enc-v1".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_user_key_for_storage(raw: str) -> str:
    """Encrypt raw MCP API key for Firestore field user_key_enc (ASCII token)."""
    return _user_key_fernet().encrypt(raw.encode("utf-8")).decode("ascii")


def decrypt_user_key_from_storage(blob: str) -> Optional[str]:
    """Decrypt user_key_enc; returns None if missing, corrupt, or wrong encryption key."""
    if not blob or not str(blob).strip():
        return None
    try:
        return _user_key_fernet().decrypt(str(blob).strip().encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, RuntimeError) as e:
        logger.warning("Could not decrypt user MCP key blob: %s", e)
        return None


def get_proxy_url() -> str:
    base = getattr(config, "MCP_PROXY_URL", None) or ""
    if base:
        return base.rstrip("/")
    return ""


def generate_user_key(user_id: str) -> str:
    """Create a new raw API key, store hash + encrypted raw on the user doc, return raw."""
    raw = "mcp_" + secrets.token_urlsafe(32)
    h = _hash_raw_key(raw)
    enc = encrypt_user_key_for_storage(raw)
    now = datetime.now(timezone.utc)
    ref = users_store._users_coll().document(user_id)
    ref.update({"user_key_hash": h, "user_key_enc": enc, "updated_at": now})
    logger.info("Issued new MCP API key for user_id=%s", user_id)
    return raw


def resolve_user_by_key(raw: str) -> Optional[UserInDB]:
    """Look up active user by raw MCP API key."""
    if not raw or not raw.strip():
        return None
    h = _hash_raw_key(raw.strip())
    q = users_store._users_coll().where(filter=FieldFilter("user_key_hash", "==", h)).limit(1)
    docs = list(q.stream())
    if not docs:
        return None
    user = users_store.user_in_db_from_document(docs[0])
    if user.status != "active":
        return None
    return user
