"""FastAPI entrypoint for the Breezeway MCP server."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from google.cloud import secretmanager
from starlette.requests import Request
from starlette.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from breezeway_client import BreezewayClient  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY_SECRET_ID = os.getenv("API_KEY_SECRET_ID", "breezeway-mcp-api-key")

breezeway_service: Optional[BreezewayClient] = None
API_KEY: Optional[str] = None


def get_api_key_from_secret_manager() -> str:
    project_id = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{API_KEY_SECRET_ID}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8").strip()
    except Exception as exc:
        logger.error("Failed to fetch API key from Secret Manager: %s", exc)
        raise RuntimeError("Unable to load MCP API key") from exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global breezeway_service, API_KEY

    logger.info("Starting Breezeway MCP lifespan")
    if os.getenv("ENV") == "local":
        API_KEY = os.getenv("API_KEY", "local-dev-key")
        logger.info("Using local API key")
    else:
        API_KEY = get_api_key_from_secret_manager()
        logger.info("Loaded API key from Secret Manager")

    breezeway_service = BreezewayClient()
    logger.info("Breezeway client initialised (tokens/breezeway)")

    import mcp_server as mcp_module  # noqa: E402
    from mcp_server import mcp  # noqa: E402

    mcp_module.set_breezeway_service(breezeway_service)

    async with mcp.session_manager.run():
        yield

    logger.info("Breezeway MCP shutdown complete")


app = FastAPI(title="Breezeway MCP", lifespan=lifespan)

from mcp_server import mcp  # noqa: E402

mcp_app = mcp.streamable_http_app()


@app.middleware("http")
async def mcp_api_key_guard(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if request.url.path.startswith("/mcp-server"):
        key = request.headers.get("X-API-Key")
        if key != API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Invalid API Key"})
    return await call_next(request)


@app.get("/health")
def health_check():
    return {"status": "ok"}


app.mount("/mcp-server", mcp_app)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
