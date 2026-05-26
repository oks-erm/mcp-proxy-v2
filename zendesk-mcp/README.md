# Zendesk MCP

FastMCP + FastAPI. Exposes Zendesk **read-only** custom object list/get/search and **ticket** search, audits, users, and reservation record lookup.

- **Cloud Run ingress**: `--no-allow-unauthenticated` and `roles/run.invoker` for `mcp-proxy-sa@…` (OIDC).
- **MCP HTTP layer**: Accepts **verified** `Authorization: Bearer` Google ID tokens for this service URL (audience `https://<Host>/`) where the token `email` is in `ALLOWED_INVOKER_SA_EMAILS` (default `mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com`). **Or** `X-API-Key` matching Secret Manager `zendesk-mcp-api-key` (`API_KEY_SECRET_ID`) for direct clients / proxy setups that send the key.

Create `zendesk-mcp-sa@it-team-hw-project.iam.gserviceaccount.com` with **Secret Manager Secret Accessor** on `zendesk-credentials` and `zendesk-mcp-api-key` before the first deploy.

### Create `zendesk-mcp-api-key` (once per project)

```bash
PROJECT=it-team-hw-project
KEY="$(openssl rand -hex 32)"
gcloud secrets describe zendesk-mcp-api-key --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud secrets create zendesk-mcp-api-key --project="$PROJECT" --replication-policy=automatic
echo -n "$KEY" | gcloud secrets versions add zendesk-mcp-api-key --project="$PROJECT" --data-file=-
echo "Store this value in mcp-proxy Firestore/server config for the zendesk upstream: $KEY"
```

## Tools

| Tool                                   | Description                                  |
| -------------------------------------- | -------------------------------------------- |
| `zendesk_list_custom_object_records`   | Paginated list for a `custom_object_key`     |
| `zendesk_get_custom_object_record`     | Get one record by ID                         |
| `zendesk_search_custom_object_records` | Search within a custom object                |
| `zendesk_search_tickets`               | Search tickets (Zendesk query string)        |
| `zendesk_get_ticket_audits`            | Audits for one or more ticket IDs            |
| `zendesk_get_users`                    | Users by ID (chunked)                        |
| `zendesk_fetch_reservation_record`     | `reservations_data` record by reservation ID |

## Data sensitivity

Tickets, users, audits, and reservation-linked custom objects can contain **end-user PII and internal support context**. Treat tool output as **production Zendesk data** with **partial organizational access**—retrieve only what you need and avoid exfiltrating full ticket threads unnecessarily.

### Prefer search vs list (custom objects)

- **`zendesk_search_custom_object_records`** when you have **query filters** or need to match field values.
- **`zendesk_list_custom_object_records`** for **paginated enumeration** of a custom object when you are not using search predicates.

## Credentials

- **Production**: Secret Manager secret `zendesk-credentials` (JSON: `zd_email`, `zd_token`, `zd_subdomain`), same shape as `global/zendesk-bridge`. Override secret ID with `ZENDESK_SECRET_ID` or `ZENDESK_CREDENTIALS_SECRET_ID`.
- **Local**: `ENV=local` and set `ZD_EMAIL`, `ZD_TOKEN`, `ZD_SUBDOMAIN`, and `API_KEY` (for `X-API-Key` on `/mcp-server`).

## Environment variables

| Variable                                              | Default                  | Description                                                           |
| ----------------------------------------------------- | ------------------------ | --------------------------------------------------------------------- |
| `ENV`                                                 | —                        | `local` uses env vars for Zendesk + API key                           |
| `API_KEY`                                             | —                        | MCP key when `ENV=local`                                              |
| `API_KEY_SECRET_ID`                                   | `zendesk-mcp-api-key`    | MCP API key in Secret Manager (non-local)                             |
| `GCP_PROJECT_ID`                                      | `it-team-hw-project`     | GCP project                                                           |
| `ZENDESK_SECRET_ID` / `ZENDESK_CREDENTIALS_SECRET_ID` | `zendesk-credentials`    | Zendesk JSON secret                                                   |
| `ALLOWED_INVOKER_SA_EMAILS`                           | mcp-proxy SA (see above) | Comma-separated SA emails allowed to call `/mcp-server` via OIDC only |
| `PORT`                                                | `8080`                   | HTTP port                                                             |

## Local run

```bash
export ENV=local
export API_KEY=local-dev-key
export ZD_EMAIL=… ZD_TOKEN=… ZD_SUBDOMAIN=…
uvicorn main:app --host 0.0.0.0 --port 8080
```

MCP endpoint (Streamable HTTP): `http://localhost:8080/mcp-server/mcp`. Register the same path on the MCP proxy with **Upstream auth: Cloud Run IAM**.

### MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector
# Connect to http://localhost:8080/mcp-server/mcp
# Header: X-API-Key: your-local-mcp-key
```

## Deploying to Cloud Run

Ensure `zendesk-credentials` exists. Grant the Cloud Run service account `roles/secretmanager.secretAccessor` on `zendesk-mcp-api-key` and `zendesk-credentials`, then deploy (example):

```bash
cd global/mcp/zendesk-mcp
gcloud run deploy zendesk-mcp \
  --source . \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --set-env-vars="GCP_PROJECT_ID=it-team-hw-project,ZENDESK_SECRET_ID=zendesk-credentials,API_KEY_SECRET_ID=zendesk-mcp-api-key" \
  --service-account zendesk-mcp-sa@it-team-hw-project.iam.gserviceaccount.com \
  --no-allow-unauthenticated
```

Grant `mcp-proxy-sa` `roles/run.invoker` on the service.

## Via mcp-proxy

```bash
curl -X POST https://your-mcp-proxy.run.app/admin/servers \
  -H "X-Admin-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "zendesk",
    "url": "https://zendesk-mcp-XXXX.run.app/mcp-server/mcp",
    "credentials_header": "X-API-Key: your-zendesk-mcp-api-key",
    "enabled": true
  }'
```

## Project structure

```
zendesk-mcp/
├── main.py            # FastAPI, lifespan, X-API-Key guard, MCP mount
├── mcp_server.py      # FastMCP tools
├── zendesk_client.py  # ZendeskService (credentials + all API methods)
├── pyproject.toml
├── requirements.txt
└── README.md
```

## Errors

Ticket/reservation tools may return `{"error": "..."}` on failure. Custom object tools return `{"error": "Zendesk request failed"}` when the HTTP layer fails. **Zero search hits** are usually an empty result set in the tool’s normal shape, not the same as transport failure—check each tool’s return fields.

### Example shape (`zendesk_search_tickets` success)

```json
{ "count": 0, "tickets": [] }
```
