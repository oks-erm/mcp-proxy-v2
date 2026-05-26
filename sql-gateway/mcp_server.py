import logging
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.types import CallToolResult
from mcp_platform.coerce_numeric import coerce_int
from mcp_platform.detail_level import parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

RESOURCES_DIR = Path(__file__).parent / "resources"
MODELS_MD_PATH = RESOURCES_DIR / "models.md"
SCHEMA_YAML_PATH = RESOURCES_DIR / "schema.yaml"
_MODELS_MISSING_MSG = (
    "Schema file (resources/models.md) not found. Ensure resources/models.md is deployed "
    "alongside the SQL Gateway service."
)
_SCHEMA_YAML_MISSING_MSG = (
    "Schema file (resources/schema.yaml) not found. Ensure resources/schema.yaml is deployed "
    "alongside the SQL Gateway service."
)


def _read_schema() -> str:
    """Read resources/models.md and fail loudly if missing."""
    if not MODELS_MD_PATH.exists():
        logger.warning("resources/models.md not found at %s", MODELS_MD_PATH)
        raise FileNotFoundError(_MODELS_MISSING_MSG)
    return MODELS_MD_PATH.read_text(encoding="utf-8")


def _read_schema_yaml() -> str:
    """Read resources/schema.yaml and fail loudly if missing."""
    if not SCHEMA_YAML_PATH.exists():
        logger.warning("resources/schema.yaml not found at %s", SCHEMA_YAML_PATH)
        raise FileNotFoundError(_SCHEMA_YAML_MISSING_MSG)
    return SCHEMA_YAML_PATH.read_text(encoding="utf-8")


def _schema_ok(*, format_name: str, content: str, tool: str, detail_level: str = "full") -> dict:
    """Success payload for schema discovery tools wrapped in canonical MCP envelope."""
    payload = {"format": format_name, "content": content, "detail_level": detail_level}
    return with_response_meta(payload, tool=tool)


# Strip leading whitespace/comments before checking the first keyword.
_SQL_PREFIX_RE = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)
# Remove single-quoted string literals before write-keyword scan to avoid
# false positives on queries like: SELECT * FROM t WHERE action = 'DELETE'
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_FORBIDDEN_WRITE_KEYWORDS_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|GRANT|REVOKE|CALL|COPY)\b"
)


def _validate_read_only_sql(sql: str) -> tuple[bool, str | None]:
    """Validate query is a single read-only SELECT/CTE statement."""
    stripped = _SQL_PREFIX_RE.sub("", sql).lstrip()
    if not stripped:
        return False, "SQL query is empty."

    upper = stripped.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return False, "Only read-only SELECT queries are allowed (SELECT or WITH ... SELECT)."

    sql_no_literals = _STRING_LITERAL_RE.sub("''", stripped)
    if ";" in sql_no_literals:
        return False, "Multiple SQL statements are not allowed."

    forbidden = _FORBIDDEN_WRITE_KEYWORDS_RE.search(sql_no_literals.upper())
    if forbidden:
        return False, (f"Detected write keyword '{forbidden.group(1)}'; only read-only queries are allowed.")

    return True, None


@dataclass
class AppContext:
    db: Engine


@asynccontextmanager
async def mcp_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    from main import db_engine

    if db_engine is None:
        raise RuntimeError("Database engine not initialised — MCP lifespan started before REST startup?")
    yield AppContext(db=db_engine)


mcp = FastMCP(
    "SQL Gateway MCP",
    instructions=(
        "Checklist:\n"
        "1. Call get_schema (or read schema://models) to understand table purpose, trusted join "
        "paths, high-value filters, and pitfalls.\n"
        "2. Call lookup_table(table_name) when you need exact columns for a specific table — "
        "much cheaper than loading the full schema.yaml.\n"
        "3. Call get_schema_yaml (or read schema://yaml) only when you need to search across all "
        "tables at once.\n"
        "4. Verify your SQL is read-only: starts with SELECT or WITH, no semicolons, no write "
        "keywords.\n"
        "5. Call run_query(sql=..., page=1, page_size=100). Pass count_rows=False when total "
        "page count is not required — skips an expensive COUNT(*).\n"
        "6. Paginate: inspect next_page in the response; call again with page+1 while not None.\n\n"
        "Discovery example:\n"
        "  get_schema()  # understand domain semantics and safe join paths\n"
        "  lookup_table('reservations_reservation')  # get exact column list\n"
        "  run_query(\n"
        "    sql='SELECT id, check_in FROM reservations_reservation',\n"
        "    page=1, page_size=50, count_rows=False\n"
        "  )\n\n"
        "Call order: 1 → 2 (or 3) → 5. Never skip step 1."
    ),
    stateless_http=True,
    json_response=True,
    lifespan=mcp_lifespan,
    host="0.0.0.0",
)


