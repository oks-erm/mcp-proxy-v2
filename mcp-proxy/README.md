# MCP Proxy

A GCP Cloud Run service that acts as a single entry point for multiple MCP (Model Context Protocol) servers. Add upstream MCP server configs via REST API; clients connect once and get access to all tools, resources, and prompts from every configured server.

## Admin web UI

After deploy, open **`/app/`** on the service URL (e.g. `https://<cloud-run-url>/app/`). Sign in with Google; **admin** users get the full console (servers, diagnostics, users, permissions, approvals, audit link to Log Explorer) **and** the same **MCP setup** / **activity logs** pages as everyone else (per-user API key via `GET /me/mcp-credentials`, Cursor JSON on the Account page). **Other approved roles** (`user`, `power_user`) only see **MCP setup** and **activity logs** (Log Explorer scoped to their user id). The UI uses the same JWT + httpOnly refresh cookies and CSRF headers as the REST API.

**Managed apps** lists production web apps whose `deploy_app` runs **through this proxy** (upstream `cloud_run_deployer`, `execute=true`). New apps are created as `pending_review`; approving them calls the deployer to expose the default Cloud Run URL behind Host Wise Google auth, and deleting them performs full cleanup by default. Deploying via a direct URL to the deployer MCP alone does not write that list; redeploy through the proxy to register.

Local UI dev (proxies API to port 8080):

```bash
cd web && npm install && npm run dev
# Vite: http://localhost:5173/ (dev uses site root; production UI stays under /app/ on the API) — run API: ENV=local uvicorn main:app --reload --port 8080
```

## Architecture

- **REST API** (Bearer JWT for `/me` and `/admin`, Google ID token for `/auth/login/google`): Add, list, update, remove MCP server configs (stored in Firestore)
- **MCP Endpoint** (user API key): Single MCP endpoint that aggregates all upstream servers
- **Credentials**: Upstream MCP credentials (e.g. API keys) are stored in GCP Secret Manager, referenced by `credentials_secret_id` in Firestore
- **Namespacing**: Tools, resources, and prompts are prefixed with `{server_id}_` to avoid collisions (tools from servers **`n8n`**, **`zendesk`**, **`stripe`**, **`breezeway`**, **`pipedrive`**, and **`absence`** already use that prefix upstream, so the proxy does not double-prefix: `n8n_get_workflows` not `n8n_n8n_get_workflows`)
- **Proxy-local shorthand aliases**: The proxy can expose a small number of extra ergonomic tool names that forward to canonical upstream tools. Example: `find_ticket_by_reservation_id` forwards to `zendesk_find_ticket_by_reservation_id`.
- **Proxy-local improvement tool**: The proxy also exposes **`mcp_proxy_request_improvement`** in `tools/list`. Agents should call it when a proxied tool repeatedly errors, lacks needed fields, or an upstream server should grow new tools. Each request must name affected `tool_names` and/or `server_ids`; include recent `error_context` when available. Requests are stored in Firestore, rate limited per requester, reviewed in the **Approvals** UI, **approved** requests are kept, and **declined** requests are deleted.
- **Proxy-local skill catalog**: The proxy also exposes **`mcp_proxy_list_skills`**, **`mcp_proxy_install_skill`**, **`mcp_proxy_check_skill_updates`**, and **`mcp_proxy_request_skill_update`** in `tools/list`, backed by the repo catalog at `skills/catalog/`. Skill manifests include a deterministic **`content_sha256`**, a catalog **`version`** (explicit frontmatter version or content-derived fallback), and **`updated_at`** so agents can verify local installs against the latest published bundle. Published skill bundles are available over HTTP at **`GET /skills/catalog`** and **`GET /skills/catalog/{skill_name}.tar.gz`** so Codex can fetch installable tarballs and place them into `~/.codex/skills/<skill-name>`.
- **Initialize bootstrap instructions**: The proxy returns MCP **`initialize.result.instructions`** pointing agents at **`mcp_proxy://help/index`**, **`mcp_proxy://help/capabilities.json`**, per-tool help pages, and the skill catalog/update-check tools so first-run clients can orient themselves without custom out-of-band setup.
- **Tool results**: Upstream MCP servers may return JSON in `structuredContent`, in `content` text, or both depending on server settings. Clients should prefer **`structuredContent`** when present; otherwise parse **`content`** items with MIME `application/json` or parse JSON from `text` fields. On **`tools/call`**, the proxy strips redundant **`content` text** when it either parses to the **same JSON** as `structuredContent`, or when `structuredContent` is a sql-gateway-style schema payload (`format` `markdown` or `yaml` with inline `content`) and a text block repeats that **raw body** without JSON wrapping (so large schema guides are not duplicated).
- **Per-user permissions** (`user` role): Firestore `permissions` rows grant **Read** and/or **Write** per upstream `server_id`. **Admin** and **power_user** bypass checks for all enabled servers. Users may connect to a server if they have **Read or Write**. **Read** tools require Read or Write; **Write**-classified tools (see [`permissions/write_tools.py`](permissions/write_tools.py)) require **Write**. Registered write tools include n8n authoring tools and Cloud Run deployer mutation tools such as `deploy_app`, `approve_app`, and `delete_app`. Managed app secrets use proxy-local tools (`mcp_proxy_set_app_secret`, `mcp_proxy_list_app_secrets`, `mcp_proxy_delete_app_secret`) and are stored encrypted in Firestore. Denied calls return JSON-RPC error `-32003`. Add another `server_id` key in `UPSTREAM_WRITE_TOOLS_BY_SERVER` if you add more upstreams with mutating tools.
- **Managed app secret encryption**: set `MCP_PROXY_APP_SECRET_ENC_KEY` to a Fernet key. Secret values are write-only through MCP/API responses and stored in Firestore collection `managed_app_secrets`.

