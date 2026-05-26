# absence.io MCP

FastMCP + FastAPI. Exposes **read-only** absence.io **API v2** operations used by the portal: **users** and **absences**, authenticated with **Hawk** (`requests-hawk`, `always_hash_content=False`), same pattern as `global/portal/.../absence_tasks.py`.

## HTTP / auth

- **MCP transport**: Streamable HTTP mounted at **`/mcp-server`** (client path is **`/mcp-server/mcp`**).
- **Ingress**: **`X-API-Key`** matching Secret Manager **`absence-mcp-api-key`** (override with `API_KEY_SECRET_ID`), **or** verified **`Authorization: Bearer`** Google ID token for this host (audience `https://<Host>/`) with `email` in `ALLOWED_INVOKER_SA_EMAILS` (default `mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com`).

Create **`absence-mcp-sa@it-team-hw-project.iam.gserviceaccount.com`** with **Secret Manager Secret Accessor** on **`absence-mcp-api-key`**, **`absence-api-key-id`**, and **`absence-api-key`** before first deploy.

### Create secrets (once per project)

Hawk **id** and **key** come from absence.io (account / API settings — treat like passwords; do not commit). Grant the runtime SA **Secret Accessor** after **§1. Service account** (see **§3** below). Prompts use `printf` + `read` so the block works in **zsh** as well as bash (zsh has no `read -p`, which caused an empty payload and `Secret Payload cannot be empty`).

```bash
PROJECT=it-team-hw-project

# MCP ingress key (for mcp-proxy / direct clients)
gcloud secrets describe absence-mcp-api-key --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud secrets create absence-mcp-api-key --project="$PROJECT" --replication-policy=automatic
MCP_KEY="$(openssl rand -hex 32)"
echo -n "$MCP_KEY" | gcloud secrets versions add absence-mcp-api-key --project="$PROJECT" --data-file=-
echo "Store MCP_KEY in your password manager; mcp-proxy needs it for upstream X-API-Key."
unset MCP_KEY

# Hawk credential id (public id string from absence.io)
gcloud secrets describe absence-api-key-id --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud secrets create absence-api-key-id --project="$PROJECT" --replication-policy=automatic
printf 'Paste absence.io Hawk id: '
read -r HAWK_ID
[ -n "$HAWK_ID" ] || { echo "Hawk id is empty; not uploading." >&2; exit 1; }
printf '%s' "$HAWK_ID" | gcloud secrets versions add absence-api-key-id --project="$PROJECT" --data-file=-

# Hawk secret key
gcloud secrets describe absence-api-key --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud secrets create absence-api-key --project="$PROJECT" --replication-policy=automatic
printf 'Paste absence.io Hawk secret key (input hidden): '
read -r -s HAWK_KEY
printf '\n'
[ -n "$HAWK_KEY" ] || { echo "Hawk key is empty; not uploading." >&2; exit 1; }
printf '%s' "$HAWK_KEY" | gcloud secrets versions add absence-api-key --project="$PROJECT" --data-file=-
unset HAWK_KEY
```

## One-time GCP bootstrap

Run from a machine with `gcloud` authenticated to the target project. Adjust **`PROJECT`** and **`REGION`** if needed.

```bash
PROJECT=it-team-hw-project
REGION=europe-west1
SA_ID=absence-mcp-sa
SA_EMAIL="${SA_ID}@${PROJECT}.iam.gserviceaccount.com"
PROXY_SA="mcp-proxy-sa@${PROJECT}.iam.gserviceaccount.com"
```

### 1. Service account (Cloud Run runtime identity)

```bash
gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_ID" \
    --project="$PROJECT" \
    --display-name="absence.io MCP (Cloud Run)" \
    --description="Runs absence-mcp; needs Secret Manager for MCP key and absence.io Hawk credentials"
```

No project-level roles are required beyond Secret Manager access on the three secrets (principle of least privilege).

### 2. Secrets

Create the three secrets and their first versions using the **Create secrets** block above (you can run it before or after the SA; IAM binding is step 3).

### 3. Grant the runtime SA access to all secrets

```bash
for SECRET in absence-mcp-api-key absence-api-key-id absence-api-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --project="$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor"
done
```

### 4. Deploy Cloud Run

From the **repository root** (same shape as [`mcp-deploy.yml`](../../../.github/workflows/mcp-deploy.yml)):

