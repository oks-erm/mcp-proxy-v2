# Cloud Run Deployer MCP

Security-first MCP server for deploying Codex-built Host Wise web apps to Google Cloud Run.

The server is deliberately conservative:

- A successful `deploy_app` first deploys with private ingress, then applies the same IAP steps as the old separate approval phase: the default Run URL requires **Google sign-in** (IAP; **`domain:hostwise.pt`** by default for principals). **Unauthenticated** public access to the service is not enabled.
- `approve_app` is still available to re-apply IAP if a service was changed by hand. MCP Proxy lists apps for metadata and **delete** when obsolete.
- Public unauthenticated access is rejected by policy.
- Each app gets its own runtime service account by default.
- App-owned SQLite and user uploads are preferred; generated apps should not connect directly to BigQuery or production databases.
- Actual mutation is disabled unless `CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED=true`.

## Recommended Architecture

Do not put every generated app into one shared container. Use one hardened deployment contract and one Cloud Run service per app.

The single-container approach looks simpler, but it creates a large blast radius: one app can affect every other app, data permissions become shared, and rollbacks become awkward. The better pattern is:

1. Codex builds or receives an app artifact.
2. The deployer MCP validates a deployment manifest.
3. Cloud Build builds the image when needed.
4. Cloud Run deploys, then the pipeline enables IAP for the app URL.
5. MCP Proxy records the app; admins use the Apps page mainly to **delete** services when no longer needed.

```text
User browser -> Host Wise auth / IAP -> Cloud Run app
Cloud Run app -> app-owned SQLite/uploads by default
```

## Managed Apps in MCP Proxy (web UI)

The **Managed apps** page in `mcp-proxy` reads from Firestore. A row is created only after a **successful Cloud Run service deploy** is completed through the proxy (aggregated MCP with upstream `cloud_run_deployer`), so the proxy can run `upsert_managed_app` after the deployer returns.

The intended flow is production-only:

1. `deploy_app(..., execute=true)` runs build, deploy, and IAP in one go.
2. MCP Proxy upserts a **live** (`approved`) record with the Run URL; use **Apps** to **delete** when a service is obsolete.
3. If IAP was changed outside the deployer, an admin can still call `approve_app` to realign.

Connecting straight to this server’s Cloud Run URL (bypassing the proxy) can deploy the app in GCP but **will not** add it to the Apps UI. Fix: call the final deploy/complete step through the **proxy** MCP URL (same manifest / `app_id` is fine; the record will upsert).

Non-admin users see only apps in **Account → Managed apps** that were created with **their** proxy user key. Admins see all apps under **Admin →** (or dashboard) as configured in your UI.

## Tools

- `validate_deployment_manifest`: validates policy and returns normalized config.
- `plan_deployment`: renders the secure deployment plan and gcloud commands.
- `start_source_upload`: starts a resumable MCP-only source upload session from declared files.
- `upload_source_chunk`: uploads one base64 source chunk for a declared file.
- `finalize_source_upload`: validates uploaded chunks, writes `source.tgz` to GCS, and returns a deploy-ready `gcs_archive` manifest.
- `plan_source_archive`: legacy helper that tells local agents how to package/upload repo folders for remote deploy.
- `deploy_app`: dry-run by default; executes only when `execute=true` and server mutations are enabled.
- `approve_app`: re-apply IAP for an existing service (usually unnecessary; deploy already enables IAP).
- `get_app_status`: read-only Cloud Run status check.
- `get_app_logs`: read-only scoped Cloud Logging lookup.
- App secrets are set through MCP Proxy (`mcp_proxy_set_app_secret`) and stored encrypted in Firestore.
- `delete_app`: dry-run by default; deletes Cloud Run resources only when `execute=true` and server mutations are enabled. MCP Proxy uses full cleanup by default.

The server also exposes guidance resources and prompts:

- `cloud-run-deployer://help/index`
- `cloud-run-deployer://help/agent-deployment` — **MCP-only agents** (no gcloud): use the Host Wise aggregated proxy, upload source with `start_source_upload` / `upload_source_chunk` / `finalize_source_upload`, then deploy the returned `gcs_archive` manifest
- `cloud-run-deployer://help/managed-dashboard-workflow`
- `cloud-run-deployer://templates/dashboard-app`
- `cloud-run-deployer://schemas/deployment-manifest`
- `cloud-run-deployer://schemas/query-registry`
- `cloud-run-deployer://safety/checklist`
- `managed_dashboard_app_builder(task)`
- `managed_dashboard_review_checklist(app_summary, data_access_summary)`
- `deployment_manifest_writer(task, app_id)`

## Manifest

