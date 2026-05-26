"""CSRF: double-submit cookie must match X-CSRF-Token on unsafe methods."""

from auth.cookies import CSRF_COOKIE_NAME
from fastapi import HTTPException, Request, status

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Paths that accept unsafe methods without prior CSRF cookie (login establishes cookies).
CSRF_EXEMPT_PATHS = frozenset({"/auth/login/google"})


async def verify_csrf_for_unsafe_methods(request: Request) -> None:
    """Require matching CSRF header and cookie for POST/PUT/PATCH/DELETE (except login)."""
    if request.method not in UNSAFE_METHODS:
        return
    path = request.url.path
    if path in CSRF_EXEMPT_PATHS:
        return
    header = request.headers.get("X-CSRF-Token") or request.headers.get("x-csrf-token")
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not header or not cookie or header != cookie:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        )
