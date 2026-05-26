"""Signed OAuth state (user_id, server_id, PKCE verifier) for callback validation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import config

STATE_TTL_SECONDS = 600


def sign_oauth_state(payload: dict[str, Any]) -> str:
    body = {
        **payload,
        "exp": int(time.time()) + STATE_TTL_SECONDS,
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    secret = config.JWT_SECRET_KEY or ""
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY is required for OAuth state")
    sig = hmac.new(secret.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_oauth_state(token: str) -> dict[str, Any]:
    if "." not in token:
        raise ValueError("Invalid state")
    b64, sig = token.rsplit(".", 1)
    secret = config.JWT_SECRET_KEY or ""
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY is required for OAuth state")
    expected = hmac.new(secret.encode(), b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("Invalid state signature")
    pad = "=" * (-len(b64) % 4)
    raw = base64.urlsafe_b64decode(b64 + pad)
    body = json.loads(raw.decode())
    if int(body.get("exp", 0)) < time.time():
        raise ValueError("State expired")
    for k in ("user_id", "server_id", "v"):
        if k not in body:
            raise ValueError("Invalid state payload")
    return body
