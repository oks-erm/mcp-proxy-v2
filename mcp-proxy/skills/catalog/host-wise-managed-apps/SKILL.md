---
name: host-wise-managed-apps
description: Build, test, and deploy simple Host Wise managed web apps through the cloud-run-deployer MCP. Use when Codex needs to create repo-backed Next.js, React/Vite, Node/Express, or FastAPI apps, deploy to production (IAP by default), and use MCP Proxy Apps for metadata and cleanup.
---

# Host Wise Managed Apps

Use this skill when creating or changing a simple managed web app for Host Wise users.

Read these references before implementation:

- [references/dashboard-app-structure.md](references/dashboard-app-structure.md)
- [references/query-registry-rules.md](references/query-registry-rules.md)
- [references/deployment-flow.md](references/deployment-flow.md)
- [references/safety-checklist.md](references/safety-checklist.md)

## Start With Questions

Clarify the app before writing code:

1. Who will use it?
2. What workflow should it support?
3. Does it need app-owned SQLite, uploads, or secrets?
4. Which CSV/XLSX/JSON imports are expected?
5. Who should be able to access it (Host Wise Google accounts via IAP by default)?

## Build Rules

- Create the app under a repo app folder such as `apps/<app-id>`.
- Use one supported v1 framework: Next.js, React/Vite, Node/Express, or Python/FastAPI.
- Keep frontend calls same-origin where a backend exists.
- Do not expose secrets, BigQuery credentials, raw SQL, production DB credentials, or internal system details in frontend code.
- Treat Host Wise Google auth as platform-owned; successful deploy enables IAP on the Run URL.

## Data Rules

- Prefer app-owned SQLite for v1.
- Support user imports through CSV, XLSX, or JSON when the app needs data.
- Do not connect generated apps directly to BigQuery or Host Wise production databases.
- Keep Cloud Run max instances at 1 for SQLite apps.

## Local Checks

Before deploying:

- Run frontend build/lint checks available in the app.
- Run backend import/test checks available in the app.
- Smoke test `/api/health`.
- Smoke test the main user flow.
- Confirm no frontend files contain credentials, raw SQL, secret IDs, or direct BigQuery imports.

## MCP Deployment Order

Use the Cloud Run deployer MCP in this order:

1. Read `cloud-run-deployer://help/index`.
2. Read `cloud-run-deployer://help/managed-dashboard-workflow`.
3. Read `cloud-run-deployer://templates/dashboard-app`.
4. Read `cloud-run-deployer://schemas/deployment-manifest`.
5. Read `cloud-run-deployer://schemas/query-registry`.
6. Read `cloud-run-deployer://safety/checklist`.
7. Get prompt `managed_dashboard_app_builder(task)` if building from scratch.
8. Get prompt `deployment_manifest_writer(task, app_id)` when writing the manifest.
9. Call `plan_source_archive(app_id, source_path)` and run the returned local packaging/upload commands.
10. Use the printed GCS URI as `source.type="gcs_archive"` in the deploy manifest, not a local-only repo path.
11. Call `validate_deployment_manifest`.
12. Call `plan_deployment`.
13. Call `deploy_app` with `execute=true` after local smoke checks pass.
14. The app deploys to production with Google sign-in (IAP) and is listed in MCP Proxy Apps.

## Summaries

Every deployment must include:

- `summary`: one useful sentence explaining what the app does and who it helps.

## Governance

Apps deploy live to Cloud Run; the deploy pipeline enables the URL with Google sign-in (IAP). MCP Proxy records the app for operators: admins can delete (full cleanup) when an app is obsolete; creators see their own rows.
Deleting from the MCP Proxy Apps UI calls the deployer MCP `delete_app` tool with full cleanup by default.
