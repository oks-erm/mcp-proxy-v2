"""Encrypt OAuth refresh tokens at rest (key derived from JWT_SECRET_KEY)."""

from __future__ import annotations

import base64
import hashlib

import config
from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    secret = config.JWT_SECRET_KEY
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY is required to store upstream OAuth tokens")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    _fernet = Fernet(key)
    return _fernet


def encrypt_refresh_token(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_refresh_token(blob: str) -> str:
    try:
        return _get_fernet().decrypt(blob.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Invalid encrypted refresh token") from e
