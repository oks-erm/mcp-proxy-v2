# Breezeway MCP Server

A FastAPI + FastMCP gateway that exposes the Breezeway inventory APIs (properties, people, tasks, reservation lookups) over the Model Context Protocol. The MCP tools stay stateless by reading the Breezeway JWT from Firestore (`tokens/breezeway`) while `main.py` enforces the MCP API key on every request.

## Tools

| Tool                        | Description                                                                               |
| --------------------------- | ----------------------------------------------------------------------------------------- |
| `breezeway_get_properties`  | Paginated list of properties (limit 1‑100 per request).                                   |
| `breezeway_get_users`       | Fetch every Breezeway person.                                                             |
| `breezeway_get_tasks`       | Paginated tasks list, optionally filtered by property.                                    |
| `breezeway_get_reservation` | Look up a reservation by external ID (`allow_multiple=true` when duplicates are allowed). |

## Data sensitivity

Properties, people (users), tasks, and reservations may include **guest, staff, and operational PII**. Treat tool output as **protected operational data**; avoid copying full payloads into untrusted logs or third-party prompts.

## Prerequisites

- Python 3.11+
- GCP project with Firestore (store the Breezeway JWT) and Secret Manager (hold the MCP API key)
- Firestore `tokens/breezeway` document containing `{"token": "<jwt>"}` so the tools can authenticate to Breezeway

## Environment Variables

| Variable                | Required         | Default                    | Description                                                  |
| ----------------------- | ---------------- | -------------------------- | ------------------------------------------------------------ |
| `ENV`                   | No               | unset                      | Set to `local` to skip Secret Manager and rely on `API_KEY`. |
| `API_KEY`               | When `ENV=local` | `local-dev-key`            | Local MCP API key for dev runs.                              |
| `API_KEY_SECRET_ID`     | When not local   | `breezeway-mcp-api-key`    | Secret Manager secret ID for the MCP API key.                |
| `GCP_PROJECT_ID`        | No               | `it-team-hw-project`       | Project with Firestore + Secret Manager resources.           |
| `GOOGLE_CLOUD_DATABASE` | No               | `data-warehouse-firestore` | Firestore database name used by the helper.                  |
| `PORT`                  | No               | `8080`                     | FastAPI/Uvicorn port.                                        |

## Running locally

```bash
cd global/mcp/breezeway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ENV=local
export API_KEY=your-local-mcp-key
# Optional: GCP_PROJECT_ID, GOOGLE_CLOUD_DATABASE if different

# Ensure Firestore has tokens/breezeway with {"token": "<jwt>"}
python main.py
# Server: http://localhost:8080
# MCP endpoint: http://localhost:8080/mcp-server/mcp
# Health: http://localhost:8080/health
```

### Testing with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector
# Connect to http://localhost:8080/mcp-server/mcp (POST)
# Header: X-API-Key: your-local-mcp-key
```

## Deploying to Cloud Run

### 1. Create the MCP API key secret

```bash
MCP_API_KEY=$(openssl rand -hex 32)
echo "Your MCP API Key: $MCP_API_KEY"

gcloud secrets create breezeway-mcp-api-key \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "$MCP_API_KEY" | gcloud secrets versions add breezeway-mcp-api-key \
  --project=it-team-hw-project \
  --data-file=-
```

### 2. Grant Secret Manager and Firestore access

```bash
SA="$(gcloud projects describe it-team-hw-project --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding breezeway-mcp-api-key \
  --project=it-team-hw-project \
  --member="serviceAccount:${SA}" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding it-team-hw-project \
  --member="serviceAccount:${SA}" \
  --role="roles/datastore.user"
```

### 3. Deploy from source

```bash
cd global/mcp/breezeway
gcloud run deploy breezeway-mcp \
  --source . \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --set-env-vars="GCP_PROJECT_ID=it-team-hw-project,API_KEY_SECRET_ID=breezeway-mcp-api-key" \
  --allow-unauthenticated
```

### 4. Verify

```bash
SERVICE_URL=$(gcloud run services describe breezeway-mcp --project=it-team-hw-project --region=europe-west1 --format='value(status.url)')
curl "$SERVICE_URL/health"
# POST to /mcp-server/mcp with header X-API-Key: $MCP_API_KEY
```

## Register with MCP proxy

```bash
curl -X POST https://your-mcp-proxy.run.app/admin/servers \
  -H "X-Admin-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "breezeway",
    "url": "https://breezeway-mcp-XXXX.run.app/mcp-server/mcp",
    "credentials_header": "X-API-Key: your-breezeway-mcp-api-key",
    "enabled": true
  }'
