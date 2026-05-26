"""Utilities MCP — Streamable HTTP at /mcp-server with Cloud Run IAM and/or X-API-Key."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import FrozenSet, Optional

import amadeus_client
import ticketmaster_client
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
API_KEY_SECRET_ID = os.getenv("API_KEY_SECRET_ID", "utils-mcp-api-key")
AMADEUS_API_KEY_SECRET_ID = os.getenv("AMADEUS_API_KEY_SECRET_ID", "amadeus-api-key")
AMADEUS_API_SECRET_SECRET_ID = os.getenv("AMADEUS_API_SECRET_SECRET_ID", "amadeus-api-secret")
AMADEUS_API_BASE_URL = os.getenv("AMADEUS_API_BASE_URL", "https://test.api.amadeus.com")
TICKETMASTER_API_KEY_SECRET_ID = os.getenv("TICKETMASTER_API_KEY_SECRET_ID", "ticketmaster-api-key")
TICKETMASTER_API_BASE_URL = os.getenv("TICKETMASTER_API_BASE_URL", "https://app.ticketmaster.com/discovery/v2")
_DEFAULT_INVOKERS = "mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com"

API_KEY: Optional[str] = None
_GOOGLE_REQUEST = google_auth_requests.Request()


def _allowed_invoker_emails() -> FrozenSet[str]:
    raw = os.getenv("ALLOWED_INVOKER_SA_EMAILS", _DEFAULT_INVOKERS)
    return frozenset(e.strip() for e in raw.split(",") if e.strip())


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
            logger.warning("ENV=local but API_KEY not set; MCP endpoint will reject requests")

        amadeus_client.configure_api(
            os.getenv("AMADEUS_API_KEY"),
            os.getenv("AMADEUS_API_SECRET"),
            base_url=os.getenv("AMADEUS_API_BASE_URL", AMADEUS_API_BASE_URL),
        )
        ticketmaster_client.configure_api(
            os.getenv("TICKETMASTER_API_KEY"),
            base_url=os.getenv("TICKETMASTER_API_BASE_URL", TICKETMASTER_API_BASE_URL),
        )
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
            amadeus_client.configure_api(
                get_secret(AMADEUS_API_KEY_SECRET_ID).strip(),
                get_secret(AMADEUS_API_SECRET_SECRET_ID).strip(),
                base_url=AMADEUS_API_BASE_URL,
            )
            logger.info(
                "Amadeus credentials loaded from %s / %s",
                AMADEUS_API_KEY_SECRET_ID,
                AMADEUS_API_SECRET_SECRET_ID,
            )
        except Exception as exc:
            logger.warning(
                "Could not load Amadeus credentials (%s, %s): %s. Flight tools will return errors until configured.",
                AMADEUS_API_KEY_SECRET_ID,
                AMADEUS_API_SECRET_SECRET_ID,
                exc,
            )
            amadeus_client.configure_api(None, None, base_url=AMADEUS_API_BASE_URL)

        try:
            ticketmaster_client.configure_api(
                get_secret(TICKETMASTER_API_KEY_SECRET_ID).strip(),
                base_url=TICKETMASTER_API_BASE_URL,
            )
            logger.info("Ticketmaster API key loaded from %s", TICKETMASTER_API_KEY_SECRET_ID)
        except Exception as exc:
            logger.warning(
                "Could not load Ticketmaster API key (%s): %s. Event tools will return errors until configured.",
                TICKETMASTER_API_KEY_SECRET_ID,
                exc,
            )
            ticketmaster_client.configure_api(None, base_url=TICKETMASTER_API_BASE_URL)

    async with mcp.session_manager.run():
        yield

    logger.info("utils-mcp shutdown complete")


app = FastAPI(title="Utilities MCP", lifespan=lifespan)

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