## Tool response conventions

- **Envelope:** Tool results are a **single JSON object** in **`structuredContent`** (or equivalent parsed from `content`). Meaningful fields live at the **top level**—for example `data`, `documents`, `error`, `details`—not wrapped in an extra universal `{ "result": ... }` bucket. (JSON-RPC still has a protocol-level `result`; that is separate from each tool’s payload shape.)
- **Empty results:** For search/list tools, **`data: []`**, **`count: 0`**, or **`totalCount: 0`** usually means **no matches**, which is **success**, not a failure. Prefer that interpretation unless the payload also includes **`error`** (or the call returned an MCP/JSON-RPC error).
- **Errors:** A **top-level `error`** string (often with **`details`** and/or **`status_code`**) means the tool **failed**. Distinguish that from an empty successful list.
- **Error contract (rolling standardization):** Upstream tools should use **`tool_error`** from shared `mcp_platform.envelope`: every failure payload includes at least **`error`** + **`details`**. Prefer also **`cause`** (`validation` | `not_found` | `permission` | `upstream_error` | `timeout` | `unknown`) and **`retryable`** (bool) so agents can decide whether to retry; optional **`suggested_fix`**. N8n and Firestore-gateway MCP tools emit these fields on validation, access, and upstream failures. Other servers are migrated incrementally to avoid breaking existing parsers that only read `error`/`details`.
- **Agent guidance when errors persist:** The machine-readable help spec at `mcp_proxy://help/capabilities.json` and the human guide at `mcp_proxy://help/index` instruct agents to submit **`mcp_proxy_request_improvement`** after repeated failures or missing capability, instead of silently giving up.
- **Success lists — `data` is the authoritative field.** All list/search tools emit **`data`** as the canonical list; `meta` carries tool name, schema version, and pagination. The following tools are **fully canonical** (only `data` + `meta`, no legacy keys): `breezeway_list_properties_page`, `guesty_get_listings`, `zendesk_search_tickets`, `quickbooks_list_bank_accounts`, `stripe_get_charges`, `firestore_gateway_list_collections`. Tools still in transition (`with_response_meta(..., data_from="…")`) retain a service-native alias (e.g. `results`, `users`, `documents`, `items`) alongside `data` for backward compat; that alias will be removed in a future major version. Agents should always read `data`.
- **Prefer `structuredContent`:** When both `structuredContent` and `content` text exist, use **`structuredContent`** first; the proxy may strip duplicate text blocks (see **Tool results** above).

### Pagination — `meta.pagination` contract

All paged tools populate `meta.pagination` with **normalized field names** via `build_pagination_meta` (`mcp_platform/meta.py`). Read `meta.pagination` for paging state — do not rely on top-level native fields (`skip`, `nextCursor`, `total_count`, etc.) which are retained only for backward compat.

#### Offset-based (absence, breezeway, guesty)

```json
{
  "limit": 50,
  "offset": 0,
  "has_more": true,
  "next_offset": 50,
  "total_count": 300
}
```