@mcp.resource("schema://models")
def get_schema_resource() -> str:
    """Full database schema reference (all Django models, fields, types, constraints, and relationships)."""
    try:
        return _read_schema()
    except FileNotFoundError as exc:
        return f"ERROR: Schema markdown unavailable.\nDETAILS: {exc}"


@mcp.resource("schema://yaml")
def get_schema_yaml_resource() -> str:
    """Structured schema index for search-first table/column discovery in YAML format."""
    try:
        return _read_schema_yaml()
    except FileNotFoundError as exc:
        return f"ERROR: Schema YAML unavailable.\nDETAILS: {exc}"


@mcp.tool(structured_output=False)
def get_schema() -> CallToolResult:
    """Return the narrative SQL schema guide (markdown) for joins, pitfalls, and workflow context.

    Prefer this before ad-hoc ``run_query``; pair with ``lookup_table`` or resource ``schema://yaml`` for exact columns.

    No parameters; content mirrors MCP resource ``schema://models`` (proxied as ``sql_gateway://schema/models``).

    **Use when:**
        You need business meaning, trusted join paths, query workflow guidance, and common pitfalls before writing SQL.

    **Args:**
        (none)

    **Returns:**
        On success: ``{"format": "markdown", "content": string, "detail_level": "full", "meta": {tool, schema_version}}``.

    **Notes:**
        Narrative guide; use get_schema_yaml or lookup_table for exact column lists. Resource schema://models mirrors content.

    **Errors:**
        ``{"error": "schema_not_found", "details": string}`` when the file is missing.

    **Example:**
        ``get_schema()``
    """
    try:
        return structured_result(
            _schema_ok(format_name="markdown", content=_read_schema(), tool="sql_gateway_get_schema")
        )
    except FileNotFoundError as exc:
        return structured_result(tool_error("schema_not_found", details=str(exc), cause="not_found", retryable=False))


@mcp.tool(structured_output=False)
def get_schema_yaml() -> CallToolResult:
    """Return the full schema index as YAML (machine-readable tables and columns).

    Prefer ``get_schema`` for semantics; prefer ``lookup_table`` when you already know one table name.

    No parameters; same data as resource ``schema://yaml`` (``sql_gateway://schema/yaml`` via proxy).

    **Use when:**
        You need structured search across tables and columns in one payload.

    **Args:**
        (none)

    **Returns:**
        On success: ``{"format": "yaml", "content": string, "detail_level": "full", "meta": {tool, schema_version}}``.

    **Notes:**
        Prefer get_schema for semantics; prefer lookup_table when you know the table name.

    **Errors:**
        ``{"error": "schema_not_found", "details": string}`` when the YAML file is missing.

    **Example:**
        ``get_schema_yaml()``
    """
    try:
        return structured_result(
            _schema_ok(format_name="yaml", content=_read_schema_yaml(), tool="sql_gateway_get_schema_yaml")
        )
    except FileNotFoundError as exc:
        return structured_result(tool_error("schema_not_found", details=str(exc), cause="not_found", retryable=False))


