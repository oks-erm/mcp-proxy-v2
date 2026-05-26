"""MCP Proxy - FastAPI app with REST admin API and MCP proxy mount."""

import logging
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from api.admin_users import router as admin_users_router
from api.auth_routes import router as auth_router
from api.managed_app_runtime import router as managed_app_runtime_router
from api.me import router as me_router
from api.skill_catalog import router as skill_catalog_router
from api.upstream_oauth import callback_router as upstream_oauth_callback_router
from api.upstream_oauth import me_router as upstream_oauth_me_router
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from firestore_store import verify_firestore_connection
from logging_config import setup_logging
from mcp_proxy import create_mcp_proxy_app
from rest_api import router as admin_router
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

setup_logging()
logger = logging.getLogger(__name__)

# Alias for tests (see tests/conftest.py)
verify_servers_connection = verify_firestore_connection


class SPAStaticFiles(StaticFiles):
    """
    Serves the Vite/React app: unknown paths (e.g. /app/dashboard) return index.html
    so client-side routes work on refresh. Real 404s under assets/ stay 404.
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not self.html:
                raise
            if path.startswith("assets/"):
                raise
        full_path, stat_result = await anyio.to_thread.run_sync(self.lookup_path, "index.html")
        if stat_result is None or not stat.S_ISREG(stat_result.st_mode):
            raise StarletteHTTPException(status_code=404)
        return self.file_response(full_path, stat_result, scope)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Verify Firestore connectivity at startup (server configs)."""
    verify_servers_connection()
    yield
    logger.info("MCP Proxy shutdown complete")


app = FastAPI(
    title="MCP Proxy",
    description="Single entry point for multiple MCP servers. Use the admin API to manage upstream server configs.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(managed_app_runtime_router)
app.include_router(upstream_oauth_callback_router)
app.include_router(me_router)
app.include_router(upstream_oauth_me_router)
app.include_router(admin_users_router)
app.include_router(admin_router)
app.include_router(skill_catalog_router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions; let HTTPException pass through to default handler."""
    if isinstance(exc, HTTPException):
        raise exc
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred"},
    )


mcp_proxy_app = create_mcp_proxy_app()
app.mount("/mcp-server", mcp_proxy_app)

_WEB_DIST = Path(__file__).resolve().parent / "web" / "dist"
if _WEB_DIST.is_dir():
    app.mount("/app", SPAStaticFiles(directory=str(_WEB_DIST), html=True), name="spa")


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """HSTS (non-local), CSP, and standard hardening headers."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if os.getenv("ENV", "local") != "local":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Lets Google Sign-In popup/postMessage work with the opener; default COOP is too strict for GIS.
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    csp = (
        "default-src 'self'; "
        "script-src 'self' https://accounts.google.com https://apis.google.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://accounts.google.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com; "
        "frame-src https://accounts.google.com;"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


@app.get("/health")
def health_check():
    """Health check for Cloud Run."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)
