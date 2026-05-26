# Firestore Gateway API

API Gateway for querying Firestore collections with API key authentication, filtering, ordering, and pagination support.

## Security

- This gateway is **not** a secrets vault reader. Collections that hold credentials (by default **`tokens`**, **`n8n-tokens`**) are **blocked** for `GET /documents/…`, `POST /query`, and MCP `query_documents` / `get_document` / `count_documents`.
- Override the deny list with **`FIRESTORE_GATEWAY_DENIED_COLLECTIONS`** (comma-separated collection IDs). Default: `tokens,n8n-tokens`.
- Optional shell-style globs: **`FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS`** (comma-separated, `fnmatch` per collection id). Default: `tmp_*,*_migration,*_webhooks,monitoring_*`. These hide ephemeral and internal collections from `list_collections` and block reads.
- Responses **redact** common secret-carrying field names recursively (e.g. `token`, `password`, `api_key`, keys ending with `_token` / `_secret`). Values are replaced with `<redacted>`.
- **Agents (MCP):** `list_collections` **omits** deny-listed (and pattern-denied) ids so discovery matches read policy. Redaction is **not** full anonymization—documents may still hold **business PII** in other fields.

## Features

- **API Key Authentication**: Secure access using API keys stored in Google Cloud Secret Manager
- **Query Endpoint**: POST method for querying Firestore collections with structured JSON requests
- **Filtering**: Support for multiple filters with various operators (==, !=, <, <=, >, >=, in, not-in, array-contains)
- **Ordering**: Sort results by any field in ascending or descending order
- **Pagination**: Cursor-based pagination for efficient data retrieval
- **Swagger Documentation**: Auto-generated API documentation at `/docs`

## Setup

### Prerequisites

- Python 3.13+
- Google Cloud Project with Firestore and Secret Manager enabled
- Service account with appropriate permissions:
  - Firestore read access
  - Secret Manager access to `firestore-gateway-api-key`

### Installation

```bash
poetry install
```

### Configuration

The API key is automatically loaded from Google Cloud Secret Manager:

- Secret name: `firestore-gateway-api-key`
- Project ID: Set via `GOOGLE_CLOUD_PROJECT` environment variable (defaults to `it-team-hw-project`)
- Database: Set via `GOOGLE_CLOUD_DATABASE` environment variable (defaults to `data-warehouse-firestore`)

### Running

```bash
poetry run python main.py
```

Or with uvicorn:

```bash
poetry run uvicorn main:app --host 0.0.0.0 --port 8000
```

## API Endpoints

### Health Check

```bash
GET /health
```

No authentication required.

### Query Collection

```bash
POST /query
Headers:
  X-API-Key: your-api-key
Content-Type: application/json

{
  "collection": "reservations",
  "filters": [
    {"field": "status", "operator": "==", "value": "confirmed"}
  ],
  "order_by": "created_at",
  "order_direction": "DESCENDING",
  "limit": 20,
  "cursor": null
}
```

## Response Format

```json
{
  "documents": [
    {
      "_id": "document-id",
      "field1": "value1",
      "field2": "value2"
    }
  ],
  "total_returned": 20,
  "has_more": true,
  "next_cursor": {
    "id": "last-document-id",
    "order_by_value": "last-value"
  }
}
```

## Pagination

To fetch the next page, include the `cursor` from the previous response:

```json
{
  "collection": "reservations",
  "limit": 20,
  "cursor": {
    "id": "last-document-id",
    "order_by_value": "last-value"
  }
}
```

## Filter Operators

- `==` - Equal to
- `!=` - Not equal to
- `<` - Less than
- `<=` - Less than or equal to
- `>` - Greater than
- `>=` - Greater than or equal to
- `in` - Value is in array
- `not-in` - Value is not in array
- `array-contains` - Array contains value

## Swagger Documentation

Once the server is running, visit:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

The Swagger UI includes an "Authorize" button where you can enter your API key for testing.

## Deployment to Cloud Run

### Prerequisites

1. **Export requirements.txt** (if not already done):

   ```bash
   poetry export -f requirements.txt --output requirements.txt --without-hashes
   ```

2. **Set up gcloud CLI**:

   ```bash
   gcloud auth login
   gcloud config set project it-team-hw-project
   ```

3. **Enable required APIs**:

   ```bash
   gcloud services enable run.googleapis.com
   gcloud services enable secretmanager.googleapis.com
   gcloud services enable firestore.googleapis.com
   ```

4. **Create/verify the API key secret**:

   ```bash
   # Create the secret (if it doesn't exist)
   echo -n "your-api-key-here" | gcloud secrets create firestore-gateway-api-key --data-file=-

   # Or update existing secret
   echo -n "your-api-key-here" | gcloud secrets versions add firestore-gateway-api-key --data-file=-
   ```

5. **Create a service account** (if not exists) with required permissions:

   ```bash
   gcloud iam service-accounts create firestore-gateway-sa \
     --display-name="Firestore Gateway Service Account"

   # Grant Secret Manager access
   gcloud secrets add-iam-policy-binding firestore-gateway-api-key \
     --member="serviceAccount:firestore-gateway-sa@it-team-hw-project.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"

   # Grant Firestore access
   gcloud projects add-iam-policy-binding it-team-hw-project \
     --member="serviceAccount:firestore-gateway-sa@it-team-hw-project.iam.gserviceaccount.com" \
     --role="roles/datastore.user"
   ```

### Deploy to Cloud Run

Deploy using source-based deployment (Cloud Build will automatically detect Python and build the container):

```bash
cd global/firestore-gateway

gcloud run deploy firestore-gateway \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated \
  --service-account firestore-gateway-sa@it-team-hw-project.iam.gserviceaccount.com
```

### Update Existing Service

To update the service with new code:

```bash
cd global/firestore-gateway

gcloud run deploy firestore-gateway \
  --source . \
  --region europe-west1 \
  --set-env-vars GOOGLE_CLOUD_PROJECT=it-team-hw-project,GOOGLE_CLOUD_DATABASE=data-warehouse-firestore,ENV=production
```

### Environment Variables

The service uses the following environment variables (set via `--set-env-vars`):

- `GOOGLE_CLOUD_PROJECT`: GCP project ID (default: `it-team-hw-project`)
- `GOOGLE_CLOUD_DATABASE`: Firestore database name (default: `data-warehouse-firestore`)
- `ENV`: Environment name (`local` for local dev, `production` for deployed)
- `FIRESTORE_GATEWAY_DENIED_COLLECTIONS`: Comma-separated collection names to block entirely (default: `tokens`)

### Service URL

After deployment, Cloud Run will provide a URL like:

```
https://firestore-gateway-<project-number>.europe-west1.run.app
```

You can get the URL with:

```bash
gcloud run services describe firestore-gateway \
  --region europe-west1 \
  --format 'value(status.url)'
```

### Testing the Deployed Service

```bash
# Health check
curl https://firestore-gateway-<project-number>.europe-west1.run.app/health

# Query endpoint (use an operational collection — `tokens` is denied by default)
curl -X POST https://firestore-gateway-<project-number>.europe-west1.run.app/query \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "collection": "your_collection",
    "limit": 10
  }'
```