```bash
gcloud run deploy absence-mcp \
  --project="$PROJECT" \
  --region="$REGION" \
  --source=global/mcp/absence-mcp \
  --service-account="$SA_EMAIL" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT},API_KEY_SECRET_ID=absence-mcp-api-key,ABSENCE_HAWK_ID_SECRET_ID=absence-api-key-id,ABSENCE_HAWK_KEY_SECRET_ID=absence-api-key" \
  --no-allow-unauthenticated
```

Capture the service URL:

```bash
SERVICE_URL="$(gcloud run services describe absence-mcp --project="$PROJECT" --region="$REGION" --format='value(status.url)')"
echo "$SERVICE_URL"
```

### 5. Let mcp-proxy invoke the service (IAM)

Grant **`roles/run.invoker`** to the proxy SA (the **[MCP Deploy](../../../.github/workflows/mcp-deploy.yml)** workflow also does this after deploy):

```bash
gcloud run services add-iam-policy-binding absence-mcp \
  --project="$PROJECT" \
  --region="$REGION" \
  --member="serviceAccount:${PROXY_SA}" \
  --role="roles/run.invoker"
```

### 6. Smoke test

```bash
ID_TOKEN="$(gcloud auth print-identity-token --audiences="${SERVICE_URL}")"
curl -fsS -H "Authorization: Bearer ${ID_TOKEN}" "${SERVICE_URL}/health"
# Expect: {"status":"ok"}
```

MCP endpoint (needs ingress key):

```bash
curl -fsS -o /dev/null -w "%{http_code}\n" \
  -H "X-API-Key: $(gcloud secrets versions access latest --secret=absence-mcp-api-key --project="$PROJECT")" \
  "${SERVICE_URL}/mcp-server/mcp"
# Expect 200 or 405/406 depending on method; 401 means key mismatch.
```

## Bootstrap checklist (summary)

1. Create **`absence-mcp-sa`** (step 1 above).
2. Create **`absence-mcp-api-key`**, **`absence-api-key-id`**, **`absence-api-key`** and grant the SA accessor (steps 2–3 / **Create secrets**).
3. Deploy **absence-mcp** (step 4) or rely on CI when `global/mcp/absence-mcp/**` changes.
4. Grant **`mcp-proxy-sa`** **`run.invoker`** (step 5).
5. In **mcp-proxy**: add server **`id`: `absence`**, URL **`${SERVICE_URL}/mcp-server/mcp`**, upstream **`cloud_run_iam`** if that is your standard, plus credentials carrying **`X-API-Key`** for `absence-mcp-api-key`.
6. If you use per-user permissions, grant **Read** on server **`absence`** for roles that need the tools.

## Tools

| Tool                        | Description                                                                                                                                                                 |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `absence_list_users`        | POST `/users` with skip/limit; optional exclude inactive (status `3`); **`compact`** (default true) trims rows to id, names, email, status like `absence_find_user_by_name` |
| `absence_list_absences`     | POST `/absences` with filters; optional `overlap_date_iso` for “on this day”                                                                                                |
| `absence_get_user_absences` | Absences for one `user_id`, sorted by start ascending                                                                                                                       |

- **Pagination**: absence tools use **`skip` / `limit`** (offset pattern); increase **`skip`** by **`limit`** for the next page. Responses include **`count`** (`len(data)` on this page) and usually **`totalCount`** from the API. With **`exclude_inactive`** on `absence_list_users`, inactive users (**`status` 3**) are removed client-side—**`count`** is after filtering, while **`totalCount`** may still be the upstream unfiltered total. With default **`compact=true`** on `absence_list_users`, each user row omits heavy API-only fields; use **`compact=false`** for full documents.
- **Cross-tools**: call **`absence_list_users`** first when you need a valid **`user_id`** for **`absence_get_user_absences`**. For “absences on this day”, pass **`overlap_date_iso`** as UTC midnight (`YYYY-MM-DDT00:00:00.000Z`).
- **Summary vs expanded (`omit_user_details`)**: default **`true`** removes embedded **`assignedTo`** objects from each absence row; **`assignedToId`** stays. Set **`omit_user_details=false`** to keep full assignee objects from the API (PII-heavy). Applies to **`absence_list_absences`** and **`absence_get_user_absences`**.
- **Sort (`absence_list_absences`)**:
  - **`sort_by`**: Mongo-style map **`{ "fieldName": 1 }`** (asc) or **`{ "fieldName": -1 }`** (desc), e.g. `{"start": 1}`. A **string** field name is shorthand for ascending on that field.
  - **`sort_field` + `sort_direction`**: preferred when the client cannot send a map; **`sort_direction`** is **`1`** or **`-1`** and must pair with a non-empty **`sort_field`**.
  - Do **not** pass conflicting **`sort_by`** and **`sort_field`/`sort_direction`**.
  - If no sort is resolved, **`sortBy`** is omitted and **absence.io’s default ordering** applies.
