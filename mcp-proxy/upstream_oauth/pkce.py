"""PKCE code verifier and S256 challenge."""

from __future__ import annotations

import base64
import hashlib
import secrets


def new_code_verifier() -> str:
    """RFC 7636: 43-128 characters from unreserved charset."""
    return secrets.token_urlsafe(48)[:128]


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
