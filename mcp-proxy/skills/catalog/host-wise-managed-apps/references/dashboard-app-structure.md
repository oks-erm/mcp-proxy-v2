# Web App Structure

Managed web apps live in repo app folders, usually `apps/<app-id>`.

Recommended structure:

```text
apps/<app-id>/
  Dockerfile
  hostwise.app.json
  package.json | pyproject.toml | requirements.txt
  src/
  data/
    schema.sql
    importers/
```

Runtime responsibilities:

- Serve `/health` or `/api/health`.
- Listen on the Cloud Run `$PORT`, defaulting to `8080`.
- Use app-owned SQLite and uploaded CSV/XLSX/JSON files for simple data.
- Keep Cloud Run max instances at 1 when SQLite is writable.
- Never include secrets, BigQuery credentials, production DB credentials, or raw internal system exports in frontend code.

Supported v1 frameworks:

- Next.js
- React/Vite
- Node/Express
- Python/FastAPI