@mcp.tool(structured_output=False)
def lookup_table(table_name: str, detail_level: str = "full") -> CallToolResult:
    """Return column metadata for one warehouse table (exact or unique prefix match).

    Prefer this over loading full YAML when you know ``table_name``; use ``get_schema`` for join guidance and pitfalls.

    Input: ``table_name`` string; optional ``detail_level``.

    **Use when:**
        You know or suspect the table name and want focused column metadata.

    **Args:**
        table_name: Exact name (case-insensitive) or unique prefix; ambiguous prefix returns error with candidates.
        detail_level: Echoed on success (default full — full column metadata).

    **Returns:**
        On success: ``{"table_name", "summary", "columns", "detail_level"}``.

    **Notes:**
        Prefer get_schema for join paths and pitfalls.

    **Errors:**
        ``{"error", "details"}`` for invalid input, ambiguous prefix, missing schema, parse errors, or not found.

    **Example:**
        ``lookup_table(table_name="reservations_reservation")``
    """
    dl = parse_detail_level(detail_level, default="full")
    if not SCHEMA_YAML_PATH.exists():
        return structured_result(
            tool_error(
                "schema_not_found",
                details=_SCHEMA_YAML_MISSING_MSG,
                table_name=table_name,
                cause="not_found",
                retryable=False,
            )
        )

    needle = table_name.strip().lower()
    if not needle:
        return structured_result(
            tool_error(
                "validation_error",
                details="Parameter 'table_name' must be a non-empty string.",
                cause="validation",
                retryable=False,
            )
        )

    try:
        schema_data = yaml.safe_load(SCHEMA_YAML_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return structured_result(
            tool_error("schema_parse_error", details=str(exc), cause="upstream_error", retryable=False)
        )

    if not isinstance(schema_data, dict):
        return structured_result(
            tool_error(
                "schema_invalid",
                details="Expected a YAML object with top-level key 'tables'.",
                cause="upstream_error",
                retryable=False,
            )
        )

    tables = schema_data.get("tables", [])
    if not isinstance(tables, list):
        return structured_result(
            tool_error(
                "schema_invalid",
                details="Expected top-level 'tables' to be a list.",
                cause="upstream_error",
                retryable=False,
            )
        )

    match = next((t for t in tables if t.get("name", "").lower() == needle), None)
    if match is None:
        prefix_matches = [t for t in tables if t.get("name", "").lower().startswith(needle)]
        if len(prefix_matches) == 1:
            match = prefix_matches[0]
        elif len(prefix_matches) > 1:
            names = ", ".join(sorted(t.get("name", "") for t in prefix_matches))
            return structured_result(
                tool_error(
                    "ambiguous_prefix",
                    details=f"Multiple tables match prefix {table_name!r}. Matches: {names}",
                    cause="validation",
                    retryable=False,
                )
            )

    if match is None:
        available = sorted(t.get("name", "") for t in tables)
        return structured_result(
            tool_error(
                "table_not_found",
                details=f"Table not in schema. Available: {', '.join(available)}",
                cause="not_found",
                retryable=False,
            )
        )

    return structured_result(
        with_response_meta(
            {
                "table_name": match.get("name"),
                "summary": match.get("summary"),
                "columns": match.get("columns", []),
                "detail_level": dl,
            },
            tool="sql_gateway_lookup_table",
            data_from="columns",
        )
    )


@mcp.tool(structured_output=False)
def run_query(
    sql: str,
    page: int = 1,
    page_size: int = 100,
    count_rows: bool = True,
    detail_level: str = "full",
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """Execute one read-only ``SELECT`` (or ``WITH … SELECT``) with paging and optional row counts.

    Use only after ``get_schema`` / ``lookup_table`` (or schema resources); avoid guessing table/column names.

    Input: ``sql`` string plus ``page``, ``page_size`` (1–500), and ``count_rows`` flag.

    **Use when:**
        You need tabular data after get_schema / lookup_table as needed.

    **Args:**
        sql: Single read-only SELECT or WITH … SELECT; semicolons stripped; write keywords rejected.
        page: 1-based page index.
        page_size: Rows per page (1–500).
        count_rows: When true, total_pages is exact via COUNT(*); when false, total_pages is null and next_page inferred from full page.
        detail_level: Echoed on success (``full`` default; row shape unchanged).

    **Returns:**
        On success: ``{"data", "page", "page_size", "total_pages", "next_page", "detail_level"}`` only — no nested error channel.

    **Notes:**
        ``data`` may be ``[]`` for an empty page (still success). Sensitive columns may be present; handle carefully.

    **Errors:**
        ``{"error", "details"}`` only — no ``data`` key on failure.

    **Example:**
        ``run_query(sql="SELECT 1 AS n", page=1, page_size=10, count_rows=False)``
    """
    sql = sql.strip().rstrip(";")
    dl = parse_detail_level(detail_level, default="full")
    page = coerce_int(page, default=1, minimum=1)
    page_size = coerce_int(page_size, default=100, minimum=1, maximum=500)

    def _ok(
        *,
        data,
        page_value: int,
        page_size_value: int,
        total_pages_value: int | None,
        has_more_value: bool | None = None,
    ) -> CallToolResult:
        if has_more_value is not None:
            has_more = has_more_value
        else:
            has_more = (page_value < total_pages_value) if total_pages_value is not None else None
        next_page = (page_value + 1) if has_more else None
        # Canonical meta.pagination: map page-based params to standard offset vocabulary.
        # Keep flat legacy fields (page, total_pages, next_page) for backward compat.
        offset = (page_value - 1) * page_size_value
        pagination = build_pagination_meta(
            limit=page_size_value,
            offset=offset,
            has_more=has_more,
            next_offset=offset + page_size_value if has_more else None,
            total_count=total_pages_value * page_size_value if total_pages_value is not None else None,
        )
        return structured_result(
            with_response_meta(
                {
                    "data": data,
                    "page": page_value,
                    "page_size": page_size_value,
                    "total_pages": total_pages_value,
                    "next_page": next_page,
                    "detail_level": dl,
                },
                tool="sql_gateway_run_query",
                pagination=pagination,
            )
        )

    if page < 1:
        return structured_result(
            tool_error(
                "validation_error",
                details="Parameter 'page' must be >= 1.",
                page=page,
                page_size=page_size,
                cause="validation",
                retryable=False,
            )
        )

    if page_size < 1 or page_size > 500:
        return structured_result(
            tool_error(
                "validation_error",
                details="Parameter 'page_size' must be between 1 and 500.",
                page=page,
                page_size=page_size,
                cause="validation",
                retryable=False,
            )
        )

    is_valid_sql, validation_error = _validate_read_only_sql(sql)
    if not is_valid_sql:
        return structured_result(
            tool_error(
                "sql_not_allowed",
                details=validation_error or "Invalid SQL.",
                page=page,
                page_size=page_size,
                cause="validation",
                retryable=False,
            )
        )

    limit = page_size
    offset = (page - 1) * page_size
    paginated_sql = f"SELECT * FROM ({sql}) AS subquery LIMIT {limit} OFFSET {offset}"

    engine = ctx.request_context.lifespan_context.db

    try:
        with engine.connect() as connection:
            if count_rows:
                count_sql = f"SELECT COUNT(*) FROM ({sql}) AS count_query"
                total_rows = connection.execute(text(count_sql)).scalar()
                total_pages = math.ceil(total_rows / page_size) if total_rows else 0
            else:
                total_pages = None

            result = connection.execute(text(paginated_sql))
            rows = [dict(row._mapping) for row in result]
            has_more = len(rows) == page_size if not count_rows else None

            return _ok(
                data=rows,
                page_value=page,
                page_size_value=page_size,
                total_pages_value=total_pages,
                has_more_value=has_more,
            )
    except Exception as e:
        logger.error("MCP query execution failed: %s", e)
        return structured_result(
            tool_error(
                "query_execution_failed",
                details=str(e),
                page=page,
                page_size=page_size,
                cause="upstream_error",
                retryable=True,
                suggested_fix="Check the SQL statement and retry.",
            )
        )


@mcp.prompt()
def sql_assistant(question: str) -> str:
    """Generate a SQL query prompt with semantic schema context.

    **Args:**
        question: The business question to answer with SQL.
    """
    try:
        schema_md = _read_schema()
    except FileNotFoundError as exc:
        schema_md = f"Schema unavailable: {exc}"

    return (
        "You are a SQL expert for a property management portal (PostgreSQL).\n\n"
        "The semantic schema guide (business meaning, join paths, pitfalls) is already included below. "
        "Do not call get_schema for the same prose unless you need a fresh copy outside this prompt; "
        "the equivalent tool is get_schema() and the MCP resource is schema://models "
        "(via mcp-proxy: sql_gateway_get_schema, resource sql_gateway://schema/models).\n\n"
        "Available MCP tools for this workflow:\n"
        "  lookup_table(table_name)   — exact column list for a single table\n"
        "  get_schema_yaml()          — full YAML schema index for searching all tables/columns\n"
        "  run_query(sql, page, page_size, count_rows)  — execute a read-only paginated query\n\n"
        "Rules:\n"
        "  - Only SELECT or WITH … SELECT queries (no write/DDL).\n"
        "  - Use count_rows=False when you don't need total_pages.\n"
        "  - Always paginate: start page=1, follow next_page until None.\n"
        "  - After reading the guide below: call lookup_table() for target tables; "
        "use get_schema_yaml() only for broad cross-table discovery.\n\n"
        "--- DATABASE SCHEMA (semantic guide) ---\n"
        f"{schema_md}\n"
        "--- END DATABASE SCHEMA (semantic guide) ---\n\n"
        f"User question: {question}\n\n"
        "Write a single SQL SELECT query. Include only the columns needed. "
        "Use table aliases for readability. Add brief comments explaining any joins or filters."
    )


# ---------------------------------------------------------------------------
# Machine-readable tool metadata
# ---------------------------------------------------------------------------

TOOL_METADATA: dict = {
    "get_schema": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "get_schema_yaml": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "lookup_table": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "primary_param": "table_name",
    },
    "run_query": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
        "primary_param": "sql",
    },
}