Continue: call again with `skip=next_offset`. `total_count` and `has_more` are omitted when the upstream does not return them.

#### Cursor-based (n8n, Stripe)

```json
{ "limit": 100, "cursor": "abc123", "has_more": true, "next_cursor": "def456" }
```

Continue: call again with `cursor=next_cursor` (n8n) or `starting_after=next_cursor` (Stripe). `cursor` is omitted when not supplied on input.

#### Internally-exhausted (zendesk_search_tickets)

These tools page internally and return the full set. Pagination exposes only `{ "limit": N }` — no continuation.

| Field         | When present              | Meaning                                         |
| ------------- | ------------------------- | ----------------------------------------------- |
| `limit`       | always                    | max items per call                              |
| `offset`      | offset-based              | current page start                              |
| `cursor`      | cursor-based              | cursor used for this request                    |
| `has_more`    | when derivable            | `true` means more items exist                   |
| `next_offset` | offset + has_more         | pass as `skip` for next page                    |
| `next_cursor` | cursor + has_more         | pass as `cursor`/`starting_after` for next page |
| `total_count` | when upstream provides it | total matching items                            |

## Endpoints

| Method | Path                                  | Auth               | Description                                     |
| ------ | ------------------------------------- | ------------------ | ----------------------------------------------- |
| GET    | `/health`                             | None               | Health check for Cloud Run                      |
| GET    | `/admin/servers`                      | Bearer JWT (admin) | List all configured MCP servers                 |
| POST   | `/admin/servers`                      | Bearer JWT (admin) | Add MCP servers (list, validates each upstream) |
| GET    | `/admin/servers/{id}`                 | Bearer JWT (admin) | Get single server config                        |
| PATCH  | `/admin/servers/{id}`                 | Bearer JWT (admin) | Update server config                            |
| DELETE | `/admin/servers/{id}`                 | Bearer JWT (admin) | Remove MCP server                               |
| POST   | `/mcp-server/mcp`                     | User API key       | MCP Streamable HTTP endpoint                    |
| GET    | `/skills/catalog`                     | None               | Public JSON catalog of published Codex skills   |
| GET    | `/skills/catalog/{skill_name}.tar.gz` | None               | Download one published skill bundle             |
| GET    | `/docs`                               | None               | Swagger UI for testing the admin API            |
| GET    | `/redoc`                              | None               | ReDoc API documentation                         |

See also `/auth/*`, `/me/*`, `/users/*` (admin) in OpenAPI (`/docs`).

## Prerequisites

- Python 3.11+
- GCP project with Firestore and Secret Manager
- Upstream MCP servers (e.g. sql-gateway, n8n-mcp, firestore-gateway)

## Upstream Cloud Run IAM (`mcp-proxy-sa`)

The proxy’s **Cloud Run** identity is **`mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com`**. For each upstream MCP that does **not** allow unauthenticated access (or if you use **`upstream_auth`: `cloud_run_iam`** so the proxy sends an ID token), that upstream service must grant this account **`roles/run.invoker`**.

The **[MCP Deploy workflow](../../.github/workflows/mcp-deploy.yml)** adds the binding after every deploy for:

| Cloud Run service   | Typical Firestore `id` / tools prefix |
| ------------------- | ------------------------------------- |
| `quickbooks-mcp`    | `quickbooks`                          |
| `stripe-mcp`        | `stripe`                              |
| `guesty-mcp`        | `guesty`                              |
| `zendesk-mcp`       | `zendesk`                             |
| `breezeway-mcp`     | `breezeway`                           |
| `moloni-mcp`        | `moloni`                              |
| `pipedrive-mcp`     | `pipedrive`                           |
| `absence-mcp`       | `absence`                             |
| `n8n-mcp`           | `n8n`                                 |
| `sql-gateway`       | `sql_gateway`                         |
| `firestore-gateway` | `firestore_gateway`                   |

**One-time** (or after a manual deploy), for any missing service:

```bash
REGION=europe-west1
PROXY_SA=mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com
for SVC in quickbooks-mcp stripe-mcp guesty-mcp zendesk-mcp breezeway-mcp moloni-mcp pipedrive-mcp absence-mcp n8n-mcp sql-gateway firestore-gateway; do
  gcloud run services add-iam-policy-binding "$SVC" \
    --region="$REGION" --member="serviceAccount:${PROXY_SA}" --role=roles/run.invoker
done
```

