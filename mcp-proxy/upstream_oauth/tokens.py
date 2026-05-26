"""Resolve a valid access token for upstream OAuth (refresh if needed)."""

from __future__ import annotations

import logging
import time

from models import ServerConfig
from upstream_oauth.token_store import (
    _ACCESS_REFRESH_MARGIN_SEC,
    get_token_doc,
    refresh_stored_tokens,
)

logger = logging.getLogger(__name__)


class UpstreamOAuthNotConnectedError(Exception):
    """User has not completed OAuth for this upstream server."""


async def ensure_upstream_oauth_access_token(
    cfg: ServerConfig,
    user_id: str,
) -> tuple[str, str]:
    """
    Return (access_token, token_type) for cfg.oauth2 + user_id.
    Refreshes when near expiry.
    """
    if cfg.upstream_auth != "oauth2" or not cfg.oauth:
        raise ValueError("ensure_upstream_oauth_access_token requires oauth2 server config")
    oauth = cfg.oauth
    row = get_token_doc(user_id, cfg.id)
    if not row or not row.get("refresh_enc"):
        raise UpstreamOAuthNotConnectedError(f"OAuth not connected for server {cfg.id}; complete the link in Account")
    exp = row.get("expires_at")
    now = time.time()
    if exp is None or float(exp) < now + _ACCESS_REFRESH_MARGIN_SEC:
        try:
            row = await refresh_stored_tokens(user_id, cfg.id, row, oauth)
        except Exception as e:
            logger.warning("OAuth refresh failed server=%s user=%s: %s", cfg.id, user_id, e)
            raise
    access = row.get("access_token")
    if not access:
        raise UpstreamOAuthNotConnectedError(f"No access token for server {cfg.id}")
    token_type = str(row.get("token_type") or "Bearer")
    return str(access), token_type
