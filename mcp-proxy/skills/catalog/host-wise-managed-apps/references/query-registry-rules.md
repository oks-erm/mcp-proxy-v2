# App-Owned Data Rules

V1 managed web apps should use app-owned data.

Allowed defaults:

- SQLite database owned by the app.
- CSV, XLSX, or JSON files imported by the app.
- App secrets stored through `mcp_proxy_set_app_secret`; MCP Proxy keeps values encrypted in Firestore.

Hard rules:

- Do not connect generated apps directly to BigQuery.
- Do not connect generated apps directly to Host Wise production databases.
- Do not commit uploaded data files that contain sensitive information.
- Do not expose secret names, secret values, credentials, raw SQL, or internal table names in frontend code.
- Keep SQLite apps at one Cloud Run instance unless the data layer is changed.
