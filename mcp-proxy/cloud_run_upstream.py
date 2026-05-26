"""Cloud Run service URL → OIDC audience and cached ID token for the proxy runtime SA."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import google.auth.transport.requests
import google.oauth2.id_token

# Tokens last ~1h; refresh before expiry to avoid edge failures
_CACHE_TTL_SECONDS = 50 * 60
_token_cache: dict[str, tuple[float, str]] = {}


def cloud_run_audience_from_url(mcp_url: str) -> str:
    """
    Audience for fetch_id_token must be the receiving service root URL, e.g.
    https://my-service-xxxxx-ew.a.run.app/ (see Cloud Run service-to-service docs).
    """
    p = urlparse(mcp_url.strip())
    if not p.scheme or not p.netloc:
        raise ValueError(f"Invalid MCP URL for Cloud Run audience: {mcp_url!r}")
    return f"{p.scheme}://{p.netloc}/"


def fetch_id_token_for_audience(audience: str) -> str:
    """Mint a Google-signed ID token for the given audience (uses ADC / metadata on Cloud Run)."""
    now = time.time()
    cached = _token_cache.get(audience)
    if cached is not None:
        ts, tok = cached
        if now - ts < _CACHE_TTL_SECONDS:
            return tok
    req = google.auth.transport.requests.Request()
    tok = google.oauth2.id_token.fetch_id_token(req, audience)
    _token_cache[audience] = (now, tok)
    return tok


def clear_id_token_cache() -> None:
    """Test helper."""
    _token_cache.clear()
