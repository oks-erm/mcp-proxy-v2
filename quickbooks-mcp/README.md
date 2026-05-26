# QuickBooks MCP Server

MCP server that exposes QuickBooks bank data: **bank accounts**, **transactions in** (deposits), broader **bank credits**, **transactions out**, **balances**, and connected company context. Uses the same Firestore-backed QuickBooks tokens as the rest of the repo (`tokens/quickbooks`).

## Tools

- **list_bank_accounts** (`quickbooks_list_bank_accounts` via proxy) – List all QuickBooks bank accounts (id, name, current balance, currency).
- **get_bank_transactions_in** (`quickbooks_get_bank_transactions_in` via proxy) – Deposits (money in), with optional date range and `account_id` filter. Dates in `YYYY-MM-DD`.
- **get_bank_credits** (`quickbooks_get_bank_credits` via proxy) – Broader incoming credit candidates for reconciliation across Deposit, incoming Transfer, Payment, and SalesReceipt. Supports date range, receiving `account_id`, and exact `external_transaction_id` matching against exposed transaction identifiers (`Id`, `DocNumber`, `PaymentRefNum`) after retrieval.
- **get_bank_transactions_out** (`quickbooks_get_bank_transactions_out` via proxy) – Money leaving bank accounts: transfers, API `Purchase` records (QuickBooks UI expenses/checks), and bank-paid `BillPayment` records, with optional date range and `from_account_id` / `to_account_id` filters.
- **get_bank_balances** (`quickbooks_get_bank_balances` via proxy) – Current balance per bank account.
- **get_bank_account_by_name** (`quickbooks_get_bank_account_by_name` via proxy) – Resolver: find bank accounts by human name. **Primary arg is `query` — not `name` or `account_name`.** `query, limit` → `{"count", "data", "detail_level": "compact"}` with id, name, account_type, account_sub_type, currency_code. Use before any account-ID-based tools.
- **get_company_context** (`quickbooks_get_company_context` via proxy) – Diagnostic context for the connected realm/company (`realm_id`, environment, company name, legal name, country, email).

> **Resolver tools** (`get_bank_account_by_name` / `quickbooks_get_bank_account_by_name` via proxy) use **`query`** as the search argument — do not pass `name` or `account_name`.

`get_bank_credits` uses public QuickBooks Online Accounting API entities. It is not a raw bank-feed/FITID endpoint; if you need to match non-QuickBooks bank feed identifiers, call it with a date/account window and reconcile against the exposed transaction fields returned by QuickBooks.

## Prerequisites

- Python 3.11+
- QuickBooks OAuth tokens stored in Firestore: collection `tokens`, document `quickbooks`.
- GCP project with Firestore and (for production) Secret Manager for the MCP API key.

## Local setup

1. Install dependencies (from `global/mcp/quickbooks-mcp`):

   ```bash
   pip install -r requirements.txt
   # or: pip install -e .
   ```

2. Set environment variables:

   ```bash
   export ENV=local
   export API_KEY=local-dev-key
   export GOOGLE_CLOUD_PROJECT=it-team-hw-project   # or GCP_PROJECT_ID
   export GOOGLE_CLOUD_DATABASE=data-warehouse-firestore
   ```

   Use credentials that can read the Firestore database where `tokens/quickbooks` lives.

3. Run the server:

   ```bash
   python main.py
   # or: uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

4. MCP endpoint: `http://localhost:8000/mcp-server/mcp` with header `X-API-Key: local-dev-key`.

## Creating the MCP API key secret (production)

Create the MCP API key in Secret Manager and grant the Cloud Run service account access:

```bash
MCP_API_KEY=$(openssl rand -hex 32)
echo "Your MCP API Key: $MCP_API_KEY"

gcloud secrets create quickbooks-mcp-api-key \
  --project=it-team-hw-project \
  --replication-policy=automatic

echo -n "$MCP_API_KEY" | gcloud secrets versions add quickbooks-mcp-api-key \
  --project=it-team-hw-project \
  --data-file=-
```

Grant the default Cloud Run service account access to the secret and to Firestore (for `tokens/quickbooks`):

```bash
SA="$(gcloud projects describe it-team-hw-project --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding quickbooks-mcp-api-key \
  --project=it-team-hw-project \
  --member="serviceAccount:${SA}" \
  --role="roles/secretmanager.secretAccessor"

# Firestore (if not already granted)
gcloud projects add-iam-policy-binding it-team-hw-project \
  --member="serviceAccount:${SA}" \
  --role="roles/datastore.user"
```

## Production / Deploying to Cloud Run

1. Create the MCP API key secret and grant IAM (see [Creating the MCP API key secret](#creating-the-mcp-api-key-secret-production) above).
2. Ensure QuickBooks OAuth tokens exist in Firestore (`tokens/quickbooks`).
3. Deploy:

   ```bash
   cd global/mcp/quickbooks-mcp
   gcloud run deploy quickbooks-mcp \
     --source . \
     --project=it-team-hw-project \
     --region=europe-west1 \
     --set-env-vars="GCP_PROJECT_ID=it-team-hw-project,API_KEY_SECRET_ID=quickbooks-mcp-api-key" \
     --no-allow-unauthenticated
   ```

   Grant the MCP proxy invoker (see **Register with MCP proxy** below), or callers must use a Google ID token with `roles/run.invoker`.

4. Verify `/health` with an identity token, or from mcp-proxy diagnostics after configuring `cloud_run_iam`.

## Register with MCP proxy

Add this server as an upstream in the [mcp-proxy](global/mcp/mcp-proxy/).

Cloud Run is deployed **without** public access (`--no-allow-unauthenticated` in CI). If the proxy uses **`headers`** with only `X-API-Key`, Cloud Run returns **403 Forbidden** before the app runs. You need:

1. **`mcp-proxy-sa`** granted **`roles/run.invoker`** on this service:

   ```bash
   gcloud run services add-iam-policy-binding quickbooks-mcp \
     --region=europe-west1 \
     --member=serviceAccount:mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com \
     --role=roles/run.invoker
   ```

2. **MCP Proxy** upstream: **`upstream_auth`: `cloud_run_iam`**, plus the app key in credentials (the FastAPI middleware still checks `X-API-Key`):

   - **Inline**: `credentials_header`: `"X-API-Key: <your-api-key>"`.
   - **Secret Manager**: `credentials_secret_id`: secret whose JSON includes `X-API-Key` (merged with OIDC; do not put `Authorization` in the secret).

Example:

```bash
curl -X POST https://your-mcp-proxy.run.app/admin/servers \
  -H "X-Admin-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '[{
    "id": "quickbooks-mcp",
    "url": "https://your-quickbooks-mcp-url/mcp-server/mcp",
    "upstream_auth": "cloud_run_iam",
    "credentials_header": "X-API-Key: <quickbooks-mcp-api-key>",
    "enabled": true
  }]'
```

For **local** QuickBooks MCP over HTTP with **no** Cloud Run IAM, **`headers`** and `credentials_header` only is enough.