If diagnostics show **403** / “not authenticated” on **`/mcp-server/mcp`**, set that upstream to **`cloud_run_iam`** in the admin UI (and keep app-level keys in credentials, e.g. **`X-API-Key`**) **and** ensure **`run.invoker`** is granted as above.

## Upstream OAuth2 (per-user)

Some remote MCPs expect an end-user **OAuth 2.0** access token (authorization code + **PKCE**). The proxy supports **`upstream_auth`: `oauth2`** on each Firestore server document, with an embedded **`oauth`** object (authorization URL, token URL, client id, optional scopes, optional Secret Manager id for **client secret**, PKCE on/off).

**Requirements:**

- **`MCP_PROXY_URL`** in config (or env) must be the public HTTPS origin of this service (no trailing slash), e.g. `https://mcp-proxy-xxxxx.run.app`. It is used to build the OAuth **redirect URI** below and for post-login redirects.
- Register this **redirect URI** with your identity provider:

  `{MCP_PROXY_URL}/auth/upstream-oauth/callback`

- **`JWT_SECRET_KEY`** is used to sign OAuth **state** (CSRF) and to **encrypt refresh tokens** in Firestore (`user_upstream_oauth` collection; document id `{user_id}__{server_id}`). Treat it as a sensitive key.

**Flow:** Admins configure the server in the **MCP servers** UI. Each user opens **Account** → **Upstream OAuth2**, clicks **Connect** (sends `Authorization: Bearer` via `fetch`), approves at the IdP, and returns to the app. Linked servers participate in MCP **initialize** for that user; unlinked OAuth2 servers are skipped until the user connects.

**Diagnostics:** For `oauth2` upstreams, `GET /admin/servers/{id}/diagnostics` returns a note unless you pass **`?user_id=<firestore user id>`** for a user who has completed the link.

REST routes: `GET /me/upstream-oauth/connections`, `GET /me/upstream-oauth/{server_id}/authorize`, `DELETE /me/upstream-oauth/{server_id}`, `GET /auth/upstream-oauth/callback`.

## Config secret (GCP Secret Manager)

When `ENV` is not `local`, the app loads configuration from a **single JSON secret** in GCP Secret Manager. This keeps secrets out of the environment and allows rotation without redeploying.

### 1. Create the secret

Create a secret named `mcp-proxy-config` (or set `MCP_PROXY_CONFIG_SECRET_ID` to another name). The value must be a JSON object with at least the keys below.

**Generate values you can randomize:**

```bash
# JWT signing secret (random; used to sign/verify REST API tokens)
JWT_SECRET=$(openssl rand -hex 32)
echo "JWT_SECRET_KEY: $JWT_SECRET"
```

**Get Google OAuth Client ID:**

