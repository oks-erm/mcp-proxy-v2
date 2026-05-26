# PriceLabs MCP Server

Read-only MCP server for the PriceLabs Customer API. Initial tools cover:

| Tool                                    | Description                                                       |
| --------------------------------------- | ----------------------------------------------------------------- |
| `pricelabs_get_neighborhood_data`       | Fetch Neighborhood Data for one PriceLabs listing.                |
| `pricelabs_get_date_specific_overrides` | Fetch existing Date Specific Overrides for one PriceLabs listing. |

The server intentionally does not expose Date Specific Override writes yet.

## Configuration

| Variable                      | Required         | Default                                                   | Description                                                  |
| ----------------------------- | ---------------- | --------------------------------------------------------- | ------------------------------------------------------------ |
| `ENV`                         | No               | -                                                         | Set to `local` to use local env keys.                        |
| `API_KEY`                     | When `ENV=local` | -                                                         | MCP ingress key for local use.                               |
| `PRICELABS_API_KEY`           | When `ENV=local` | -                                                         | PriceLabs Customer API key.                                  |
| `API_KEY_SECRET_ID`           | Production       | `pricelabs-mcp-api-key`                                   | Secret Manager secret for MCP ingress key.                   |
| `PRICELABS_API_KEY_SECRET_ID` | Production       | `pricelabs-api-key`                                       | Secret Manager secret for the PriceLabs Customer API key.    |
| `PRICELABS_API_BASE`          | No               | `https://api.pricelabs.co`                                | PriceLabs API base URL.                                      |
| `PRICELABS_TIMEOUT_SECONDS`   | No               | `300`                                                     | Upstream request timeout.                                    |
| `GCP_PROJECT_ID`              | No               | `it-team-hw-project`                                      | GCP project for Secret Manager.                              |
| `ALLOWED_INVOKER_SA_EMAILS`   | No               | `mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com` | Comma-separated service accounts allowed via Cloud Run OIDC. |
| `PORT`                        | No               | `8080`                                                    | HTTP port.                                                   |

## Running Locally

```bash
cd global/mcp/pricelabs-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ENV=local
export API_KEY=local-dev-key
export PRICELABS_API_KEY=your-pricelabs-api-key
python main.py
```

MCP endpoint: `http://localhost:8080/mcp-server/mcp` with header `X-API-Key: local-dev-key`.

## Deploying to Cloud Run

Create separate secrets for the MCP ingress key and the PriceLabs upstream key:

```bash
gcloud secrets create pricelabs-mcp-api-key --project=it-team-hw-project --replication-policy=automatic
echo -n "$MCP_API_KEY" | gcloud secrets versions add pricelabs-mcp-api-key \
  --project=it-team-hw-project \
  --data-file=-

gcloud secrets create pricelabs-api-key --project=it-team-hw-project --replication-policy=automatic
echo -n "$PRICELABS_API_KEY" | gcloud secrets versions add pricelabs-api-key \
  --project=it-team-hw-project \
  --data-file=-
```

Grant the Cloud Run runtime service account Secret Manager access to both secrets, deploy the service, and allow the MCP proxy service account to invoke it:

```bash
cd global/mcp/pricelabs-mcp
gcloud run deploy pricelabs-mcp \
  --source . \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --set-env-vars="GCP_PROJECT_ID=it-team-hw-project,API_KEY_SECRET_ID=pricelabs-mcp-api-key,PRICELABS_API_KEY_SECRET_ID=pricelabs-api-key" \
  --no-allow-unauthenticated
```

Add the upstream to mcp-proxy with server id `pricelabs`, URL `${SERVICE_URL}/mcp-server/mcp`, and `upstream_auth` set to `cloud_run_iam`. If you use the app-level `X-API-Key` in addition to OIDC, store `X-API-Key: <pricelabs-mcp-api-key>` as the upstream credential.
