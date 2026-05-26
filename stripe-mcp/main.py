"""Stripe MCP server — FastAPI app with MCP mount (streamable HTTP)."""

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from google.cloud import secretmanager
from starlette.requests import Request
from starlette.responses import JSONResponse
from stripe import StripeClient

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ID = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
STRIPE_API_KEY_SECRET_NAME = os.getenv("STRIPE_API_KEY_SECRET_ID", "stripe-api-key")
API_KEY_SECRET_ID = os.getenv("API_KEY_SECRET_ID", "stripe-mcp-api-key")

_stripe_client = None
API_KEY = None


def get_secret(secret_id: str, key: str | None = None) -> str:
    """Fetch a secret from GCP Secret Manager. If key is set, expect JSON and return secret[key]."""
    project_id = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        payload = response.payload.data.decode("UTF-8").strip()
    except Exception as e:
        logger.error("Failed to fetch secret %s: %s", secret_id, e)
        raise RuntimeError(f"Failed to fetch secret {secret_id}: {e}") from e

    if key:
        try:
            data = json.loads(payload)
            return data.get(key) or payload
        except json.JSONDecodeError:
            return payload
    return payload


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load Stripe API key, create Stripe client, and run MCP session manager."""
    global _stripe_client, API_KEY

    logger.info("Loading secrets...")
    # Stripe API key: same secret name/key as shared/stripe.py — GCP Secret Manager "stripe-api-key", JSON key "stripe_api_key"
    stripe_api_key = get_secret(STRIPE_API_KEY_SECRET_NAME, "stripe_api_key") or get_secret(STRIPE_API_KEY_SECRET_NAME)
    if not stripe_api_key:
        raise ValueError(f"No API key found for stripe_api_key in secret {STRIPE_API_KEY_SECRET_NAME}")

    API_KEY = get_secret(API_KEY_SECRET_ID) if os.getenv("ENV") != "local" else os.getenv("API_KEY", "local-dev-key")

    _stripe_client = StripeClient(stripe_api_key)
    import stripe_ops as stripe_ops_module

    stripe_ops_module.set_stripe_client(_stripe_client)
    logger.info("Stripe client and MCP API key loaded")

    from mcp_server import mcp

    async with mcp.session_manager.run():
        yield

    logger.info("stripe-mcp shutdown complete")


app = FastAPI(title="Stripe MCP Gateway", lifespan=lifespan)

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
