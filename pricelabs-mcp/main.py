"""PriceLabs MCP — Streamable HTTP at /mcp-server. Cloud Run IAM and/or X-API-Key."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import FrozenSet, Optional

import pricelabs_client
from dotenv import load_dotenv
from fastapi import FastAPI
from google.auth.transport import requests as google_auth_requests
from google.cloud import secretmanager
from google.oauth2 import id_token as google_id_token
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "it-team-hw-project")
ENV = os.getenv("ENV", "")
API_KEY_SECRET_ID = os.getenv("API_KEY_SECRET_ID", "pricelabs-mcp-api-key")
PRICELABS_API_KEY_SECRET_ID = os.getenv("PRICELABS_API_KEY_SECRET_ID", "pricelabs-api-key")
_DEFAULT_INVOKERS = "mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com"

API_KEY: Optional[str] = None
_GOOGLE_REQUEST = google_auth_requests.Request()


def _allowed_invoker_emails() -> FrozenSet[str]:
    raw = os.getenv("ALLOWED_INVOKER_SA_EMAILS", _DEFAULT_INVOKERS)
    return frozenset(email.strip() for email in raw.split(",") if email.strip())


def _cloud_run_caller_authorized_sync(request: Request) -> bool:
    if ENV == "local":
        return False
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[7:].strip()
    if not token:
        return False
    host = (request.headers.get("host") or "").strip()
    if not host:
        return False
    audience = f"https://{host}/"
    try:
        info = google_id_token.verify_oauth2_token(token, _GOOGLE_REQUEST, audience=audience)
    except Exception as exc:
        logger.debug("Cloud Run OIDC verification failed: %s", exc)
        return False
    email = (info.get("email") or "").strip()
    return email in _allowed_invoker_emails()


def get_secret(secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as exc:
        logger.error("Failed to fetch secret %s: %s", secret_id, exc)
        raise RuntimeError(f"Failed to fetch secret {secret_id}: {exc}") from exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global API_KEY

    from mcp_server import mcp

    if ENV == "local":
        API_KEY = os.getenv("API_KEY")
        if not API_KEY:
            logger.warning("ENV=local but API_KEY not set; MCP endpoint will reject X-API-Key requests")
        pricelabs_client.configure_client(os.getenv("PRICELABS_API_KEY"))
        if os.getenv("PRICELABS_API_KEY"):
            logger.info("PriceLabs API key loaded from PRICELABS_API_KEY")
        else:
            logger.warning("ENV=local but PRICELABS_API_KEY not set; PriceLabs tools will fail until configured")
    else:
        logger.info("Loading MCP API key from Secret Manager (%s)...", API_KEY_SECRET_ID)
        try:
            API_KEY = get_secret(API_KEY_SECRET_ID).strip()
            logger.info("MCP API key loaded")
        except Exception as exc:
            logger.warning(
                "Could not load MCP API key from Secret Manager (%s): %s. "
                "X-API-Key auth disabled; OIDC (mcp-proxy) still works.",
                API_KEY_SECRET_ID,
                exc,
            )
            API_KEY = None

        try:
            upstream_key = get_secret(PRICELABS_API_KEY_SECRET_ID).strip()
            pricelabs_client.configure_client(upstream_key)
            logger.info("PriceLabs API key loaded from %s", PRICELABS_API_KEY_SECRET_ID)
        except Exception as exc:
            logger.warning(
                "Could not load PriceLabs API key from %s: %s. Tools will return errors until configured.",
                PRICELABS_API_KEY_SECRET_ID,
                exc,
            )
            pricelabs_client.configure_client(None)

    async with mcp.session_manager.run():
        yield

    logger.info("pricelabs-mcp shutdown complete")


app = FastAPI(title="PriceLabs MCP", lifespan=lifespan)

from mcp_server import mcp  # noqa: E402

mcp_app = mcp.streamable_http_app()


@app.middleware("http")
async def mcp_api_key_guard(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if request.url.path.startswith("/mcp-server"):
        key = request.headers.get("X-API-Key")
        if key and API_KEY is not None and key == API_KEY:
            return await call_next(request)
        if await asyncio.to_thread(_cloud_run_caller_authorized_sync, request):
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


app.mount("/mcp-server", mcp_app)


@app.get("/health")
def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
