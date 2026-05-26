# SQL Gateway

A FastAPI-based SQL Gateway that executes read-only SQL queries against a PostgreSQL database. Exposes both a REST API and an MCP (Model Context Protocol) server so AI agents can query the database and access schema documentation.

## Features

- Read-only SQL execution with pagination
- GCP Secret Manager integration
- API Key authentication (REST and MCP)
- MCP server with `run_query`, `get_schema`, and `get_schema_yaml` tools plus schema resources
- Database schema served via REST (`/schema` for Markdown and `/schema/yaml` for YAML) and MCP resources

## Endpoints

| Method | Path              | Auth        | Description                       |
| ------ | ----------------- | ----------- | --------------------------------- |
| GET    | `/health`         | None        | Health check                      |
| GET    | `/schema`         | None        | Database schema guide as Markdown |
| GET    | `/schema/yaml`    | None        | Search-first schema index as YAML |
| POST   | `/query`          | `X-API-Key` | Execute a read-only SQL query     |
| POST   | `/mcp-server/mcp` | `X-API-Key` | MCP Streamable HTTP endpoint      |

## Setup

1. Install dependencies: `poetry install`
2. Set environment variables: `GCP_PROJECT_ID`, `API_KEY`
3. Run: `uvicorn main:app --reload`

## MCP Usage

### Connect from Cursor

The MCP endpoint requires the same `X-API-Key` header as the REST API. Add to your Cursor MCP config (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "sql-gateway": {
      "url": "http://localhost:8000/mcp-server/mcp",
      "headers": {
        "X-API-Key": "<your-api-key>"
      }
    }
  }
}
```

### Connect from Claude Code

```bash
claude mcp add --transport http sql-gateway http://localhost:8000/mcp-server/mcp --header "X-API-Key: <your-api-key>"
```

### Available MCP capabilities

Schema tools and `run_query` return **`CallToolResult` with `structuredContent` only** (`content` is empty). That avoids the MCP Python server from emitting the same JSON again as a giant `text` content block (which doubled token usage for `get_schema`). When connecting **through mcp-proxy**, redundant `content` text is also stripped if it repeats either the same JSON as `structuredContent` or the raw markdown/yaml string already carried in `structuredContent.content` (see proxy `dedupe_tool_call_jsonrpc`).

- **Tool `run_query`** — Execute a paginated read-only query (`SELECT` or `WITH ... SELECT`). Pass `count_rows=False` to skip the `COUNT(*)` subquery when `total_pages` is not needed; in this mode `total_pages` is `null` and `next_page` is inferred from whether the current page returned `page_size` rows. Returns `{ data, page, page_size, total_pages, next_page, error, details }`.
- **Tool `get_schema`** — Return a markdown schema payload from `resources/models.md`. Shape: `{ format, content, error, details }`.
- **Tool `get_schema_yaml`** — Return the full structured schema from `resources/schema.yaml`. Prefer `lookup_table` for single-table lookups.
- **Tool `lookup_table`** — Return the definition of a single table (summary + columns) from `schema.yaml` without loading the full file. Supports exact and case-insensitive prefix match, rejects empty input, and returns an explicit ambiguity error when a prefix matches multiple tables. Returns `{ table_name, summary, columns, error, details }`.
- **Resource `schema://models`** — Full database schema narrative reference for semantic understanding.
- **Resource `schema://yaml`** — Structured YAML schema for exact table/column lookup.
- **Prompt `sql_assistant`** — Provide a business question and get a prompt with the full semantic guide inlined. It does **not** ask you to call `get_schema` for the same markdown; use `lookup_table` / `get_schema_yaml` for exact columns, then `run_query`.

### Agent guidance

- **Call order (standalone tools).** `get_schema` (or read resource `schema://models`) → `lookup_table(table_name)` (or `get_schema_yaml` for full search) → `run_query`. Never skip loading the semantic guide when you are **not** using `sql_assistant`.
- **`sql_assistant`.** The guide is already in the prompt; skip `get_schema` unless you explicitly need to re-fetch. Then `lookup_table` / `get_schema_yaml` as needed → `run_query`.
- **Targeted lookup.** Use `lookup_table` instead of `get_schema_yaml` when you already know (or suspect) the table name — it loads only the rows you need and saves context budget.
- **Schema guide vs schema index.** Prefer **`get_schema`** (or `sql_assistant` / resource `schema://models`) for **narrative semantics and join safety**; prefer **`get_schema_yaml`** or **`lookup_table`** for **exact columns**—do not scrape the markdown for machine-precise column lists.
- **Read-only enforcement.** `run_query` accepts `SELECT` and `WITH ... SELECT`, rejects write/DDL, and blocks multi-statement SQL. String literal values containing SQL keywords (e.g. `WHERE action = 'DELETE'`) are allowed. Rejections return structured `error` + `details`.
- **Skip COUNT(\*).** Pass `count_rows=False` when you only need the current page and not `total_pages`. This avoids a second subquery and is recommended for exploratory queries while keeping `next_page` usable.
- **Pagination validation.** `page >= 1`, `page_size` 1–500. Failures return the same stable response shape.
- **Schema file reliability.** All schema tools return explicit structured error fields when artifacts are missing so agents can distinguish missing-doc failures from valid schema content.

### Schema artifact locations

- `global/mcp/sql-gateway/resources/models.md`: agent-facing schema super prompt (business semantics, trusted joins, query recipes, pitfalls).
- `global/mcp/sql-gateway/resources/schema.yaml`: machine-searchable schema index for exact columns, references, and table lookup.
- Keep both files synchronized with Django models under `global/portal/src/api/src/apps/`.

### Test with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector
```

Then connect to `http://localhost:8000/mcp-server/mcp` in the inspector UI. Add the `X-API-Key` header in the inspector's connection settings.

## Deployment

### 0. Generate requirements.txt

Cloud Run requires `requirements.txt` to install dependencies.

```bash
poetry export --without-hashes --format=requirements.txt > requirements.txt
```

### 1. Create Service Account & Grant Permissions

```bash
# Create Service Account
gcloud iam service-accounts create sql-gateway-sa \
    --display-name="SQL Gateway Service Account"

# Grant Secret Manager Accessor
gcloud projects add-iam-policy-binding it-team-hw-project \
    --member="serviceAccount:sql-gateway-sa@it-team-hw-project.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"

# Grant Cloud SQL Client
gcloud projects add-iam-policy-binding it-team-hw-project \
    --member="serviceAccount:sql-gateway-sa@it-team-hw-project.iam.gserviceaccount.com" \
    --role="roles/cloudsql.client"
```

### 2. Deploy to Cloud Run

```bash
gcloud run deploy sql-gateway \
  --source . \
  --region europe-west1 \
  --set-env-vars ENV=prod \
  --service-account sql-gateway-sa@it-team-hw-project.iam.gserviceaccount.com \
  --add-cloudsql-instances it-team-hw-project:europe-west1:data-warehouse-postgres-replica \
  --allow-unauthenticated
```
