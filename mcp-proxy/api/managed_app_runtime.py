"""Internal runtime endpoints for deployed Host Wise managed apps."""

from __future__ import annotations

import logging
from typing import Any, Dict

import config
from fastapi import APIRouter, HTTPException, Request, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from managed_app_secrets_store import get_app_secret_value, normalize_secret_name
from managed_apps_store import get_managed_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/apps", tags=["managed-app-runtime"])


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    return auth.split(" ", 1)[1].strip()


def verify_runtime_identity(token: str) -> Dict[str, Any]:
    """Verify a Google ID token from a Cloud Run runtime service account."""
    audience = (config.MCP_PROXY_URL or "").rstrip("/")
    if not audience:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MCP_PROXY_URL is not configured")
    try:
        return id_token.verify_oauth2_token(token, google_requests.Request(), audience)
    except Exception as exc:
        logger.warning("Managed app runtime identity verification failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid runtime token") from exc


@router.get("/{app_id}/secrets/{name}")
async def get_runtime_app_secret(app_id: str, name: str, request: Request):
    """Return one plaintext secret to the app runtime after service-account verification."""
    record = get_managed_app(app_id)
    if not record or record.status == "deleted":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed app not found")
    payload = verify_runtime_identity(_bearer_token(request))
    caller_email = str(payload.get("email") or "").lower()
    expected = (record.runtime_service_account or "").lower()
    if not caller_email or caller_email != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Runtime service account not allowed")
    try:
        secret_name = normalize_secret_name(name)
        value = get_app_secret_value(app_id=app_id, name=secret_name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if value is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found")
    return {"app_id": app_id, "name": secret_name, "value": value}
