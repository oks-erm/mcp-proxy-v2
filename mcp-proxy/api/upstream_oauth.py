"""Routes: per-user upstream OAuth2 link, callback, list connections, disconnect."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlencode

import config
from api.deps import get_current_user, verify_csrf_for_unsafe_methods
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from firestore_store import get_server, list_servers
from models import UpstreamOAuthConnectionsResponse, UpstreamOAuthConnectionStatus
from permissions.store import get_user_permissions
from upstream_oauth.http_client import exchange_authorization_code
from upstream_oauth.pkce import code_challenge_s256, new_code_verifier
from upstream_oauth.secrets import fetch_secret_string
from upstream_oauth.state import sign_oauth_state, verify_oauth_state
from upstream_oauth.token_store import (
    delete_token_doc,
    get_token_doc,
    parse_token_response,
    save_tokens_after_exchange,
)
from users.schemas import UserInDB

logger = logging.getLogger(__name__)


def _allowed_server_ids(user: UserInDB) -> set[str]:
    if not user or user.status != "active":
        return set()
    if user.role in ("admin", "power_user"):
        return {s.id for s in list_servers(enabled_only=True)}
    return {p.server_id for p in get_user_permissions(user.id) if p.read or p.write}


me_router = APIRouter(
    prefix="/me/upstream-oauth",
    tags=["me"],
)
callback_router = APIRouter(prefix="/auth", tags=["auth"])


def _public_base_url() -> str:
    base = (config.MCP_PROXY_URL or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MCP_PROXY_URL is not configured (required for OAuth callback)",
        )
    return base


def _callback_uri() -> str:
    return f"{_public_base_url()}/auth/upstream-oauth/callback"


def _append_query(url: str, params: dict) -> str:
    q = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{q}"


@me_router.get("/connections", response_model=UpstreamOAuthConnectionsResponse)
async def list_upstream_oauth_connections(user: UserInDB = Depends(get_current_user)):
    """OAuth2 upstreams the user may access and whether they have linked."""
    if user.status != "active":
        raise HTTPException(status_code=403, detail="User not approved")
    allowed = _allowed_server_ids(user)
    enabled = list_servers(enabled_only=True)
    deployment_oauth2 = sum(1 for s in enabled if s.upstream_auth == "oauth2")
    out: list[UpstreamOAuthConnectionStatus] = []
    for s in enabled:
        if s.upstream_auth != "oauth2" or s.id not in allowed:
            continue
        row = get_token_doc(user.id, s.id)
        out.append(UpstreamOAuthConnectionStatus(server_id=s.id, connected=bool(row and row.get("refresh_enc"))))
    return UpstreamOAuthConnectionsResponse(servers=out, deployment_oauth2_server_count=deployment_oauth2)


@me_router.get("/{server_id}/authorize")
async def start_upstream_oauth(
    server_id: str,
    user: UserInDB = Depends(get_current_user),
    next: str = Query("/app/account", description="Redirect path after success (relative)"),
):
    if user.status != "active":
        raise HTTPException(status_code=403, detail="User not approved")
    allowed = _allowed_server_ids(user)
    if server_id not in allowed:
        raise HTTPException(status_code=403, detail="Not permitted for this server")
    srv = get_server(server_id)
    if not srv or not srv.enabled or srv.upstream_auth != "oauth2" or not srv.oauth:
        raise HTTPException(status_code=404, detail="OAuth2 server not found")
    oauth = srv.oauth
    verifier = new_code_verifier() if oauth.use_pkce else ""
    state = sign_oauth_state(
        {
            "user_id": user.id,
            "server_id": server_id,
            "v": verifier,
            "next": next if next.startswith("/") else "/app/account",
        }
    )
    redirect_uri = _callback_uri()
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": oauth.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if oauth.scopes.strip():
        params["scope"] = oauth.scopes.strip()
    if oauth.use_pkce and verifier:
        params["code_challenge"] = code_challenge_s256(verifier)
        params["code_challenge_method"] = "S256"
    url = _append_query(oauth.authorization_endpoint, params)
    return RedirectResponse(url=url, status_code=302)


@callback_router.get("/upstream-oauth/callback")
async def upstream_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    base = (config.MCP_PROXY_URL or "").strip().rstrip("/") or "http://localhost:8080"
    dest_fail = f"{base}/app/account?upstream_oauth_error=1"
    if error:
        logger.warning("OAuth error from IdP: %s %s", error, error_description)
        return RedirectResponse(f"{dest_fail}&reason={error}", status_code=302)
    if not code or not state:
        return RedirectResponse(dest_fail, status_code=302)
    try:
        payload = verify_oauth_state(state)
    except ValueError as e:
        logger.warning("OAuth state invalid: %s", e)
        return RedirectResponse(dest_fail, status_code=302)
    user_id = str(payload["user_id"])
    server_id = str(payload["server_id"])
    verifier = str(payload.get("v") or "")
    next_path = str(payload.get("next") or "/app/account")
    srv = get_server(server_id)
    if not srv or srv.upstream_auth != "oauth2" or not srv.oauth:
        return RedirectResponse(dest_fail, status_code=302)
    oauth = srv.oauth
    client_secret = ""
    if oauth.client_secret_secret_id:
        client_secret = await asyncio.to_thread(fetch_secret_string, oauth.client_secret_secret_id)
    try:
        body = await exchange_authorization_code(
            oauth.token_endpoint,
            code=code,
            redirect_uri=_callback_uri(),
            client_id=oauth.client_id,
            client_secret=client_secret or None,
            code_verifier=verifier if oauth.use_pkce else None,
            audience=oauth.audience,
            extra_form=oauth.extra_token_params,
        )
        access, refresh, expires_in, token_type = parse_token_response(body)
    except Exception as e:
        logger.warning("Token exchange failed: %s", e)
        return RedirectResponse(dest_fail, status_code=302)
    if not refresh:
        logger.warning("Token response had no refresh_token for server %s", server_id)
        return RedirectResponse(dest_fail, status_code=302)
    try:
        save_tokens_after_exchange(
            user_id,
            server_id,
            access_token=access,
            refresh_token=refresh,
            expires_in=expires_in,
            token_type=token_type,
        )
    except Exception as e:
        logger.exception("Failed to save OAuth tokens: %s", e)
        return RedirectResponse(dest_fail, status_code=302)
    ok = f"{base}{next_path}"
    sep = "&" if "?" in ok else "?"
    return RedirectResponse(f"{ok}{sep}upstream_oauth_connected={server_id}", status_code=302)


@me_router.delete(
    "/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(verify_csrf_for_unsafe_methods)],
)
async def disconnect_upstream_oauth(server_id: str, user: UserInDB = Depends(get_current_user)):
    if user.status != "active":
        raise HTTPException(status_code=403, detail="User not approved")
    allowed = _allowed_server_ids(user)
    if server_id not in allowed:
        raise HTTPException(status_code=403, detail="Not permitted for this server")
    delete_token_doc(user.id, server_id)
