"""Cookie names and helpers for refresh (httpOnly) and CSRF (double-submit)."""

import os
import secrets

REFRESH_COOKIE_NAME = "mcp_refresh"
CSRF_COOKIE_NAME = "mcp_csrf"


def cookie_secure() -> bool:
    """Use Secure flag on cookies when not in local dev (HTTPS in prod)."""
    return os.getenv("ENV", "local") != "local"


def new_csrf_value() -> str:
    return secrets.token_urlsafe(32)
