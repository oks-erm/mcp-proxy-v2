"""QuickBooks MCP server: FastAPI app with streamable HTTP MCP endpoint."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

from anyio import BrokenResourceError, ClosedResourceError
from fastapi import FastAPI
from quickbooks_client import QuickBooksService
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY_SECRET_ID = "quickbooks-mcp-api-key"
qb_service: Optional[QuickBooksService] = None
API_KEY: Optional[str] = None


def get_api_key_from_secret_manager() -> str:
    project_id = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{API_KEY_SECRET_ID}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8").strip()
    except Exception as e:
        logger.error("Failed to fetch API key from Secret Manager: %s", e)
        raise RuntimeError(f"Failed to fetch secret {API_KEY_SECRET_ID}: {e}") from e


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    global qb_service, API_KEY

    logger.info("Initialising QuickBooks MCP server...")

    if os.getenv("ENV") == "local":
        API_KEY = os.getenv("API_KEY", "local-dev-key")
        logger.info("Using local API key")
    else:
        API_KEY = get_api_key_from_secret_manager()
        logger.info("Fetched API key from Secret Manager")

    qb_service = QuickBooksService()
    logger.info("QuickBooks service initialised (tokens from Firestore tokens/quickbooks)")

    import mcp_server as mcp_module  # noqa: E402
    from mcp_server import mcp  # noqa: E402

    mcp_module.set_qb_service(qb_service)

    async with mcp.session_manager.run():
        yield

    logger.info("QuickBooks MCP server shutdown complete")


app = FastAPI(
    title="QuickBooks MCP API",
    description="MCP server for QuickBooks bank accounts, transactions in/out, and balances",
    version="0.1.0",
    lifespan=lifespan,
)

from mcp_server import mcp  # noqa: E402

mcp_app = mcp.streamable_http_app()


@app.middleware("http")
async def mcp_api_key_guard(request: Request, call_next):
    """Enforce API key auth on the MCP endpoint."""
    if request.url.path.startswith("/mcp-server"):
        key = request.headers.get("X-API-Key")
        if key != API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Invalid API Key"})
    try:
        return await call_next(request)
    except (ClosedResourceError, BrokenResourceError) as e:
        # Client closed connection before MCP response was sent (SDK issue #1658)
        logger.warning("MCP client closed connection before response was sent: %s", e)
        return JSONResponse(
            status_code=500,
            content={"detail": "Connection closed by client before response could be sent"},
        )


app.mount("/mcp-server", mcp_app)


@app.get("/health")
def health_check():
    """Health check endpoint for Cloud Run."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