1. Open [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **Credentials**.
2. Create or select an **OAuth 2.0 Client ID** (application type: **Web application**).
3. Copy the **Client ID** (it looks like `xxxxx.apps.googleusercontent.com`). This is `GOOGLE_OAUTH_CLIENT_ID` and is required for Google sign-in.

**Example JSON payload** (replace with your values):

```json
{
  "JWT_SECRET_KEY": "<paste output of openssl rand -hex 32>",
  "GOOGLE_OAUTH_CLIENT_ID": "<your-oauth-client-id>.apps.googleusercontent.com",
  "GCP_PROJECT_ID": "it-team-hw-project",
  "MCP_PROXY_DATABASE": "mcp-proxy-database",
  "MCP_PROXY_URL": "https://mcp-proxy-xxxxx.run.app",
  "JWT_ACCESS_EXPIRE_MINUTES": 60,
  "MCP_PROXY_BOOTSTRAP_ADMIN_EMAILS": "admin@yourcompany.com"
}
```

- **JWT_SECRET_KEY** (required in prod): Random secret for signing JWTs. Generate with `openssl rand -hex 32`.
- **GOOGLE_OAUTH_CLIENT_ID** (required): OAuth 2.0 Client ID from GCP Console; used to verify Google ID tokens.
- **GCP_PROJECT_ID**, **MCP_PROXY_DATABASE**, **MCP_PROXY_URL**, **JWT_ACCESS_EXPIRE_MINUTES**: Optional; can be set via environment variables instead. **`MCP_PROXY_URL` is required** if you use **upstream OAuth2** (callback and redirects).

### 2. Create the secret in Secret Manager

```bash
export GCP_PROJECT_ID=it-team-hw-project

# Build the JSON (replace placeholders with real values)
JWT_SECRET=$(openssl rand -hex 32)
echo "Save this JWT_SECRET_KEY for the secret: $JWT_SECRET"

# Create secret with JSON payload (edit the JSON with your GOOGLE_OAUTH_CLIENT_ID and optional fields)
echo -n "{
  \"JWT_SECRET_KEY\": \"$JWT_SECRET\",
  \"GOOGLE_OAUTH_CLIENT_ID\": \"YOUR_CLIENT_ID.apps.googleusercontent.com\",
  \"GCP_PROJECT_ID\": \"$GCP_PROJECT_ID\",
  \"MCP_PROXY_DATABASE\": \"mcp-proxy-database\",
  \"MCP_PROXY_URL\": \"https://your-mcp-proxy-url.run.app\"
}" | gcloud secrets create mcp-proxy-config \
  --project="$GCP_PROJECT_ID" \
  --replication-policy=automatic \
  --data-file=-

# Or add a new version to an existing secret
echo -n "{\"JWT_SECRET_KEY\": \"$JWT_SECRET\", \"GOOGLE_OAUTH_CLIENT_ID\": \"...\"}" | \
  gcloud secrets versions add mcp-proxy-config --data-file=-
```

Ensure the Cloud Run (or app) service account has `roles/secretmanager.secretAccessor` on this secret.

### 3. Override with environment variables

Any key in the JSON can be overridden by setting the same name as an environment variable (e.g. `GOOGLE_OAUTH_CLIENT_ID`, `JWT_SECRET_KEY`). For local development, set `ENV=local` and use environment variables or defaults (no secret is loaded).

## Local Setup

```bash
cd global/mcp/mcp-proxy
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

All proxy data (MCP server configs, users, permissions, access requests) lives in **`MCP_PROXY_DATABASE`** (default `mcp-proxy-database`), collection **`FIRESTORE_COLLECTION`** (default `mcp_servers`) for server configs. For local dev, set:

```bash
export ENV=local
export API_KEY=local-dev-key
export ADMIN_KEY=local-admin-key
export GCP_PROJECT_ID=it-team-hw-project
export MCP_PROXY_DATABASE=mcp-proxy-database   # optional; matches prod
# export FIRESTORE_COLLECTION=my_servers       # optional override of collection name
```

**First admin (avoid “pending approval”):** the first Google sign-in always created users as `pending` until another admin approves them. Set one or more bootstrap admin emails (comma-separated, case-insensitive). Those addresses are created or upgraded to **`admin` + `active`** on login. On each login, Firestore **server permissions** for that user are refreshed to **read+write on every configured MCP server** (so the Users UI and stored data stay complete even though the proxy grants admins full access without those rows).

```bash
export MCP_PROXY_BOOTSTRAP_ADMIN_EMAILS="you@yourcompany.com,other@yourcompany.com"
```

In production, add the same key to `mcp-proxy-config` JSON or as a Cloud Run env var. Remove or trim this list once you have a normal admin workflow, if you prefer not to keep a permanent backdoor.

Run:

```bash
python main.py
# or: uvicorn main:app --reload --port 8080
# Debug logging: LOG_LEVEL=DEBUG python main.py
# Logs use GCP-structured format (JSON) for Cloud Logging / Log Explorer
```

## Shared skill catalog

Installable Codex skills published by the proxy live in:

```text
global/mcp/mcp-proxy/skills/catalog/<skill-name>/
```

Each skill bundle is generated from that folder as a tar.gz archive whose root directory matches the skill name.
Agents can:

- call `mcp_proxy_list_skills` to browse the catalog
- call `mcp_proxy_install_skill` to get the download URL, checksum, and install commands
- call `mcp_proxy_request_skill_update` when a shared skill should improve for everyone

The intended local install target is:

```text
~/.codex/skills/<skill-name>
```

## Add Upstream Servers

Credentials can be provided in two ways:

1. **Inline header** (`credentials_header`): Same format as mcp.json `--header`, e.g. `"X-API-Key: abc123"` or `"xc-mcp-token: token"`. Convenient for testing.
2. **Secret Manager** (`credentials_secret_id`): Reference a GCP secret containing JSON headers, e.g. `{"X-API-Key": "your-api-key"}`. Recommended for production.

Provide exactly one of `credentials_header` or `credentials_secret_id` per server.

### Option 1: Inline header (quick setup)

```bash
curl -X POST http://localhost:8080/admin/servers \
  -H "X-Admin-Key: local-admin-key" \
  -H "Content-Type: application/json" \
  -d '[{
    "id": "sql-gateway",
    "url": "https://sql.hwit.online/mcp-server/mcp",
    "credentials_header": "X-API-Key: your-upstream-api-key",
    "enabled": true
  }]'
