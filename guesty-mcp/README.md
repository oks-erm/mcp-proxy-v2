# Guesty MCP Server

A Python MCP (Model Context Protocol) server that exposes [Guesty](https://www.guesty.com/) (vacation rental PMS) features as MCP tools. Uses a self-contained Guesty API client (OAuth2 client credentials, token stored in Firestore, credentials from Secret Manager). Built with the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), deployable to **Google Cloud Run**.

## Features

| Tool                                      | Description                                                                                                                                                                  |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `guesty_get_reservations`                 | Get reservations with optional limit, skip, sort, fields                                                                                                                     |
| `guesty_search_reservations`              | Search reservations with filters; optional fetch_all pagination                                                                                                              |
| `guesty_get_listings`                     | Get listings; optional limit, skip, sort; **`compact`** (default true) whitelists core fields + `address` city/country, optional `cleaningStatus.value`, `picture.thumbnail` |
| `guesty_search_listings`                  | Search listings; optional fetch_all; **`compact`** (default true) same whitelisted row shape as get_listings                                                                 |
| `guesty_search_reviews`                   | Search reviews; optional start_date, end_date, fetch_all                                                                                                                     |
| `guesty_search_owners`                    | Search owners; optional fetch_all pagination                                                                                                                                 |
| `guesty_get_guests`                       | Get guests with optional limit, skip                                                                                                                                         |
| `guesty_get_guest`                        | Get a single guest by ID                                                                                                                                                     |
| `guesty_search_guests`                    | Search guests; optional columns, fetch_all                                                                                                                                   |
| `guesty_get_webhooks`                     | List all webhooks                                                                                                                                                            |
| `guesty_get_webhook`                      | Get a webhook by ID                                                                                                                                                          |
| `guesty_create_webhook`                   | Create webhook (events list + url)                                                                                                                                           |
| `guesty_update_webhook`                   | Update webhook (webhook_id, events, url)                                                                                                                                     |
| `guesty_delete_webhook`                   | Delete a webhook by ID                                                                                                                                                       |
| `guesty_get_webhook_secret`               | Get webhook signing secret                                                                                                                                                   |
| `guesty_update_reservation_custom_fields` | Update custom fields on a reservation                                                                                                                                        |
| `guesty_update_listing_custom_fields`     | Update custom fields on a listing                                                                                                                                            |
| `guesty_get_listing_calendar`             | Get availability/pricing calendar for a listing                                                                                                                              |

| `guesty_find_listing` | Resolver: find listings by human name or nickname. **Primary arg is `query` — not `name` or `listing_name`.** `query, limit, detail_level` → `{"count", "data", "detail_level": "compact"}` |
| `guesty_find_reservation` | Resolver: find reservations by confirmation code or guest name. `confirmation_code, limit` → `{"count", "data"}` |
| `guesty_get_listing_summary` | Get one listing's key fields by id. `listing_id` |
| `guesty_is_listing_available_on_dates` | Check availability for a listing on specific dates. `listing_id, check_in, check_out` |

**Resource:** `guesty://webhook-events` — lists valid webhook event names (e.g. `reservation.new`, `listing.updated`) for use in create/update webhook tools.

> **Resolver tools** (`guesty_find_listing`, `guesty_find_reservation`) use **`query`** or a specific search key as their primary argument — do not pass `name`, `title`, or `listing_name`.

### Prefer search vs list/get

- Prefer **`guesty_search_reservations`**, **`guesty_search_listings`**, **`guesty_search_guests`**, **`guesty_search_owners`**, **`guesty_search_reviews`** when you have **filters, text, or date criteria** (`fetch_all` optional).
- Prefer **`guesty_get_reservations`**, **`guesty_get_listings`**, **`guesty_get_guests`** with **`skip` / `limit`** (and optional sort) when **browsing or paging** without a search predicate.

**Detail vs summary:** use **`guesty_get_guest`** / **`guesty_get_webhook`** (by id) when you already have an identifier; use list/search tools to discover ids.

## Prerequisites

- Python 3.11+
- GCP project with Firestore and Secret Manager (for Guesty token storage and MCP API key)
- Guesty API credentials (`client_id`, `client_secret`) stored in GCP Secret Manager secret `guesty-api-key` (or override with `GUESTY_SECRET_ID`)

## Environment Variables

| Variable                | Required         | Default                    | Description                                                                               |
| ----------------------- | ---------------- | -------------------------- | ----------------------------------------------------------------------------------------- |
| `ENV`                   | No               | —                          | Set to `local` to use `API_KEY` from env; otherwise API key is loaded from Secret Manager |
| `API_KEY`               | When `ENV=local` | —                          | MCP client API key (for local dev)                                                        |
| `API_KEY_SECRET_ID`     | When not local   | `guesty-mcp-api-key`       | Secret Manager secret ID for MCP API key                                                  |
| `GCP_PROJECT_ID`        | No               | `it-team-hw-project`       | GCP project ID (Secret Manager + Firestore)                                               |
| `GOOGLE_CLOUD_DATABASE` | No               | `data-warehouse-firestore` | Firestore database name for token storage                                                 |
| `GUESTY_SECRET_ID`      | No               | `guesty-api-key`           | Secret Manager secret ID for Guesty client_id/client_secret                               |
| `PORT`                  | No               | `8080`                     | HTTP port                                                                                 |

Guesty API credentials are **not** set in guesty-mcp env; they are in the `guesty-api-key` secret (`client_id`, `client_secret`). Override the secret name with `GUESTY_SECRET_ID`.

## Running Locally

```bash
# 1. Create a virtual environment and install dependencies
cd global/mcp/guesty-mcp
python3 -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# 2. For local dev: use ENV=local and set API_KEY in env (no Secret Manager for MCP key)
export ENV=local
export API_KEY=your-local-mcp-key
# Optional: GCP_PROJECT_ID, GOOGLE_CLOUD_DATABASE if different

# 3. Ensure Guesty credentials exist in GCP Secret Manager (guesty-api-key with client_id, client_secret)
#    and that Firestore is available for token storage (tokens/guesty document).

# 4. Start the server
python main.py
# Server: http://localhost:8080
# MCP endpoint: http://localhost:8080/mcp-server/mcp
# Health: http://localhost:8080/health
```

### Testing with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector
# Connect to http://localhost:8080/mcp-server/mcp
# Add header: X-API-Key: your-api-key
```

## Deploying to Cloud Run

### 1. Create the MCP API key secret

```bash
MCP_API_KEY=$(openssl rand -hex 32)
echo "Your MCP API Key: $MCP_API_KEY"

gcloud secrets create guesty-mcp-api-key \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "$MCP_API_KEY" | gcloud secrets versions add guesty-mcp-api-key \
  --project=it-team-hw-project \
  --data-file=-
```

Ensure the `guesty-api-key` secret exists with Guesty `client_id` and `client_secret` (used by shared GuestyService).

### 2. Grant Secret Manager and Firestore access to the Cloud Run service account

The service account needs access to `guesty-mcp-api-key` and `guesty-api-key`, and to Firestore (for Guesty token storage).

```bash
SA="$(gcloud projects describe it-team-hw-project --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

for SECRET in guesty-mcp-api-key guesty-api-key; do
  gcloud secrets add-iam-policy-binding $SECRET \
    --project=it-team-hw-project \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"
done

# Firestore (if not already granted)
gcloud projects add-iam-policy-binding it-team-hw-project \
  --member="serviceAccount:${SA}" \
  --role="roles/datastore.user"
```

### 3. Deploy from source

From the repo root (so `shared` is available) or ensure the built `shared` package is installable:

```bash
cd global/mcp/guesty-mcp
gcloud run deploy guesty-mcp \
  --source . \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --set-env-vars="GCP_PROJECT_ID=it-team-hw-project,API_KEY_SECRET_ID=guesty-mcp-api-key" \
  --no-allow-unauthenticated
```

The app loads the MCP API key from Secret Manager at startup (using `API_KEY_SECRET_ID`). Ensure the Cloud Run service account has `roles/secretmanager.secretAccessor` on `guesty-mcp-api-key` and `guesty-api-key`.

Grant **`mcp-proxy-sa`** **`roles/run.invoker`** on this service if you use mcp-proxy (see **Via mcp-proxy** below), or call with a Google ID token that has invoker.

### 4. Verify

```bash
SERVICE_URL=$(gcloud run services describe guesty-mcp --region=europe-west1 --project=it-team-hw-project --format='value(status.url)')
ID_TOKEN="$(gcloud auth print-identity-token --audiences="${SERVICE_URL}")"
curl -fsS -H "Authorization: Bearer ${ID_TOKEN}" "${SERVICE_URL}/health"
# MCP (Streamable HTTP): same Bearer plus X-API-Key header for the app middleware.
```

## MCP Client Configuration

### Cursor / Claude Desktop

```json
{
  "mcpServers": {
    "guesty": {
      "url": "https://guesty-mcp-XXXXX.run.app/mcp-server/mcp",
      "headers": {
        "X-API-Key": "your-mcp-api-key"
      }
    }
  }
}
```

### Via mcp-proxy

Cloud Run is deployed **without** public access (`--no-allow-unauthenticated` in CI). If the proxy uses **`headers`** with only `X-API-Key`, Cloud Run returns **403 Forbidden** before the app runs.

1. Grant **`mcp-proxy-sa`** **`roles/run.invoker`** on this service:

   ```bash
   gcloud run services add-iam-policy-binding guesty-mcp \
     --region=europe-west1 \
     --member=serviceAccount:mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com \
     --role=roles/run.invoker
   ```

2. **MCP Proxy** upstream: **`upstream_auth`: `cloud_run_iam`**, plus **`X-API-Key`** in `credentials_header` or Secret Manager (non-`Authorization` keys are merged with OIDC).

```bash
curl -X POST https://your-mcp-proxy.run.app/admin/servers \
  -H "X-Admin-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "guesty",
    "url": "https://guesty-mcp-XXXXX.run.app/mcp-server/mcp",
    "upstream_auth": "cloud_run_iam",
    "credentials_header": "X-API-Key: your-guesty-mcp-api-key",
    "enabled": true
  }'
```

Or use `credentials_secret_id` pointing to a secret with MCP headers (e.g. `X-API-Key`).

For **local** Guesty MCP without Cloud Run IAM, **`headers`** and `credentials_header` only is enough.

## Project Structure

```
guesty-mcp/
├── main.py           # FastAPI app, lifespan, auth middleware, MCP mount
├── mcp_server.py     # FastMCP server, tools + webhook-events resource
├── guesty_client.py  # Self-contained Guesty API client (OAuth2, Firestore token, Secret Manager)
├── pyproject.toml    # Dependencies (Poetry)
├── requirements.txt  # For Cloud Run / pip
└── README.md
```

## Errors

If a Guesty API call fails, tools return a JSON object with an `"error"` key (e.g. `{"error": "Guesty API request failed or returned no data"}`). Invalid webhook event names in create/update webhook return `{"error": "Invalid webhook event name: ..."}`.
