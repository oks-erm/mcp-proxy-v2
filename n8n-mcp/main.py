"""n8n MCP Gateway - FastAPI app with MCP mount (sql-gateway pattern)."""

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
API_KEY_SECRET_ID = os.getenv("API_KEY_SECRET_ID", "n8n-mcp-api-key")
N8N_API_KEY_SECRET_ID = os.getenv("N8N_API_KEY_SECRET_ID", "n8n-api-key")
N8N_ADMIN_API_KEY_SECRET_ID = os.getenv("N8N_ADMIN_API_KEY_SECRET_ID", "n8n-admin-api-key")
N8N_BASE_URL_SECRET_ID = os.getenv("N8N_BASE_URL_SECRET_ID", "n8n-base-url")
GITHUB_TOKEN_SECRET_ID = os.getenv("GITHUB_TOKEN_SECRET_ID", "n8n-mcp-github-token")

API_KEY: Optional[str] = None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_secret(secret_id: str) -> str:
    """Fetches a secret from GCP Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.error(f"Failed to fetch secret {secret_id}: {e}")
        raise RuntimeError(f"Failed to fetch secret {secret_id}: {e}") from e


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load secrets, set n8n_client.N8N_API_KEY and N8N_BASE_URL, and run MCP session manager."""
    global API_KEY

    import n8n_client
    from mcp_server import mcp

    logger.info("Loading secrets from Secret Manager...")
    API_KEY = get_secret(API_KEY_SECRET_ID)
    try:
        n8n_client.N8N_API_KEY = get_secret(N8N_API_KEY_SECRET_ID)
    except Exception:
        n8n_client.N8N_API_KEY = os.getenv("N8N_API_KEY")
        if n8n_client.N8N_API_KEY:
            logger.info("Using N8N_API_KEY from environment (Secret Manager unavailable)")
        else:
            raise

    admin_api_key = None
    try:
        admin_api_key = get_secret(N8N_ADMIN_API_KEY_SECRET_ID)
    except Exception:
        admin_api_key = os.getenv("N8N_ADMIN_API_KEY")
        if admin_api_key:
            logger.info("Using N8N_ADMIN_API_KEY from environment (Secret Manager unavailable)")
        else:
            logger.warning(
                "N8N_ADMIN_API_KEY not set (secret %s missing). Credential tools will fail until configured.",
                N8N_ADMIN_API_KEY_SECRET_ID,
            )
    n8n_client.N8N_ADMIN_API_KEY = admin_api_key

    # N8N_BASE_URL: prefer env (e.g. from --set-env-vars), then Secret Manager
    base_url = os.getenv("N8N_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        try:
            base_url = get_secret(N8N_BASE_URL_SECRET_ID).strip().rstrip("/")
            logger.info("Using N8N_BASE_URL from Secret Manager (%s)", N8N_BASE_URL_SECRET_ID)
        except Exception:
            pass
    n8n_client.N8N_BASE_URL = base_url
    if not n8n_client.N8N_BASE_URL:
        logger.warning(
            "N8N_BASE_URL not set (no env and no secret %s). n8n API tools will fail until configured.",
            N8N_BASE_URL_SECRET_ID,
        )

    # GitHub token: Secret Manager first, then env (for node browse/search tools)
    try:
        n8n_client.GITHUB_TOKEN = get_secret(GITHUB_TOKEN_SECRET_ID).strip() or None
        if n8n_client.GITHUB_TOKEN:
            logger.info("Using GITHUB_TOKEN from Secret Manager (%s)", GITHUB_TOKEN_SECRET_ID)
    except Exception:
        n8n_client.GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or None
        if n8n_client.GITHUB_TOKEN:
            logger.info("Using GITHUB_TOKEN from environment (Secret Manager unavailable)")

    logger.info("Secrets loaded")

    async with mcp.session_manager.run():
        yield

    logger.info("n8n-mcp shutdown complete")


app = FastAPI(title="n8n MCP Gateway", lifespan=lifespan)

from mcp_server import mcp  # noqa: E402

mcp_app = mcp.streamable_http_app()


@app.middleware("http")
async def mcp_api_key_guard(request: Request, call_next):
    """Enforce API key auth on the MCP endpoint (skip health)."""
    if request.url.path == "/health":
        return await call_next(request)
    # MCP streamable HTTP app registers at /mcp, so full path is /mcp-server/mcp
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
    reload_enabled = _env_bool("UVICORN_RELOAD", default=False)
    # Uvicorn requires an import string (not app object) when reload is enabled.
    app_target = "main:app" if reload_enabled else app
    uvicorn.run(app_target, host="0.0.0.0", port=port, reload=reload_enabled)