```

## Project structure

```
breezeway-mcp/
├── breezeway_client.py  # Firestore-backed Breezeway helpers
├── main.py              # FastAPI entry point + API key guard
├── mcp_server.py        # FastMCP tools exposing Breezeway data
├── pyproject.toml       # Poetry metadata
├── requirements.txt     # Cloud Run / pip dependencies
└── README.md
```

# Breezeway MCP Server

A Python MCP (Model Context Protocol) server that exposes the read-only Breezeway inventory APIs (properties, users, tasks, reservations) through MCP tools. Built with the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) so it can run locally or on **Google Cloud Run** alongside the other MCP services.

## Read-only Tools

| Tool                        | Description                                                             |
| --------------------------- | ----------------------------------------------------------------------- |
| `breezeway_get_properties`  | Paginated list of Breezeway properties (limit up to 100, page by page). |
| `breezeway_get_users`       | Fetch all Breezeway people (users) in one go.                           |
| `breezeway_get_tasks`       | Paginated list of Breezeway tasks, with optional property filtering.    |
| `breezeway_get_reservation` | Look up a reservation by external ID (optional `allow_multiple=true`).  |

The operations are backed by `shared.firestore.FirestoreService` to pull the Breezeway JWT from Firestore (`tokens/breezeway`) so that the MCP tools stay stateless.

**Data sensitivity:** properties, people, tasks, and reservations may include **guest and operational PII**—treat responses as protected.

## Prerequisites

- Python 3.11+
- GCP project with Firestore (for storing the Breezeway JWT) and Secret Manager (for the MCP API key)
- A valid Breezeway JWT stored in Firestore at `tokens/breezeway` under the `token` field

## Environment Variables

| Variable                | Required         | Default                    | Description                                                                      |
| ----------------------- | ---------------- | -------------------------- | -------------------------------------------------------------------------------- |
| `ENV`                   | No               | —                          | Set to `local` to skip Secret Manager and read config purely from env variables. |
| `API_KEY`               | When `ENV=local` | `local-dev-key`            | MCP API key used by clients when running locally.                                |
| `API_KEY_SECRET_ID`     | When not local   | `breezeway-mcp-api-key`    | Secret Manager secret ID that stores the MCP API key.                            |
| `GCP_PROJECT_ID`        | No               | `it-team-hw-project`       | GCP project containing Firestore + Secret Manager.                               |
| `GOOGLE_CLOUD_DATABASE` | No               | `data-warehouse-firestore` | Firestore database name used by `FirestoreService`.                              |
| `PORT`                  | No               | `8080`                     | HTTP port used by FastAPI/Uvicorn.                                               |

The Breezeway JWT itself is kept in Firestore rather than Secret Manager; ensure the service account running the MCP server can read from `tokens/breezeway`.

## Running Locally

```bash
# 1. Create a virtual environment and install dependencies
cd global/mcp/breezeway
python3 -m venv .venv
source .venv/bin/activate  # on Windows use .venv\Scripts\activate
pip install -r requirements.txt

# 2. For local dev: set ENV=local and provide API_KEY
export ENV=local
export API_KEY=your-local-mcp-key
# Optional: GCP_PROJECT_ID, GOOGLE_CLOUD_DATABASE if different

# 3. Ensure Firestore has the Breezeway JWT:
#    Collection `tokens`, document `breezeway` with {"token": "<jwt>"}

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

gcloud secrets create breezeway-mcp-api-key \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "$MCP_API_KEY" | gcloud secrets versions add breezeway-mcp-api-key \
  --project=it-team-hw-project \
  --data-file=-
```

### 2. Grant Secret Manager and Firestore access

The Cloud Run service account must be able to read `breezeway-mcp-api-key` and query Firestore for `tokens/breezeway`.

```bash
SA="$(gcloud projects describe it-team-hw-project --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding breezeway-mcp-api-key \
  --project=it-team-hw-project \
  --member="serviceAccount:${SA}" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding it-team-hw-project \
  --member="serviceAccount:${SA}" \
  --role="roles/datastore.user"
```

### 3. Deploy from source

```bash
cd global/mcp/breezeway
gcloud run deploy breezeway-mcp \
  --source . \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --set-env-vars="GCP_PROJECT_ID=it-team-hw-project,API_KEY_SECRET_ID=breezeway-mcp-api-key" \
  --allow-unauthenticated
```

### 4. Verify

```bash
SERVICE_URL=$(gcloud run services describe breezeway-mcp --region=europe-west1 --project=it-team-hw-project --format='value(status.url)')
curl "$SERVICE_URL/health"
# With API key:
# curl -H "X-API-Key: $MCP_API_KEY" -X POST "$SERVICE_URL/mcp-server/mcp" ...
```

## MCP Client Configuration

### Cursor / Claude Desktop

```json
{
  "mcpServers": {
    "breezeway": {
      "url": "https://breezeway-mcp-XXXX.run.app/mcp-server/mcp",
      "headers": {
        "X-API-Key": "your-mcp-api-key"
      }
    }
  }
}
```

### Via mcp-proxy

Register the Breezeway MCP as an upstream so the tools appear under the proxy prefix (e.g. `breezeway_get_properties`):

```bash
curl -X POST https://your-mcp-proxy.run.app/admin/servers \
  -H "X-Admin-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "breezeway",
    "url": "https://breezeway-mcp-XXXX.run.app/mcp-server/mcp",
    "credentials_header": "X-API-Key: your-breezeway-mcp-api-key",
    "enabled": true
  }'
```

## Project Structure

```
breezeway-mcp/
├── main.py           # FastAPI app, lifespan, auth middleware, MCP mount
├── mcp_server.py     # FastMCP server, Breezeway read-only tools
├── pyproject.toml    # Dependencies (Poetry)
├── requirements.txt  # For Cloud Run / pip
└── README.md
```
