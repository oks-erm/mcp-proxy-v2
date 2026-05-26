"""Auth routes: config, POST /auth/login/google, /auth/refresh, /auth/logout (cookies + JWT)."""

import logging
from typing import Any, Dict, Union

import config
from api.deps import verify_csrf_for_unsafe_methods
from audit.logger import log_audit
from auth.cookies import (
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    cookie_secure,
    new_csrf_value,
)
from auth.google import verify_google_token
from auth.jwt_handler import (
    create_access_token,
    create_refresh_token,
    get_user_from_refresh_token,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from users.schemas import (
    LoginResponsePending,
    LoginResponseRejected,
    LoginResponseTokens,
)
from users.service import ensure_user_and_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class GoogleLoginRequest(BaseModel):
    credential: str = Field(..., description="Google ID token from frontend")


def _set_auth_cookies(response: Response, refresh: str, csrf: str) -> None:
    sec = cookie_secure()
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh,
        httponly=True,
        secure=sec,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/auth",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf,
        httponly=False,
        secure=sec,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    sec = cookie_secure()
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/auth", secure=sec)
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", secure=sec)


@router.get("/config")
async def auth_config() -> Dict[str, Any]:
    """Public config for SPA (OAuth client ID is not secret)."""
    return {
        "google_oauth_client_id": config.GOOGLE_OAUTH_CLIENT_ID or "",
        "gcp_project_id": config.GCP_PROJECT_ID,
    }


@router.post(
    "/login/google",
    response_model=Union[LoginResponseTokens, LoginResponsePending, LoginResponseRejected],
)
async def login_google(body: GoogleLoginRequest, response: Response):
    """
    Authenticate with Google ID token. Active users receive access JWT in JSON and
    refresh + CSRF cookies (httpOnly refresh; CSRF cookie readable by JS for X-CSRF-Token).
    """
    try:
        idinfo = verify_google_token(body.credential)
    except ValueError as e:
        log_audit("login_attempt", result="error", details=str(e))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e

    email = idinfo.get("email") or ""
    google_id = idinfo.get("sub") or ""
    if not email or not google_id:
        log_audit("login_attempt", result="error", details="Missing email or sub in token")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token payload")

    user, access_request, is_new = ensure_user_and_request(email=email, google_id=google_id)

    if is_new and access_request:
        log_audit(
            "access_request_created",
            user_id=user.id,
            role=user.role,
            result="success",
            details={"email": email},
        )
    log_audit("login_attempt", user_id=user.id, role=user.role, result="success")

    if user.status == "rejected":
        return LoginResponseRejected()

    if user.status == "pending":
        return LoginResponsePending()

    access = create_access_token(user_id=user.id, role=user.role, email=user.email)
    refresh = create_refresh_token(user_id=user.id, role=user.role, email=user.email)
    csrf = new_csrf_value()
    _set_auth_cookies(response, refresh, csrf)
    return LoginResponseTokens(tokens={"access": access})


@router.post("/refresh")
async def refresh_tokens(
    request: Request,
    response: Response,
    _: None = Depends(verify_csrf_for_unsafe_methods),
) -> Dict[str, str]:
    """Rotate refresh cookie and return new access token. Requires CSRF header + cookie."""
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh session")
    user = get_user_from_refresh_token(raw)
    if not user:
        _clear_auth_cookies(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh")
    access = create_access_token(user_id=user.id, role=user.role, email=user.email)
    new_refresh = create_refresh_token(user_id=user.id, role=user.role, email=user.email)
    csrf = new_csrf_value()
    _set_auth_cookies(response, new_refresh, csrf)
    return {"access": access}


@router.post("/logout")
async def logout(
    response: Response,
    _: None = Depends(verify_csrf_for_unsafe_methods),
) -> Dict[str, str]:
    """Clear refresh and CSRF cookies."""
    _clear_auth_cookies(response)
    return {"status": "logged_out"}