```json
{
  "app_id": "owner-kpis",
  "name": "Owner KPIs",
  "summary": "Shows owner KPI trends for operations review.",
  "framework": "fastapi",
  "source": {
    "type": "gcs_archive",
    "gcs_archive": "gs://it-team-hw-project-cloud-run-deployer-staging/apps/owner-kpis/source.tgz"
  },
  "access": {
    "mode": "iap"
  },
  "runtime": {
    "port": 8080,
    "cpu": "1000m",
    "memory": "512Mi",
    "min_instances": 0,
    "max_instances": 1
  },
  "data": { "sqlite": true, "uploads": true }
}
```

Omit `access.iap_principals` to allow every **@hostwise.pt** Workspace user; set it only to narrow the list.

## MCP-only source upload

Agents without local shell, CI, repo access, or `gcloud` should not use `plan_source_archive`. They should:

1. Call `start_source_upload` with app metadata and declared files. Each file needs `path`, `size_bytes`, `sha256`, and `content_type` (`text` or `binary`).
2. Upload every file as 2 MB raw chunks with `upload_source_chunk`.
3. Call `finalize_source_upload(execute=true)`.
4. Use the returned `manifest` with `validate_deployment_manifest`, `plan_deployment`, and `deploy_app`.

Upload policy excludes `node_modules`, `.git`, build outputs, caches, virtualenvs, local env files, and credential-looking files. Source must include a `Dockerfile` and at least one build marker (`package.json`, `requirements.txt`, `pyproject.toml`, or `hostwise.app.json`).

## Data Access Policy

Default stance: apps do not get direct broad access to production systems.

Generated apps should use app-owned SQLite and uploaded CSV/XLSX/JSON files by default. Do not expose BigQuery credentials, production DB credentials, secret names, raw SQL, or internal table names to frontend code.

## Local Setup

```bash
cd global/mcp/cloud-run-deployer-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ENV=local
export API_KEY=local-dev-key
export GCP_PROJECT_ID=it-team-hw-project
# Optional: overrides the in-code default (domain:hostwise.pt) for local runs
# export CLOUD_RUN_DEPLOYER_DEFAULT_IAP_PRINCIPALS="user:you@hostwise.pt"

python main.py
```

MCP endpoint: `http://localhost:8080/mcp-server/mcp` with `X-API-Key: local-dev-key`.

## Deploy

Create the deployer MCP API key:

```bash
KEY=$(openssl rand -hex 32)
gcloud secrets create cloud-run-deployer-mcp-api-key \
  --project=it-team-hw-project \
  --replication-policy=automatic
echo -n "$KEY" | gcloud secrets versions add cloud-run-deployer-mcp-api-key \
  --project=it-team-hw-project \
  --data-file=-
```

Deploy locked behind Cloud Run IAM:

```bash
gcloud run deploy cloud-run-deployer-mcp \
  --source global/mcp/cloud-run-deployer-mcp \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --service-account=665134555831-compute@developer.gserviceaccount.com \
  --set-env-vars="GCP_PROJECT_ID=it-team-hw-project,API_KEY_SECRET_ID=cloud-run-deployer-mcp-api-key,CLOUD_RUN_DEPLOYER_DEFAULT_IAP_PRINCIPALS=domain:hostwise.pt,CLOUD_RUN_DEPLOYER_DASHBOARD_RUNTIME_SERVICE_ACCOUNT=dashboard-runtime-sa@it-team-hw-project.iam.gserviceaccount.com,CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED=true,CLOUD_RUN_DEPLOYER_GCS_SOURCE_STAGING_DIR=gs://it-team-hw-project-cloud-run-deployer-staging/source,CLOUD_RUN_DEPLOYER_SOURCE_ARCHIVE_BUCKET=it-team-hw-project-cloud-run-deployer-staging" \
  --no-allow-unauthenticated
```

Grant `mcp-proxy-sa` invoker:

```bash
gcloud run services add-iam-policy-binding cloud-run-deployer-mcp \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --member=serviceAccount:mcp-proxy-sa@it-team-hw-project.iam.gserviceaccount.com \
  --role=roles/run.invoker
```

Register in mcp-proxy with id `cloud_run_deployer` (hyphenated `cloud-run-deployer` is also supported), `upstream_auth: cloud_run_iam`, and the deployer API key as `X-API-Key`. The id matters because `deploy_app` and `delete_app` are classified as write tools in mcp-proxy permissions and for Managed Apps Firestore sync.

## Enabling Execution

Leave execution disabled until the deployer service account has the exact IAM you want and the mcp-proxy write permission is configured.

When ready:

```bash
gcloud run services update cloud-run-deployer-mcp \
  --project=it-team-hw-project \
  --region=europe-west1 \
  --set-env-vars=CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED=true
```

The deployer runtime service account will need a narrow control-plane role set, typically:

- Cloud Run Admin for managed app services.
- Service Account Admin or a pre-created service account pool strategy.
- Service Account User on app runtime service accounts.
- IAP Admin for Cloud Run IAP access bindings.
- Cloud Build Editor if building from GCS archives.
- Artifact Registry Writer for app images.

Prefer a custom role and project/folder scoping once the final deployment surface is stable.
