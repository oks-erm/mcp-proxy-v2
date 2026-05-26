"""Resolve HTTP headers for upstream MCP requests (Secret Manager / inline, Cloud Run IAM, OAuth2)."""

from __future__ import annotations

import asyncio

from cloud_run_upstream import cloud_run_audience_from_url, fetch_id_token_for_audience
from credentials_resolver import get_headers
from models import ServerConfig
from upstream_oauth.tokens import ensure_upstream_oauth_access_token


async def resolve_upstream_headers(cfg: ServerConfig, *, user_id: str | None = None) -> dict[str, str]:
    """Build headers for POSTs to an upstream MCP (initialize, tools/call, etc.)."""
    if cfg.upstream_auth == "cloud_run_iam":
        audience = cloud_run_audience_from_url(cfg.url)
        token = await asyncio.to_thread(fetch_id_token_for_audience, audience)
        headers = {"Authorization": f"Bearer {token}"}
        extra = await asyncio.to_thread(get_headers, cfg.credentials_secret_id or "", cfg.credentials_header or "")
        for name, value in extra.items():
            if name.lower() == "authorization":
                continue
            headers[name] = value
        return headers
    if cfg.upstream_auth == "oauth2":
        if not user_id:
            raise ValueError("user_id is required for upstream_auth oauth2")
        access, token_type = await ensure_upstream_oauth_access_token(cfg, user_id)
        tt = (token_type or "Bearer").strip()
        if tt.lower() == "bearer":
            headers = {"Authorization": f"Bearer {access}"}
        else:
            headers = {"Authorization": f"{tt} {access}"}
        extra = await asyncio.to_thread(get_headers, cfg.credentials_secret_id or "", cfg.credentials_header or "")
        for name, value in extra.items():
            if name.lower() == "authorization":
                continue
            headers[name] = value
        return headers
    return await asyncio.to_thread(get_headers, cfg.credentials_secret_id or "", cfg.credentials_header or "")
