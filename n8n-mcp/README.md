# MCP Server for n8n

A Python MCP (Model Context Protocol) server that wraps the [n8n REST API](https://docs.n8n.io/api/api-reference/) and exposes it as MCP tools. Built with the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), deployed to **Google Cloud Run**.

## Features

| Tool                                  | Description                                                                                                                                                                                                |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `n8n_get_workflows`                   | List workflows (summary by default; full payload optional)                                                                                                                                                 |
| `n8n_get_workflow`                    | Get a single workflow by ID (full or `summary_only`)                                                                                                                                                       |
| `n8n_create_workflow`                 | Create a workflow (auto-tagged `mcp`)                                                                                                                                                                      |
| `n8n_update_workflow`                 | Update a workflow, optionally activate/deactivate, and preserve tags                                                                                                                                       |
| `n8n_get_credentials`                 | List credentials with compact summaries by default (`summary_only` for full payload)                                                                                                                       |
| `n8n_get_credential_schema`           | Get the schema for a credential type                                                                                                                                                                       |
| `n8n_create_credential`               | Create a credential (use the schema to build the data payload)                                                                                                                                             |
| `n8n_get_executions`                  | List executions (filter by workflow/status; optional `summary_only`)                                                                                                                                       |
| `n8n_stop_execution`                  | Stop an active execution                                                                                                                                                                                   |
| `n8n_retry_execution`                 | Retry a failed execution                                                                                                                                                                                   |
| `n8n_list_nodes`                      | List node types from source in structured format                                                                                                                                                           |
| `n8n_list_node_files`                 | Browse files inside a node folder                                                                                                                                                                          |
| `n8n_get_node_source`                 | Fetch raw source code of a node file                                                                                                                                                                       |
| `n8n_search_nodes`                    | Search node files by name/content                                                                                                                                                                          |
| `n8n_find_node_by_name_or_capability` | Rank node package folders from GitHub; optional code-search fallback                                                                                                                                       |
| `n8n_validate_workflow_definition`    | Dry-run structural validation (nodes, connections, settings) before create/update                                                                                                                          |
| `n8n_find_workflow_by_name`           | Resolver: find workflows by human name. **Primary arg is `query` — not `name` or `workflow_name`.** `query, limit, active, limit_pages, case_insensitive` → `{"count", "data", "detail_level": "compact"}` |

## Resources

| URI                                                | Description                                                                                        |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `n8n://templates/workflow-starter-minimal`         | Minimal JSON (`nodes` + `connections`) suitable as a starting point for `n8n_create_workflow`      |
| `n8n://prompts/n8n-minimal-workflow-editor`        | Full text of prompt `n8n_minimal_workflow_editor` (with a visible placeholder for argument `task`) |
| `n8n://prompts/n8n-minimal-workflow-editor/static` | Rules-only section of that prompt (everything before `---` / Current task)                         |

## Model Usage Guardrails

Use this checklist whenever an AI agent creates/updates workflows:

1. **Proxy help:** open `mcp_proxy://help/by-task/n8n-create-workflow` (canonical authoring journey).
2. **Credential reuse first:** call `n8n_get_credentials(fetch_all=true)` before any credential creation.
3. **No duplicate credentials:** if `(name, type)` already exists, reuse it instead of creating a new one.
4. **Schema before create:** call `n8n_get_credential_schema` before `n8n_create_credential`.
5. **Dry-run:** call `n8n_validate_workflow_definition` when the graph is non-trivial; use resource `n8n://templates/workflow-starter-minimal` for a skeleton.
6. **Safe updates:** call `n8n_get_workflow` first, then send only modified fields to `n8n_update_workflow` (omit `connections` / `settings` unless replacing them entirely).
7. **Activation is explicit:** only pass `active` when a task explicitly asks to activate/deactivate.
8. **Parameter validation:** use `n8n_list_nodes` → `n8n_list_node_files` → `n8n_get_node_source`, or `n8n_find_node_by_name_or_capability`, to confirm node parameter shape.
9. **Required MCP tag:** every workflow created or updated through this server must have the `mcp` tag; writes only succeed after tag enforcement is confirmed.
10. **Schedule minimum:** for `n8n-nodes-base.scheduleTrigger`, minute-based schedules are forbidden and the minimum allowed recurring interval is every 2 hours.
11. **After writes:** read `mcp_next_steps` on `n8n_create_workflow` / `n8n_update_workflow` responses for suggested follow-up calls.

## Prerequisites

- Python 3.11+
- A self-hosted n8n instance with API access enabled
- A GCP project with Secret Manager API enabled
- `gcloud` CLI installed and authenticated

## Environment Variables

| Variable                      | Required | Default                | Description                                                                                                                                                                                                              |
| ----------------------------- | -------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `N8N_BASE_URL`                | Yes\*    | —                      | n8n API base URL (e.g. `https://n8n.example.com/api/v1`). \*Or use secret.                                                                                                                                               |
| `N8N_BASE_URL_SECRET_ID`      | No       | `n8n-base-url`         | If `N8N_BASE_URL` is not set, load URL from this Secret Manager secret.                                                                                                                                                  |
| `GCP_PROJECT_ID`              | No       | `it-team-hw-project`   | GCP project ID                                                                                                                                                                                                           |
| `API_KEY_SECRET_ID`           | No       | `n8n-mcp-api-key`      | Secret Manager secret ID for the MCP API key (always used)                                                                                                                                                               |
| `N8N_API_KEY_SECRET_ID`       | No       | `n8n-api-key`          | Secret Manager secret ID for the n8n API key                                                                                                                                                                             |
| `N8N_ADMIN_API_KEY_SECRET_ID` | No       | `n8n-admin-api-key`    | Secret Manager secret ID for the n8n **admin** API key (used to list/create credentials and, if the main key lacks scope, to activate/deactivate workflows); you can also set `N8N_ADMIN_API_KEY` locally as a fallback. |
| `GITHUB_TOKEN_SECRET_ID`      | No       | `n8n-mcp-github-token` | Secret Manager secret ID for the GitHub API token (node browse/search). Fallback: `GITHUB_TOKEN` env.                                                                                                                    |
| `GITHUB_TOKEN`                | No       | —                      | Fallback GitHub token if secret is not set (e.g. local dev)                                                                                                                                                              |
| `PORT`                        | No       | `8080`                 | HTTP port                                                                                                                                                                                                                |

## Running Locally

```bash
# 1. Create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure non-secret settings (e.g. via env vars or .env)
# Required: N8N_BASE_URL (and optionally GCP_PROJECT_ID)

# 3. Ensure the following secrets exist in GCP Secret Manager
#    - n8n-mcp-api-key   (MCP API key used by clients)
#    - n8n-api-key       (n8n API key used by this server)
# The server will ALWAYS load both keys from Secret Manager.

# 4. Start the server
python main.py
# Server starts on http://localhost:8080
# MCP endpoint: http://localhost:8080/mcp-server/mcp
# Health check:  http://localhost:8080/health

# Optional: enable hot reload for local development
UVICORN_RELOAD=true python main.py
```

### Testing with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector
# Connect to http://localhost:8080/mcp-server/mcp
# Add header: X-API-Key: your-api-key
```

## Deploying to Cloud Run

### 1. Create the secrets

Store both the MCP API key and the n8n API key in Secret Manager:

```bash
# --- MCP API Key (used by clients to authenticate to this server) ---
MCP_API_KEY=$(openssl rand -hex 32)
echo "Your MCP API Key: $MCP_API_KEY"

gcloud secrets create n8n-mcp-api-key \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "$MCP_API_KEY" | gcloud secrets versions add n8n-mcp-api-key \
  --project=it-team-hw-project \
  --data-file=-

# --- n8n API Key (used by this server to call n8n) ---
gcloud secrets create n8n-api-key \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "your-n8n-api-key" | gcloud secrets versions add n8n-api-key \
  --project=it-team-hw-project \
  --data-file=-

# --- n8n Admin API Key (credentials API requires this) ---
gcloud secrets create n8n-admin-api-key \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "your-n8n-admin-api-key" | gcloud secrets versions add n8n-admin-api-key \
  --project=it-team-hw-project \
  --data-file=-

# --- N8N_BASE_URL (optional: use if you don't set N8N_BASE_URL via --set-env-vars at deploy) ---
gcloud secrets create n8n-base-url \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "https://your-n8n.example.com/api/v1" | gcloud secrets versions add n8n-base-url \
  --project=it-team-hw-project \
  --data-file=-

# --- GitHub token (optional; for n8n_search_nodes and higher rate limits) ---
gcloud secrets create n8n-mcp-github-token \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "ghp_your_github_pat" | gcloud secrets versions add n8n-mcp-github-token \
  --project=it-team-hw-project \
  --data-file=-
```

### 2. Grant Secret Manager access to the Cloud Run service account

```bash
SA="$(gcloud projects describe it-team-hw-project --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

for SECRET in n8n-mcp-api-key n8n-api-key n8n-admin-api-key n8n-base-url n8n-mcp-github-token; do
  gcloud secrets add-iam-policy-binding $SECRET \
    --project=it-team-hw-project \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

### 3. Deploy from source

```bash
gcloud run deploy n8n-mcp \
  --source . \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --set-env-vars="N8N_BASE_URL=https://n8n.srv860220.hstgr.cloud/api/v1,GCP_PROJECT_ID=it-team-hw-project" \
  --no-allow-unauthenticated
```

> **Note:** MCP and n8n keys load from Secret Manager at startup. With **`--no-allow-unauthenticated`**, Cloud Run requires a Google ID token **or** an identity with **`roles/run.invoker`**; the app still requires **`X-API-Key`** on `/mcp-server/*` after the request reaches the container.

Grant **`mcp-proxy-sa`** **`roles/run.invoker`** if you use **[mcp-proxy](../mcp-proxy/README.md)** (see **Via mcp-proxy** below).

### 4. Verify the deployment

```bash
SERVICE_URL=$(gcloud run services describe n8n-mcp --region=europe-west1 --project=it-team-hw-project --format='value(status.url)')

ID_TOKEN="$(gcloud auth print-identity-token --audiences="${SERVICE_URL}")"
curl -fsS -H "Authorization: Bearer ${ID_TOKEN}" "${SERVICE_URL}/health"

# MCP call: Cloud Run Bearer + app X-API-Key
curl -H "Authorization: Bearer ${ID_TOKEN}" -H "X-API-Key: $MCP_API_KEY" -X POST "$SERVICE_URL/mcp-server/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"n8n_get_workflows","arguments":{}},"id":1}'
```

### Via mcp-proxy

If Cloud Run returns **403** for diagnostics, the proxy is only sending **`X-API-Key`**. Set the upstream to **`upstream_auth`: `cloud_run_iam`** and keep **`X-API-Key`** in `credentials_header` or Secret Manager.

1. **`mcp-proxy-sa`** invoker (CI does this after deploy):

   ```bash
   gcloud run services add-iam-policy-binding n8n-mcp \
     --region=europe-west1 \
     --member=serviceAccount:mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com \
     --role=roles/run.invoker
   ```

2. Firestore / admin UI: **`upstream_auth`**: **`cloud_run_iam`**, **`credentials_header`**: **`X-API-Key: …`** (same value as Secret Manager **`n8n-mcp-api-key`**) or `credentials_secret_id` with JSON headers.

## MCP Client Configuration

### Claude Desktop / Cursor

Prefer **mcp-proxy** for private Cloud Run (proxy adds OIDC; you only use the proxy API key there). For a **direct** URL on **`--no-allow-unauthenticated`**, the client must also send a valid **`Authorization: Bearer`** Google ID token for the service URL (not shown below); simplest path is mcp-proxy with **`cloud_run_iam`** on the n8n upstream.

```json
{
  "mcpServers": {
    "n8n-gateway": {
      "url": "https://n8n-mcp-XXXXX.run.app/mcp-server/mcp",
      "headers": {
        "X-API-Key": "your-api-key"
      }
    }
  }
}
```

## Troubleshooting

### "N8N_BASE_URL and N8N_API_KEY must be set"

This means the server started but one or both of these are missing when a tool runs.

- **Cloud Run (CI deploy)**

  - Set the GitHub repo secret **`N8N_BASE_URL`** (e.g. `https://n8n.example.com/api/v1`). The workflow passes it into the service; if the secret is missing or empty, the deployed service will have no base URL.
  - Ensure the secret **`n8n-api-key`** exists in GCP Secret Manager and the Cloud Run service account has `secretmanager.secretAccessor` on it.
  - Ensure the secret **`n8n-admin-api-key`** exists in Secret Manager (and grant the service account `secretmanager.secretAccessor`) or set `N8N_ADMIN_API_KEY` via env vars so credential tools can run.
  - Redeploy (e.g. re-run the `deploy-n8n` job or push a change that triggers it).

- **Local**
  - Set **`N8N_BASE_URL`** in your environment or `.env`.
  - Either have GCP credentials so the app can load the n8n API key from Secret Manager, or set **`N8N_API_KEY`** in your environment (used as fallback when Secret Manager is unavailable).
  - For credential APIs, either load **`n8n-admin-api-key`** from Secret Manager or set **`N8N_ADMIN_API_KEY`** locally (tools that list/create credentials require this).

### "User is missing a scope required to perform this action" when activating/deactivating

n8n only allows workflow activation/deactivation with an API key (or user) that has the right scope (typically **owner**). If the main `N8N_API_KEY` was created by a non-owner or has limited scopes, activate/deactivate will fail. Fixes:

- **Use the admin key for activate/deactivate:** Ensure `N8N_ADMIN_API_KEY` (or secret `n8n-admin-api-key`) is set to an n8n API key that has **owner** (or equivalent) permissions. The MCP will retry activate/deactivate with the admin key when the main key returns a scope error.
- **In the n8n UI:** Have an owner open the workflow and turn the Active toggle off, or create a new API key as an owner and use it as `N8N_API_KEY` or `N8N_ADMIN_API_KEY`.

### `n8n_search_nodes` returns GitHub 401/403

GitHub code search can reject unauthenticated requests or rate-limit them.

- **Cloud Run / Secret Manager:** Create secret **`n8n-mcp-github-token`** (or set `GITHUB_TOKEN_SECRET_ID`) and grant the Cloud Run service account `secretmanager.secretAccessor` on it. The server loads the token at startup.
- **Local:** Set **`GITHUB_TOKEN`** in your environment, or create the same secret in Secret Manager and ensure the app can access it.
- Restart the MCP server after changing the token. `n8n_list_nodes` and `n8n_get_node_source` can still work without a token in many cases; `n8n_search_nodes` typically requires one.

## Project Structure

```
n8n-mcp/
├── main.py             # FastAPI app + MCP mount + auth middleware (sql-gateway pattern)
├── mcp_server.py       # FastMCP server definition + tools
├── n8n_client.py       # n8n API client helpers
├── pyproject.toml      # Python dependencies (Poetry)
└── README.md
```
