"""OAuth token endpoint calls (async httpx)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


async def exchange_authorization_code(
    token_url: str,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: Optional[str],
    code_verifier: Optional[str],
    audience: Optional[str],
    extra_form: Optional[dict[str, str]],
) -> dict[str, Any]:
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if code_verifier:
        data["code_verifier"] = code_verifier
    if audience:
        data["audience"] = audience
    if extra_form:
        for k, v in extra_form.items():
            if k not in data:
                data[k] = v
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(token_url, data=data)
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(
    token_url: str,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: Optional[str],
    audience: Optional[str],
    extra_form: Optional[dict[str, str]],
) -> dict[str, Any]:
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if audience:
        data["audience"] = audience
    if extra_form:
        for k, v in extra_form.items():
            if k not in data:
                data[k] = v
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(token_url, data=data)
        resp.raise_for_status()
        return resp.json()