```

Or add multiple at once. Copy the example file and fill in your credentials (the real file is gitignored):

```bash
cp scripts/servers-from-mcp-json.json.example scripts/servers-from-mcp-json.json
# Edit servers-from-mcp-json.json with your real URLs and credentials

curl -X POST http://localhost:8080/admin/servers \
  -H "X-Admin-Key: local-admin-key" \
  -H "Content-Type: application/json" \
  -d @scripts/servers-from-mcp-json.json
```

Response: `{"created": [...], "errors": []}`. Failed items are in `errors` with `index`, `id`, and `detail`.

### Option 2: Secret Manager

### 2a. Create credentials secrets for each upstream

```bash
# sql-gateway credentials
echo -n '{"X-API-Key": "your-sql-gateway-api-key"}' | \
  gcloud secrets create mcp-proxy-sql-gateway-creds \
    --project=it-team-hw-project \
    --replication-policy=automatic \
    --data-file=-

# n8n-mcp credentials
echo -n '{"X-API-Key": "your-n8n-mcp-api-key"}' | \
  gcloud secrets create mcp-proxy-n8n-mcp-creds \
    --project=it-team-hw-project \
    --replication-policy=automatic \
    --data-file=-
```

### 2b. Add servers via admin API

**Option A: Use Swagger UI**

Open http://localhost:8080/docs, click **Authorize**, enter `local-admin-key` (or your admin key), then use the interactive API to add servers.

**Option B: Use curl**

```bash
# Add sql-gateway (references Secret Manager secret)
curl -X POST http://localhost:8080/admin/servers \
  -H "X-Admin-Key: local-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "sql-gateway",
    "url": "https://sql-gateway-xxx.run.app/mcp-server/mcp",
    "credentials_secret_id": "mcp-proxy-sql-gateway-creds",
    "enabled": true
  }'

# Add n8n-mcp
curl -X POST http://localhost:8080/admin/servers \
  -H "X-Admin-Key: local-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "n8n-mcp",
    "url": "https://n8n-mcp-xxx.run.app/mcp-server/mcp",
    "credentials_secret_id": "mcp-proxy-n8n-mcp-creds",
    "enabled": true
  }'
```

The proxy validates each upstream (sends `initialize`) before persisting. Ensure the mcp-proxy service account has `roles/secretmanager.secretAccessor` on these secrets.

## Validate Proxy

Run the validation script to exercise Firestore, sql-gateway, NocoDB, and n8n through the proxy:

```bash
# With proxy running locally (ENV=local, API_KEY=local-dev-key):
X_API_KEY=local-dev-key poetry run python scripts/validate_mcp_proxy.py

# With Cloud Run or custom API key:
MCP_PROXY_URL=https://your-proxy.run.app X_API_KEY=<your-mcp-api-key> poetry run python scripts/validate_mcp_proxy.py
```

The script: initializes, lists Firestore collections and queries documents, gets SQL schema and runs a query, lists NocoDB tables, finds the `mcp-workflow-test` n8n workflow, fetches it, and updates it.

## Cursor Config

Configure Cursor to use the proxy (one connection for all servers):

```json
{
  "mcpServers": {
    "mcp-proxy": {
      "url": "https://mcp-proxy-xxx.run.app/mcp-server/mcp",
      "headers": {
        "X-API-Key": "<mcp-proxy-api-key>"
      }
    }
  }
}
```

Tool names will be prefixed: e.g. `sql_gateway_run_query`, `n8n_get_workflows`, `zendesk_search_tickets`.

## Environment Variables

| Variable                        | Required | Default             | Description                                                                                                                                                                  |
| ------------------------------- | -------- | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GCP_PROJECT_ID`                | No       | it-team-hw-project  | GCP project ID                                                                                                                                                               |
| `MCP_PROXY_DATABASE`            | No       | mcp-proxy-database  | Firestore database for server configs, users, permissions, access requests                                                                                                   |
| `FIRESTORE_COLLECTION`          | No       | mcp_servers         | Collection name for upstream MCP server configs                                                                                                                              |
| `MCP_SERVERS_CACHE_TTL_SECONDS` | No       | `30`                | In-process cache TTL (seconds) for the `mcp_servers` listing; set `0` to disable caching. Invalidated on this instance when servers are created/updated/deleted via the API. |
| `ENV`                           | No       | -                   | `local` for dev, else production                                                                                                                                             |
| `API_KEY`                       | Local    | -                   | Legacy local MCP key (per-user keys are used in production)                                                                                                                  |
| `ADMIN_KEY`                     | Local    | -                   | Legacy local admin key                                                                                                                                                       |
| `API_KEY_SECRET_ID`             | Prod     | mcp-proxy-api-key   | (Legacy) Secret Manager                                                                                                                                                      |
| `ADMIN_KEY_SECRET_ID`           | Prod     | mcp-proxy-admin-key | (Legacy) Secret Manager                                                                                                                                                      |
| `PORT`                          | No       | 8080                | HTTP port                                                                                                                                                                    |

