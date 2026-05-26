"""Guesty MCP Gateway - FastAPI app with MCP mount (n8n-mcp / sql-gateway pattern)."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from google.cloud import secretmanager
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ID = os.getenv("GCP_PROJECT_ID", "it-team-hw-project")
ENV = os.getenv("ENV", "")
API_KEY_SECRET_ID = os.getenv("API_KEY_SECRET_ID", "guesty-mcp-api-key")

API_KEY: Optional[str] = None


def get_secret(secret_id: str) -> str:
    """Fetches a secret from GCP Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.error("Failed to fetch secret %s: %s", secret_id, e)
        raise RuntimeError(f"Failed to fetch secret {secret_id}: {e}") from e


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load API key and optionally pre-warm GuestyService; run MCP session manager."""
    global API_KEY

    from mcp_server import mcp

    if ENV == "local":
        API_KEY = os.getenv("API_KEY")
        if not API_KEY:
            logger.warning("ENV=local but API_KEY not set; MCP endpoint will reject requests")
    else:
        logger.info("Loading API key from Secret Manager...")
        API_KEY = get_secret(API_KEY_SECRET_ID)
        logger.info("API key loaded")

    # Optionally pre-warm GuestyService so first tool call doesn't pay token cost
    try:
        from guesty_client import GuestyService

        service = GuestyService()
        await asyncio.to_thread(service._ensure_initialized)
        logger.info("GuestyService pre-warmed")
    except Exception as e:
        logger.warning("Could not pre-warm GuestyService (first tool call will initialize): %s", e)

    async with mcp.session_manager.run():
        yield

    logger.info("guesty-mcp shutdown complete")


app = FastAPI(title="Guesty MCP Gateway", lifespan=lifespan)

from mcp_server import mcp  # noqa: E402

mcp_app = mcp.streamable_http_app()


@app.middleware("http")
async def mcp_api_key_guard(request: Request, call_next):
    """Enforce API key auth on the MCP endpoint (skip health)."""
    if request.url.path == "/health":
        return await call_next(request)
    if request.url.path.startswith("/mcp-server"):
        key = request.headers.get("X-API-Key")
        if key != API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Invalid API Key"})
    return await call_next(request)


app.mount("/mcp-server", mcp_app)


@app.get("/health")
def health_check():
    """Health check endpoint for Cloud Run."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
