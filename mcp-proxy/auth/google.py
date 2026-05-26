"""Verify Google ID tokens (Sign-In for Web)."""

from typing import Any, Dict

import config
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token


def verify_google_token(credential: str) -> Dict[str, Any]:
    """Validate JWT from Google One Tap / Sign-In; returns token payload."""
    audience = config.GOOGLE_OAUTH_CLIENT_ID or ""
    if not audience:
        raise ValueError("GOOGLE_OAUTH_CLIENT_ID not configured")
    request = google_requests.Request()
    return id_token.verify_oauth2_token(credential, request, audience)