## Migrating `mcp_servers` from another database

If configs used to live in **`data-warehouse-firestore`** (or any other DB), copy the **`mcp_servers`** documents into **`MCP_PROXY_DATABASE`** (same collection name unless you set `FIRESTORE_COLLECTION`). Easiest for a small set: **Firestore console** → select source database → `mcp_servers` → re-create or export each document in the target database. For many documents, use a one-off script with the Firestore API (read from old `database_id`, write to `mcp-proxy-database`). After migration, remove `GOOGLE_CLOUD_DATABASE` from Cloud Run if you had set it only for this service; the app no longer reads it for server configs.

## Deployment to Cloud Run

### 1. Create Secrets

```bash
# MCP client API key
API_KEY=$(openssl rand -hex 32)
echo "MCP API Key: $API_KEY"
gcloud secrets create mcp-proxy-api-key --project=it-team-hw-project --replication-policy=automatic
echo -n "$API_KEY" | gcloud secrets versions add mcp-proxy-api-key --data-file=-

# Admin API key
ADMIN_KEY=$(openssl rand -hex 32)
echo "Admin Key: $ADMIN_KEY"
gcloud secrets create mcp-proxy-admin-key --project=it-team-hw-project --replication-policy=automatic
echo -n "$ADMIN_KEY" | gcloud secrets versions add mcp-proxy-admin-key --data-file=-
```

### 2. Create Service Account

```bash
gcloud iam service-accounts create mcp-proxy-sa --display-name="MCP Proxy SA"

# Secret Manager: for mcp-proxy API keys and upstream MCP credentials
gcloud projects add-iam-policy-binding it-team-hw-project \
  --member="serviceAccount:mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Firestore: project-level (covers all databases) — simplest
gcloud projects add-iam-policy-binding it-team-hw-project \
  --member="serviceAccount:mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com" \
  --role="roles/datastore.user"

# Or scope only to mcp-proxy-database (server configs + users + permissions + access_requests):
gcloud projects add-iam-policy-binding it-team-hw-project \
  --member='serviceAccount:mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com' \
  --role='roles/datastore.user' \
  --condition='expression=resource.name=="projects/it-team-hw-project/databases/mcp-proxy-database",title=MCP_Proxy_Firestore,description=Grant mcp-proxy access to mcp-proxy-database'
```

### 3. Deploy

```bash
gcloud run deploy mcp-proxy \
  --source . \
  --region europe-west1 \
  --set-env-vars ENV=production,GCP_PROJECT_ID=it-team-hw-project,MCP_PROXY_DATABASE=mcp-proxy-database \
  --set-secrets API_KEY=mcp-proxy-api-key:latest,ADMIN_KEY=mcp-proxy-admin-key:latest \
  --service-account mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com \
  --allow-unauthenticated
```

**Important:** The service account must have `roles/secretmanager.secretAccessor` for `--set-secrets` to work (Cloud Run fetches secrets at container start). Run the IAM bindings in step 2 before deploying.

### 4. Alternative: Load from Secret Manager in Code

The app loads `mcp-proxy-api-key` and `mcp-proxy-admin-key` from Secret Manager when `ENV != local`. Ensure the service account has `roles/secretmanager.secretAccessor` and set:

```bash
--set-env-vars ENV=production,GCP_PROJECT_ID=it-team-hw-project,MCP_PROXY_DATABASE=mcp-proxy-database
```

The app will fetch both keys at startup from Secret Manager.
