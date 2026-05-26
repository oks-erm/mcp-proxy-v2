# Moloni MCP

Read-only MCP server for [Moloni](https://www.moloni.pt/) API v1: **invoices** and **credit notes** via [`invoices/getAll`](https://www.moloni.pt/dev/documents/invoices/getall/), [`invoices/getOne`](https://www.moloni.pt/dev/documents/invoices/getone/), [`creditNotes/getAll`](https://www.moloni.pt/dev/documents/credit-notes/getall/), [`creditNotes/getOne`](https://www.moloni.pt/dev/documents/credit-notes/getone/). Uses the same password grant and JSON POST style as the portal `MoloniService`.

## HTTP / auth

- **MCP transport**: Streamable HTTP mounted at **`/mcp-server`** (client path is typically **`/mcp-server/mcp`**).
- **Ingress**: **`X-API-Key`** matching Secret Manager secret **`moloni-mcp-api-key`** (override with `API_KEY_SECRET_ID`), **or** verified **`Authorization: Bearer`** Google ID token for this host (audience `https://<Host>/`) with `email` in `ALLOWED_INVOKER_SA_EMAILS` (default `mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com`).

Create **`moloni-mcp-sa@it-team-hw-project.iam.gserviceaccount.com`** with **Secret Manager Secret Accessor** on **`moloni-mcp-api-key`** and **`moloni-credentials`** (or the secret id you set in `MOLONI_CREDENTIALS_SECRET_ID`) before first deploy.

### Credentials secret shape

The Moloni credentials secret is JSON (same as the portal), either:

- `{ "creds": { "developer_id", "client_secret", "username", "password", "company_id", ... } }`, or
- a flat object with those keys.

`company_id` is required for all tools.

## Tools

| Tool                       | Purpose                                                           |
| -------------------------- | ----------------------------------------------------------------- |
| `moloni_list_invoices`     | Paginated invoices — `offset`, `limit` (max 50), optional filters |
| `moloni_get_invoice`       | One invoice by Moloni `document_id`                               |
| `moloni_list_credit_notes` | Paginated credit notes — same pagination/filters                  |
| `moloni_get_credit_note`   | One credit note by Moloni `document_id`                           |

## Local development

```bash
cd global/mcp/moloni-mcp
export ENV=local
export API_KEY=dev-key
export MOLONI_CREDENTIALS_JSON='{"developer_id":"...","client_secret":"...","username":"...","password":"...","company_id":123}'
pip install -r requirements.txt
python main.py
```

## Firestore / mcp-proxy

Add an **`mcp_servers`** document with id **`moloni`**, **enabled**, URL **`${SERVICE_URL}/mcp-server/mcp`**, and upstream headers your proxy expects (typically `X-API-Key` matching **`moloni-mcp-api-key`**).

## Layout

```
moloni-mcp/
  main.py
  mcp_server.py
  moloni_client.py
  mcp_platform/   # synced from ../mcp_platform via scripts/sync_mcp_platform.py
  pyproject.toml
  requirements.txt
  README.md
```
