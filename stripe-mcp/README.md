# Stripe MCP Server

A Python MCP (Model Context Protocol) server that exposes [Stripe](https://stripe.com/) features as MCP tools: payouts, payout transactions, charges, balance, balance transactions, and refunds. Built with the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), deployable to **Google Cloud Run**.

## Features

| Tool                             | Description                                                 |
| -------------------------------- | ----------------------------------------------------------- |
| `stripe_get_payouts`             | List payouts (optional date range; default: previous month) |
| `stripe_get_payout_transactions` | List balance transactions for a payout                      |
| `stripe_get_charges`             | List charges (optional limit)                               |
| `stripe_get_charge`              | Get a charge by ID                                          |
| `stripe_get_balance`             | Get account balance (available/pending)                     |
| `stripe_get_balance_transaction` | Get a balance transaction by ID                             |
| `stripe_get_refund`              | Get a refund by ID                                          |

## Stripe object ID prefixes (detail tools)

Use the tool that matches the ID type:

| Prefix | Object              | Typical detail tool                                                              |
| ------ | ------------------- | -------------------------------------------------------------------------------- |
| `ch_`  | Charge              | `stripe_get_charge`                                                              |
| `pi_`  | PaymentIntent       | `stripe_get_payment_intent`                                                      |
| `po_`  | Payout              | (list via `stripe_get_payouts`; line items via `stripe_get_payout_transactions`) |
| `txn_` | Balance transaction | `stripe_get_balance_transaction`                                                 |
| `re_`  | Refund              | `stripe_get_refund`                                                              |

**Prefer** `stripe_get_charges` to discover recent `ch_` IDs; **prefer** `stripe_get_charge` when you already have a charge id. Same pattern: list tools for discovery, detail tools when the id is known.

## Data sensitivity

Charge, payment, payout, and refund payloads can include **cardholder, customer, and bank-related fields**. Treat responses as **financial / regulated-adjacent data**; minimize retention in prompts and logs.

## Prerequisites

- Python 3.11+
- GCP project with Secret Manager (for Stripe API key and MCP API key)
- Stripe API key stored in GCP Secret Manager secret `stripe-api-key` (or override with `STRIPE_API_KEY_SECRET_ID`). Same secret name and key as [shared/stripe.py](../../../shared/shared/stripe.py): JSON key `stripe_api_key` (e.g. `{"stripe_api_key": "sk_..."}`) or raw API key.

## Environment Variables

| Variable                   | Required         | Default                  | Description                                                                               |
| -------------------------- | ---------------- | ------------------------ | ----------------------------------------------------------------------------------------- |
| `ENV`                      | No               | —                        | Set to `local` to use `API_KEY` from env; otherwise API key is loaded from Secret Manager |
| `API_KEY`                  | When `ENV=local` | `local-dev-key`          | MCP client API key (for local dev)                                                        |
| `API_KEY_SECRET_ID`        | When not local   | `stripe-mcp-api-key`     | Secret Manager secret ID for MCP API key                                                  |
| `GCP_PROJECT_ID`           | No               | `it-team-hw-project`     | GCP project ID (Secret Manager)                                                           |
| `GOOGLE_CLOUD_PROJECT`     | No               | same as `GCP_PROJECT_ID` | Alternative env for GCP project                                                           |
| `STRIPE_API_KEY_SECRET_ID` | No               | `stripe-api-key`         | Secret Manager secret ID for Stripe API key                                               |
| `PORT`                     | No               | `8080`                   | HTTP port                                                                                 |

Stripe API key is loaded from Secret Manager (same as shared). For local dev, set `ENV=local` and `API_KEY` to skip loading the MCP gateway key from Secret Manager.

## Running Locally

```bash
# 1. Create a virtual environment and install dependencies
cd global/mcp/stripe-mcp
python3 -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# 2. For local dev: use ENV=local and set API_KEY in env (no Secret Manager for MCP key)
export ENV=local
export API_KEY=your-local-mcp-key
# Optional: GCP_PROJECT_ID, STRIPE_API_KEY_SECRET_ID if different

# 3. Ensure Stripe API key exists in GCP Secret Manager (stripe-api-key).

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

gcloud secrets create stripe-mcp-api-key \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "$MCP_API_KEY" | gcloud secrets versions add stripe-mcp-api-key \
  --project=it-team-hw-project \
  --data-file=-
```

Ensure the `stripe-api-key` secret exists with your Stripe secret key (raw or JSON `{"stripe_api_key": "sk_..."}`).

### 2. Grant Secret Manager access to the Cloud Run service account

The service account needs access to `stripe-mcp-api-key` and `stripe-api-key`.

```bash
SA="$(gcloud projects describe it-team-hw-project --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

for SECRET in stripe-mcp-api-key stripe-api-key; do
  gcloud secrets add-iam-policy-binding $SECRET \
    --project=it-team-hw-project \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

### 3. Deploy from source

```bash
cd global/mcp/stripe-mcp
gcloud run deploy stripe-mcp \
  --source . \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --set-env-vars="GCP_PROJECT_ID=it-team-hw-project,API_KEY_SECRET_ID=stripe-mcp-api-key" \
  --no-allow-unauthenticated
```

The app reads secrets by name at startup. Override `STRIPE_API_KEY_SECRET_ID` / `API_KEY_SECRET_ID` if your secret names differ.

### 4. Verify

```bash
SERVICE_URL=$(gcloud run services describe stripe-mcp --region=europe-west1 --project=it-team-hw-project --format='value(status.url)')
curl "$SERVICE_URL/health"
# With API key (Streamable HTTP JSON-RPC):
# curl -H "X-API-Key: $MCP_API_KEY" -X POST "$SERVICE_URL/mcp-server/mcp" ...
```

## MCP Client Configuration

### Cursor / Claude Desktop

```json
{
  "mcpServers": {
    "stripe": {
      "url": "https://stripe-mcp-XXXX.run.app/mcp-server/mcp",
      "headers": {
        "X-API-Key": "your-mcp-api-key"
      }
    }
  }
}
```

### Via mcp-proxy

Cloud Run is deployed **without** public access (`--no-allow-unauthenticated`). Google returns **403 Forbidden** to callers that only send `X-API-Key` until **Cloud Run IAM** allows the caller.

1. Grant the MCP proxy service account invoker on this service (once per deploy / org):

   ```bash
   gcloud run services add-iam-policy-binding stripe-mcp \
     --region=europe-west1 \
     --member=serviceAccount:mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com \
     --role=roles/run.invoker
   ```

2. Register stripe-mcp with **`upstream_auth`: `cloud_run_iam`** so the proxy sends an OIDC token to Cloud Run, **and** keep the app-level key in credentials (the FastAPI app still checks `X-API-Key` after the request reaches the container):

```bash
curl -X POST https://your-mcp-proxy.run.app/admin/servers \
  -H "X-Admin-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "stripe",
    "url": "https://stripe-mcp-XXXX.run.app/mcp-server/mcp",
    "upstream_auth": "cloud_run_iam",
    "credentials_header": "X-API-Key: your-stripe-mcp-api-key",
    "enabled": true
  }'
```

Or use `credentials_secret_id` pointing to a secret with MCP headers (non-`Authorization` keys are merged when `upstream_auth` is `cloud_run_iam`).

If the server were deployed with `--allow-unauthenticated`, **`headers`** mode with only `credentials_header` would reach the app, but the current workflow uses authenticated Cloud Run ingress.

## Project Structure

```
stripe-mcp/
├── main.py           # FastAPI app, lifespan, auth middleware, MCP mount
├── mcp_server.py     # FastMCP server, Stripe tools
├── pyproject.toml    # Dependencies (Poetry)
├── requirements.txt  # For Cloud Run / pip
└── README.md
```

## Dependencies

- Python 3.11+
- [stripe](https://pypi.org/project/stripe/) (Stripe SDK)
- [mcp](https://pypi.org/project/mcp/) (FastMCP, streamable HTTP)
- FastAPI, Uvicorn, google-cloud-secret-manager, python-dotenv

## Example tool shapes

Success (list):

```json
{ "charges": [{ "id": "ch_...", "amount": 1000, "currency": "eur" }] }
```

Success (empty is normal):

```json
{ "payout_id": "po_...", "transactions": [] }
```

Error:

```json
{ "error": "charge_id is required" }
```
