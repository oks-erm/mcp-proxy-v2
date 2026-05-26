# Pipedrive MCP

Read-only MCP server for [Pipedrive](https://developers.pipedrive.com/docs/api/v1): **list**, **`pipedrive_get_*`** (single item by id), and **`pipedrive_search_*`** (v2 search or client-side scan where the API has no `/search`). Authenticates with `x-api-token`. Default v1 base is `https://api.pipedrive.com/v1` (v2 URLs use `{root}/api/v2/...` from that base).

## HTTP / auth

- **MCP transport**: Streamable HTTP mounted at **`/mcp-server`** (client path is typically **`/mcp-server/mcp`**).
- **Ingress**: **`X-API-Key`** matching Secret Manager secret **`pipedrive-mcp-api-key`** (override with `API_KEY_SECRET_ID`), **or** verified **`Authorization: Bearer`** Google ID token for this host (audience `https://<Host>/`) with `email` in `ALLOWED_INVOKER_SA_EMAILS` (default `mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com`).

Create **`pipedrive-mcp-sa@it-team-hw-project.iam.gserviceaccount.com`** with **Secret Manager Secret Accessor** on **`pipedrive-mcp-api-key`** and **`pipedrive-api-token`** before first deploy.

### Create secrets (once per project)

````bash
PROJECT=it-team-hw-project

# MCP ingress key (for proxy / direct clients)
gcloud secrets describe pipedrive-mcp-api-key --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud secrets create pipedrive-mcp-api-key --project="$PROJECT" --replication-policy=automatic
read -s MCP_KEY && echo -n "$MCP_KEY" | gcloud secrets versions add pipedrive-mcp-api-key --project="$PROJECT" --data-file=-

# Pipedrive personal API token (Settings → Personal preferences → API)
gcloud secrets describe pipedrive-api-token --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud secrets create pipedrive-api-token --project="$PROJECT" --replication-policy=automatic
read -s PD_TOKEN && echo -n "$PD_TOKEN" | gcloud secrets versions add pipedrive-api-token --project="$PROJECT" --data-file=-

gcloud secrets add-iam-policy-binding pipedrive-mcp-api-key --project="$PROJECT" \
  --member="serviceAccount:pipedrive-mcp-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding pipedrive-api-token --project="$PROJECT" \
  --member="serviceAccount:pipedrive-mcp-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

## Tools

| Tool | Purpose |
| ---- | ------- |
| `pipedrive_list_activities` | Paginated activities — v1 (`user_id`, `deal_id`, `done`) |
| `pipedrive_get_activity` | One activity — v2 [`GET /activities/{id}`](https://developers.pipedrive.com/docs/api/v1/Activities); optional `include_fields` |
| `pipedrive_search_activities` | Text match on subject / note / description — **client-side** scan of [`GET /api/v2/activities`](https://developers.pipedrive.com/docs/api/v1/Activities) (optional `user_id`→owner, `deal_id`, `done`); see `additional_data.client_side_filter` |
| `pipedrive_list_deals` | Paginated deals — v1 (`status` optional) |
| `pipedrive_get_deal` | One deal — v2 [`GET /deals/{id}`](https://developers.pipedrive.com/docs/api/v1/Deals); optional `include_fields` |
| `pipedrive_search_deals` | Full-text deal search — v2 [`GET /deals/search`](https://developers.pipedrive.com/docs/api/v1/Deals): `term`, `fields`, `exact_match`, `person_id`, `organization_id`, `status`, `include_fields`, `limit`, `cursor` |
| `pipedrive_find_deal_by_title_or_exact_name` | Resolver: ranked exact-then-partial title match without interpreting raw search payloads. **Primary arg is `query` — not `title` or `name`.** `query, person_id, organization_id, status, limit, cursor, detail_level` → `{"count", "data", "detail_level": "compact"}` |
| `pipedrive_list_leads` | Paginated leads — v1 |
| `pipedrive_get_lead` | One lead — v1 [`GET /leads/{id}`](https://developers.pipedrive.com/docs/api/v1/Leads) (`lead_id` UUID) |
| `pipedrive_search_leads` | Full-text lead search — v2 [`GET /leads/search`](https://developers.pipedrive.com/docs/api/v1/Leads): `term`, `fields`, `exact_match`, `person_id`, `organization_id`, `include_fields`, `limit`, `cursor` |
| `pipedrive_list_persons` | Paginated persons — v1 |
| `pipedrive_get_person` | One person — v2 [`GET /persons/{id}`](https://developers.pipedrive.com/docs/api/v1/Persons); optional `include_fields` |
| `pipedrive_search_persons` | Full-text person search — v2 [`GET /persons/search`](https://developers.pipedrive.com/docs/api/v1/Persons): `term`, `fields`, `exact_match`, `organization_id`, `include_fields`, `limit`, `cursor` |
| `pipedrive_list_pipelines` | Paginated pipelines — v1 |
| `pipedrive_get_pipeline` | One pipeline — v2 [`GET /pipelines/{id}`](https://developers.pipedrive.com/docs/api/v1/Pipelines) |
| `pipedrive_search_pipelines` | Name substring — **client-side** scan of [`GET /api/v2/pipelines`](https://developers.pipedrive.com/docs/api/v1/Pipelines) (no official search API) |
| `pipedrive_list_stages` | Paginated stages — v1 (`pipeline_id` optional) |
| `pipedrive_get_stage` | One stage — v2 [`GET /stages/{id}`](https://developers.pipedrive.com/docs/api/v1/Stages) |
| `pipedrive_search_stages` | Name substring — **client-side** scan of [`GET /api/v2/stages`](https://developers.pipedrive.com/docs/api/v1/Stages) (`pipeline_id` optional) |

## HTTP / auth

- **MCP URL**: mount **`/mcp-server`** — clients use **`/mcp-server/mcp`** (streamable HTTP).
- **Ingress** (either):
  - **`X-API-Key`**: must match the value in Secret Manager **`pipedrive-mcp-api-key`** (`API_KEY_SECRET_ID`), or
  - **`Authorization: Bearer <Google ID token>`**: verified OIDC from Cloud Run / IAM; caller `email` must be listed in `ALLOWED_INVOKER_SA_EMAILS` (default includes `mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com`).

## One‑time GCP bootstrap

Run from a machine with `gcloud` authenticated to the target project. Adjust **`PROJECT`** and **`REGION`** if needed.

```bash
PROJECT=it-team-hw-project
REGION=europe-west1
SA_ID=pipedrive-mcp-sa
SA_EMAIL="${SA_ID}@${PROJECT}.iam.gserviceaccount.com"
PROXY_SA="mcp-proxy-sa@${PROJECT}.iam.gserviceaccount.com"
````

### 1. Service account (Cloud Run runtime identity)

```bash
gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_ID" \
    --project="$PROJECT" \
    --display-name="Pipedrive MCP (Cloud Run)" \
    --description="Runs pipedrive-mcp; needs Secret Manager access for API tokens"
```

No project-level roles are required beyond Secret Manager access on the two secrets below (principle of least privilege).

### 2. Secrets

**MCP ingress key** (for direct clients or mcp-proxy `X-API-Key`):

```bash
gcloud secrets describe pipedrive-mcp-api-key --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud secrets create pipedrive-mcp-api-key \
    --project="$PROJECT" \
    --replication-policy="automatic"

MCP_KEY="$(openssl rand -hex 32)"
echo -n "$MCP_KEY" | gcloud secrets versions add pipedrive-mcp-api-key \
  --project="$PROJECT" \
  --data-file=-
# Store MCP_KEY somewhere safe (password manager); mcp-proxy Firestore/Secret config needs it.
unset MCP_KEY
```

**Pipedrive API token** (from Pipedrive: **Settings → Personal preferences → API**):

```bash
gcloud secrets describe pipedrive-api-token --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud secrets create pipedrive-api-token \
    --project="$PROJECT" \
    --replication-policy="automatic"

# Paste token at prompt (input hidden); or: printf '%s' 'YOUR_TOKEN' | gcloud secrets versions add pipedrive-api-token --project="$PROJECT" --data-file=-
read -r -s PD_TOKEN && printf '%s' "$PD_TOKEN" | gcloud secrets versions add pipedrive-api-token \
  --project="$PROJECT" \
  --data-file=-
unset PD_TOKEN
```

### 3. Grant the runtime SA access to both secrets

```bash
for SECRET in pipedrive-mcp-api-key pipedrive-api-token; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --project="$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor"
done
```

### 4. Deploy Cloud Run

From the **repository root** (same shape as [`mcp-deploy.yml`](../../../.github/workflows/mcp-deploy.yml)):

```bash
gcloud run deploy pipedrive-mcp \
  --project="$PROJECT" \
  --region="$REGION" \
  --source=global/mcp/pipedrive-mcp \
  --service-account="$SA_EMAIL" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT},API_KEY_SECRET_ID=pipedrive-mcp-api-key,PIPEDRIVE_TOKEN_SECRET_ID=pipedrive-api-token" \
  --no-allow-unauthenticated
```

Capture the service URL:

```bash
SERVICE_URL="$(gcloud run services describe pipedrive-mcp --project="$PROJECT" --region="$REGION" --format='value(status.url)')"
echo "$SERVICE_URL"
```

### 5. Let mcp-proxy invoke the service (IAM)

If the proxy calls this URL with a Google ID token (recommended), grant **`roles/run.invoker`** to the proxy SA:

```bash
gcloud run services add-iam-policy-binding pipedrive-mcp \
  --project="$PROJECT" \
  --region="$REGION" \
  --member="serviceAccount:${PROXY_SA}" \
  --role="roles/run.invoker"
```

Direct clients using only **`X-API-Key`** still need a network path to the URL; with **`--no-allow-unauthenticated`**, unauthenticated browser calls get **403** unless they send a valid invoker identity or the key path your app implements (this app accepts OIDC **or** matching `X-API-Key`).

### 6. Smoke test

```bash
curl -fsS "${SERVICE_URL}/health"
# Expect: {"status":"ok"}
```

MCP (requires the ingress key):

```bash
curl -fsS -o /dev/null -w "%{http_code}\n" \
  -H "X-API-Key: $(gcloud secrets versions access latest --secret=pipedrive-mcp-api-key --project=$PROJECT)" \
  "${SERVICE_URL}/mcp-server/mcp"
# Expect 200 or 405/406 depending on method; 401 means key mismatch.
```

## Environment variables

| Variable                    | When        | Default                        | Description                                                       |
| --------------------------- | ----------- | ------------------------------ | ----------------------------------------------------------------- |
| `GCP_PROJECT_ID`            | Always      | `it-team-hw-project`           | GCP project for Secret Manager                                    |
| `ENV`                       | Local dev   | empty                          | Set to `local` to use env vars instead of GCP secrets for MCP key |
| `API_KEY`                   | `ENV=local` | —                              | MCP ingress `X-API-Key` value                                     |
| `PIPEDRIVE_API_TOKEN`       | `ENV=local` | —                              | Pipedrive personal API token                                      |
| `API_KEY_SECRET_ID`         | Not local   | `pipedrive-mcp-api-key`        | Secret ID for MCP ingress key                                     |
| `PIPEDRIVE_TOKEN_SECRET_ID` | Not local   | `pipedrive-api-token`          | Secret ID for Pipedrive API token                                 |
| `PIPEDRIVE_API_BASE_URL`    | Optional    | `https://api.pipedrive.com/v1` | Override API base (no trailing slash)                             |
| `PORT`                      | Runtime     | `8080`                         | HTTP port                                                         |
| `ALLOWED_INVOKER_SA_EMAILS` | Cloud Run   | `mcp-proxy-sa@…`               | Comma-separated SA emails allowed via OIDC                        |
| Variable                    | When        | Default                        | Description                                                       |
| --------------------------- | ----------- | ------------------------------ | --------------------------------------------------                |
| `GCP_PROJECT_ID`            | Always      | `it-team-hw-project`           | GCP project for Secret Manager                                    |
| `ENV`                       | Local       | empty                          | Set to `local` to skip GCP secrets for the MCP key                |
| `API_KEY`                   | `ENV=local` | —                              | MCP ingress `X-API-Key` value                                     |
| `PIPEDRIVE_API_TOKEN`       | `ENV=local` | —                              | Pipedrive personal API token                                      |
| `API_KEY_SECRET_ID`         | Cloud Run   | `pipedrive-mcp-api-key`        | Secret id for MCP ingress key                                     |
| `PIPEDRIVE_TOKEN_SECRET_ID` | Cloud Run   | `pipedrive-api-token`          | Secret id for Pipedrive token                                     |
| `PIPEDRIVE_API_BASE_URL`    | Optional    | `https://api.pipedrive.com/v1` | API base (no trailing slash)                                      |
| `PORT`                      | Runtime     | `8080`                         | HTTP port                                                         |
| `ALLOWED_INVOKER_SA_EMAILS` | Cloud Run   | `mcp-proxy-sa@…`               | Comma-separated SA emails allowed via OIDC                        |

## Local run

```bash
cd global/mcp/pipedrive-mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ENV=local
export API_KEY='your-local-mcp-key'
export PIPEDRIVE_API_TOKEN='your-pipedrive-token'
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Firestore / mcp-proxy

Add an **`mcp_servers`** document with id **`pipedrive`**, enabled, pointing at the Cloud Run URL (e.g. `https://pipedrive-mcp-….run.app/mcp-server/mcp`) and configure upstream credentials per your proxy model.
python3 -m venv .venv

# macOS/Linux:

source .venv/bin/activate
pip install -r requirements.txt

export ENV=local
export API_KEY='local-dev-key'
export PIPEDRIVE_API_TOKEN='your-pipedrive-personal-token'

export PORT="${PORT:-8080}"
uvicorn main:app --host 0.0.0.0 --port "$PORT"

```

- Health: `http://localhost:${PORT}/health`
- MCP: `http://localhost:${PORT}/mcp-server/mcp` with header `X-API-Key: local-dev-key`

## Register with mcp-proxy

Add / update Firestore **`mcp_servers`** with id **`pipedrive`**, **enabled**, URL **`${SERVICE_URL}/mcp-server/mcp`**, and upstream credentials your proxy expects (for example a Secret Manager JSON body with `X-API-Key` matching `pipedrive-mcp-api-key`).

## Layout

```

pipedrive-mcp/
main.py # FastAPI + auth + lifespan
mcp*server.py # FastMCP tools (pipedrive*\*)
pipedrive_client.py # GET + x-api-token
requirements.txt
pyproject.toml
README.md

```

```
