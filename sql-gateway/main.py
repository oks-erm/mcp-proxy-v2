import json
import logging
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import PlainTextResponse
from fastapi.security import APIKeyHeader
from google.cloud import secretmanager
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "it-team-hw-project")
DB_CREDENTIALS_SECRET_ID = "portal-db-credentials"
API_KEY_SECRET_ID = "sql-gateway-api-key"

db_engine: Optional[Engine] = None
API_KEY: Optional[str] = None

RESOURCES_DIR = Path(__file__).parent / "resources"
MODELS_MD_PATH = RESOURCES_DIR / "models.md"
SCHEMA_YAML_PATH = RESOURCES_DIR / "schema.yaml"

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class QueryRequest(BaseModel):
    sql: str
    page: int = 1
    page_size: int = 100


def get_secret(secret_id: str) -> str:
    """Fetches a secret from GCP Secret Manager."""
    if not PROJECT_ID:
        raise RuntimeError("GCP_PROJECT_ID environment variable is not set.")

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"

    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.error(f"Failed to fetch secret {secret_id}: {e}")
        raise RuntimeError(f"Failed to fetch secret {secret_id}: {e}")


def get_db_credentials() -> Dict[str, Any]:
    """Fetches and parses database credentials from GCP Secret Manager."""
    payload = get_secret(DB_CREDENTIALS_SECRET_ID)
    return json.loads(payload)


def get_api_key() -> str:
    """Fetches the API Key from GCP Secret Manager."""
    return get_secret(API_KEY_SECRET_ID)


def create_db_connection_string(creds: Dict[str, Any]) -> str:
    """Constructs the PostgreSQL connection string from credentials."""
    user = creds.get("db_portal_username")
    password = creds.get("db_portal_password")

    env = os.getenv("ENV", "local")

    if env == "local":
        host = "localhost"
        port = creds.get("db_warehouse_port", 5432)
        dbname = "postgres"
        return f"postgresql://postgres:postgres@{host}:{port}/{dbname}"
    else:
        raw_host = creds.get("db_warehouse_host")
        if raw_host and not raw_host.endswith("-replica"):
            raw_host = f"{raw_host}-replica"

        socket_path = f"/cloudsql/{raw_host}"
        dbname = creds.get("db_portal_name", "postgres")
        return f"postgresql://{user}:{password}@/{dbname}?host={socket_path}"


async def verify_api_key(x_api_key: str = Security(api_key_header)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    global db_engine, API_KEY

    # --- Startup ---
    logger.info("Fetching credentials and connecting to database...")

    if os.getenv("ENV") == "local":
        API_KEY = os.getenv("API_KEY", "local-dev-key")
        logger.info("Using local API Key")
    else:
        API_KEY = get_api_key()
        logger.info("Fetched API Key from Secret Manager")

    creds = get_db_credentials()
    db_url = create_db_connection_string(creds)
    db_engine = create_engine(db_url, pool_pre_ping=True)

    with db_engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        logger.info(f"Database connection verified: {result.scalar()}")

    # Mount MCP after db_engine is ready
    from mcp_server import mcp

    async with mcp.session_manager.run():
        yield

    # --- Shutdown ---
    if db_engine:
        db_engine.dispose()
        logger.info("Database engine disposed")


app = FastAPI(title="SQL Gateway", lifespan=lifespan)


# Mount MCP as a sub-application with API key auth middleware
from mcp_server import mcp  # noqa: E402

mcp_app = mcp.streamable_http_app()


@app.middleware("http")
async def mcp_api_key_guard(request: Request, call_next):
    """Enforce API key auth on the MCP endpoint, matching the REST /query auth."""
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


def _read_schema_markdown_text() -> str:
    if not MODELS_MD_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Schema file not found.",
                "details": f"Missing required schema document at {MODELS_MD_PATH}",
            },
        )
    return MODELS_MD_PATH.read_text(encoding="utf-8")


def _read_schema_yaml_text() -> str:
    if not SCHEMA_YAML_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Schema YAML file not found.",
                "details": f"Missing required schema document at {SCHEMA_YAML_PATH}",
            },
        )
    return SCHEMA_YAML_PATH.read_text(encoding="utf-8")


@app.get("/schema", response_class=PlainTextResponse)
def get_schema():
    """Returns the database schema documentation as Markdown."""
    return PlainTextResponse(
        content=_read_schema_markdown_text(),
        media_type="text/markdown",
    )


@app.get("/schema/yaml", response_class=PlainTextResponse)
def get_schema_yaml():
    """Returns the database schema documentation as YAML."""
    return PlainTextResponse(
        content=_read_schema_yaml_text(),
        media_type="application/yaml",
    )


@app.post("/query", dependencies=[Depends(verify_api_key)])
def run_query(request: QueryRequest):
    """Executes a read-only SQL query with pagination."""
    sql = request.sql.strip().rstrip(";")

    if not sql.upper().startswith("SELECT"):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed.")

    limit = request.page_size
    offset = (request.page - 1) * request.page_size

    count_sql = f"SELECT COUNT(*) FROM ({sql}) AS count_query"
    paginated_sql = f"SELECT * FROM ({sql}) AS subquery LIMIT {limit} OFFSET {offset}"

    try:
        with db_engine.connect() as connection:
            count_result = connection.execute(text(count_sql))
            total_rows = count_result.scalar()

            result = connection.execute(text(paginated_sql))
            rows = [dict(row._mapping) for row in result]

            total_pages = math.ceil(total_rows / request.page_size)

            return {"data": rows, "page": request.page, "page_size": request.page_size, "total_pages": total_pages}
    except Exception as e:
        logger.error(f"Query execution failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