- **Sort (`absence_get_user_absences`)**: always **start ascending** (`start: 1`); no `sort_by` override.
- **Empty vs error**: **`data: []`** with **`count: 0`** is a **successful empty result** (no matching rows). **`error`** at the top level means the call **failed** (bad parameters, network, API error)—distinct from zero matches.

Register mcp-proxy with **`id`: `absence`** so it matches [`_SERVERS_WITH_SELF_PREFIXED_TOOLS`](../mcp-proxy/mcp_proxy.py) (avoids `absence_absence_*` tool names).

## Environment variables

| Variable                                 | When        | Default                         | Description                          |
| ---------------------------------------- | ----------- | ------------------------------- | ------------------------------------ |
| `GCP_PROJECT_ID`                         | Always      | `it-team-hw-project`            | GCP project for Secret Manager       |
| `ENV`                                    | Local       | —                               | Set `local` to use env vars for keys |
| `API_KEY`                                | `ENV=local` | —                               | MCP ingress `X-API-Key`              |
| `ABSENCE_HAWK_ID` / `ABSENCE_API_KEY_ID` | `ENV=local` | —                               | Hawk id                              |
| `ABSENCE_HAWK_KEY` / `ABSENCE_API_KEY`   | `ENV=local` | —                               | Hawk secret key                      |
| `API_KEY_SECRET_ID`                      | Cloud Run   | `absence-mcp-api-key`           | MCP key secret id                    |
| `ABSENCE_HAWK_ID_SECRET_ID`              | Cloud Run   | `absence-api-key-id`            | Hawk id secret id                    |
| `ABSENCE_HAWK_KEY_SECRET_ID`             | Cloud Run   | `absence-api-key`               | Hawk key secret id                   |
| `ABSENCE_API_BASE_URL`                   | Optional    | `https://app.absence.io/api/v2` | API base (no trailing slash)         |
| `ALLOWED_INVOKER_SA_EMAILS`              | Cloud Run   | mcp-proxy SA                    | OIDC allowlist                       |
| `PORT`                                   | Runtime     | `8080`                          | HTTP port                            |

## Local run

```bash
cd global/mcp/absence-mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ENV=local
export API_KEY=local-dev-key
export ABSENCE_HAWK_ID=…
export ABSENCE_HAWK_KEY=…
uvicorn main:app --host 0.0.0.0 --port 8080
```

- Health: `http://localhost:8080/health`
- MCP: `http://localhost:8080/mcp-server/mcp` with header `X-API-Key: local-dev-key`

### MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector
# Connect to http://localhost:8080/mcp-server/mcp
# Header: X-API-Key: your-local-mcp-key
```

## Firestore / mcp-proxy

Add or update **`mcp_servers`** with id **`absence`**, **enabled**, URL **`${SERVICE_URL}/mcp-server/mcp`**, and upstream credentials your proxy expects (for example Secret Manager or inline header with **`X-API-Key`** matching `absence-mcp-api-key`). Use **`cloud_run_iam`** for transport if that is how other MCPs are configured.

Example (adjust auth to match your admin API):

```bash
curl -X POST https://your-mcp-proxy.run.app/admin/servers \
  -H "Authorization: Bearer <admin-jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "absence",
    "url": "https://absence-mcp-XXXX.run.app/mcp-server/mcp",
    "credentials_header": "X-API-Key: your-absence-mcp-api-key",
    "enabled": true
  }'
```

## Project structure

```
absence-mcp/
├── main.py
├── mcp_server.py
├── absence_client.py
├── pyproject.toml
├── requirements.txt
└── README.md
```

## Errors

Tools return JSON with **`error`** when configuration or absence.io requests fail. If you hit rate limits, retry with a smaller **`limit`** or add backoff in the client. An empty **`data`** list without **`error`** is not a failure.

### Example response shapes

Successful list (abbreviated):

```json
{
  "skip": 0,
  "limit": 100,
  "data": [{ "id": "…", "assignedToId": "…", "start": "…", "end": "…" }],
  "totalCount": 1,
  "count": 1
}
```

Successful empty list:

```json
{ "skip": 0, "limit": 100, "data": [], "totalCount": 0, "count": 0 }
```

Client/validation error:

```json
{
  "error": "sort_field/sort_direction conflicts with sort_by; use only one style."
}
```
