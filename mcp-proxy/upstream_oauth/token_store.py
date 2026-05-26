"""Firestore storage for per-user upstream OAuth tokens."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

from gcp_firestore import get_client
from upstream_oauth.crypto import decrypt_refresh_token, encrypt_refresh_token
from upstream_oauth.http_client import refresh_access_token
from upstream_oauth.secrets import fetch_secret_string

logger = logging.getLogger(__name__)

_COLLECTION = os.getenv("USER_UPSTREAM_OAUTH_COLLECTION", "user_upstream_oauth")
_ACCESS_REFRESH_MARGIN_SEC = 120


def _coll():
    return get_client().collection(_COLLECTION)


def doc_id(user_id: str, server_id: str) -> str:
    return f"{user_id}__{server_id}"


def get_token_doc(user_id: str, server_id: str) -> Optional[dict[str, Any]]:
    ref = _coll().document(doc_id(user_id, server_id))
    snap = ref.get()
    if not snap.exists:
        return None
    return snap.to_dict()


def delete_token_doc(user_id: str, server_id: str) -> None:
    _coll().document(doc_id(user_id, server_id)).delete()
    logger.info("Deleted upstream OAuth tokens user=%s server=%s", user_id, server_id)


def save_tokens_after_exchange(
    user_id: str,
    server_id: str,
    *,
    access_token: str,
    refresh_token: Optional[str],
    expires_in: Optional[int],
    token_type: str = "Bearer",
    existing_refresh_enc: Optional[str] = None,
) -> None:
    now = datetime.utcnow()
    if refresh_token:
        refresh_enc = encrypt_refresh_token(refresh_token)
    else:
        refresh_enc = existing_refresh_enc or ""
    if not refresh_enc:
        raise ValueError("No refresh token to store")
    expires_at: Optional[float] = None
    if expires_in is not None:
        expires_at = time.time() + float(expires_in)
    data = {
        "user_id": user_id,
        "server_id": server_id,
        "access_token": access_token,
        "refresh_enc": refresh_enc,
        "expires_at": expires_at,
        "token_type": token_type or "Bearer",
        "updated_at": now,
    }
    _coll().document(doc_id(user_id, server_id)).set(data)


def parse_token_response(body: dict[str, Any]) -> tuple[str, Optional[str], Optional[int], str]:
    access = body.get("access_token") or body.get("accessToken")
    if not access or not isinstance(access, str):
        raise ValueError("Token response missing access_token")
    refresh = body.get("refresh_token") or body.get("refreshToken")
    if refresh is not None and not isinstance(refresh, str):
        refresh = None
    expires_in = body.get("expires_in") or body.get("expiresIn")
    if expires_in is not None:
        try:
            expires_in = int(expires_in)
        except (TypeError, ValueError):
            expires_in = None
    token_type = str(body.get("token_type") or body.get("tokenType") or "Bearer")
    return access, refresh, expires_in, token_type


async def refresh_stored_tokens(
    user_id: str,
    server_id: str,
    row: dict[str, Any],
    oauth_cfg: Any,
) -> dict[str, Any]:
    """Refresh access token using stored refresh_enc; updates Firestore."""
    blob = row.get("refresh_enc")
    if not blob:
        raise ValueError("No refresh token stored")
    refresh_plain = decrypt_refresh_token(str(blob))
    client_secret = ""
    if oauth_cfg.client_secret_secret_id:
        client_secret = await asyncio.to_thread(fetch_secret_string, oauth_cfg.client_secret_secret_id)
    body = await refresh_access_token(
        oauth_cfg.token_endpoint,
        refresh_token=refresh_plain,
        client_id=oauth_cfg.client_id,
        client_secret=client_secret or None,
        audience=oauth_cfg.audience,
        extra_form=oauth_cfg.extra_token_params,
    )
    access, new_refresh, expires_in, token_type = parse_token_response(body)
    save_tokens_after_exchange(
        user_id,
        server_id,
        access_token=access,
        refresh_token=new_refresh,
        expires_in=expires_in,
        token_type=token_type,
        existing_refresh_enc=str(blob) if not new_refresh else None,
    )
    return get_token_doc(user_id, server_id) or row
