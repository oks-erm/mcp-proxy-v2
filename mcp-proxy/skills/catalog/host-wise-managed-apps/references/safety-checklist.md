# Safety Checklist

Before deploy:

- The request is clear enough to build the right app.
- App source lives under a repo app folder.
- The app uses a supported v1 framework.
- Frontend has no credentials, secret IDs, raw SQL, BigQuery clients, or production DB clients.
- App data is app-owned SQLite and/or user-uploaded files.
- `/health` or `/api/health` works locally.
- The main user flow has been smoke tested.
- The production frontend build renders non-empty UI locally when applicable.
- `summary` explains what the app does.
- `validate_deployment_manifest` passes.
- `plan_deployment` output has been reviewed.

After deploy:

- Tell the user the app is deployed with Google sign-in (IAP) on the Run URL, and is listed in MCP Proxy Apps.
- Admins use MCP Proxy Apps to delete obsolete apps (full cleanup) when needed.
- Do not run ad hoc cleanup commands outside the platform.
