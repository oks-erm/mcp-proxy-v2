"""Security-first MCP tools for deploying Host Wise internal web apps to Cloud Run."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.request
from typing import Any

from deploy_policy import (
    app_metadata,
    build_plan,
    render_approve_commands,
    render_build_commands,
    render_commands,
    render_delete_commands,
    render_deploy_commands,
    render_shell,
    validate_manifest,
)
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.envelope import tool_error
from mcp_platform.meta import with_response_meta
from mcp_platform.transport import structured_result
from source_upload import finalize_upload_session, start_upload_session, upload_chunk

logger = logging.getLogger(__name__)

MUTATIONS_ENABLED = os.getenv("CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED", "").lower() in {"1", "true", "yes"}
ALLOW_LOCAL_SOURCE_PATHS = os.getenv("CLOUD_RUN_DEPLOYER_ALLOW_LOCAL_SOURCE_PATHS", "").lower() in {
    "1",
    "true",
    "yes",
}
COMMAND_TIMEOUT_SECONDS = int(os.getenv("CLOUD_RUN_DEPLOYER_COMMAND_TIMEOUT_SECONDS", "1800"))
BUILD_ID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# Cloud Run service URLs as printed by `gcloud run deploy` and shown in `status.url`.
RUN_APP_URL_RE = re.compile(r"https://[a-z0-9\-.]+\.run\.app/?", re.I)

mcp = FastMCP(
    "cloud-run-deployer",
    instructions=(
        "Deploy simple Host Wise internal web apps to Cloud Run. A successful deploy enables Google sign-in (IAP) "
        "on the service URL. MCP Proxy records the app for metadata and delete/cleanup. "
        "Do not expose raw GCP, Git, Docker, or shell details to end users. Prefer app-owned SQLite "
        "and uploads; do not connect generated apps directly to BigQuery or production databases. "
        "start_source_upload, upload_source_chunk, finalize_source_upload, deploy_app, approve_app, "
        "and delete_app are write tools. App secrets are owned by MCP Proxy."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


MANAGED_DASHBOARD_TEMPLATE = """# Host Wise Web App Template

Recommended app layout:

```text
apps/<app-id>/
  Dockerfile
  hostwise.app.json
  package.json | pyproject.toml | requirements.txt
  src/...
  data/
    schema.sql
    importers/
```

Runtime contract:
- Supported v1 frameworks: Next.js, React/Vite, Node/Express, Python/FastAPI.
- Apps deploy to production with IAP; MCP Proxy lists them for operators to delete when obsolete.
- Use app-owned SQLite and uploaded files for simple data.
- Do not put secrets, BigQuery credentials, raw production DB credentials, or raw internal data exports in frontend code.
"""


QUERY_REGISTRY_SCHEMA = """# App-Owned Data Rules

V1 web apps use app-owned SQLite and user uploads by default.

Rules:
- Do not connect generated apps directly to BigQuery or Host Wise production databases.
- File uploads should be CSV, XLSX, or JSON and imported into the app-owned SQLite database.
- Keep Cloud Run max instances at 1 for SQLite apps.
- Store app secrets through MCP Proxy encrypted Firestore secret tools only.
"""


DEPLOYMENT_MANIFEST_SCHEMA = """# Deployment Manifest

Simple production-only web app manifest:

```json
{
  "app_id": "owner-dashboard",
  "name": "Owner Dashboard",
  "summary": "Shows monthly owner KPIs and reservation health.",
  "framework": "fastapi",
  "source": {"type": "gcs_archive", "gcs_archive": "gs://example-staging/apps/owner-dashboard/source.tgz"},
  "data": {"sqlite": true, "uploads": true},
  "access": {"mode": "iap"}
}
```

MCP-only agents can create the GCS source archive without local shell or gcloud by calling
`start_source_upload`, `upload_source_chunk` for every declared chunk, and `finalize_source_upload`.
The finalization response includes this same `source.type="gcs_archive"` shape plus a deploy-ready
manifest draft.

`source.type="dashboard_app"` with `source.path` is only valid when that path exists inside the deployer runtime.

`deploy_app` validates this manifest and deploys one production Cloud Run service, then applies the same
IAP steps so the URL requires Google sign-in (Host Wise principals for `domain:hostwise.pt` by default).
"""


AGENT_DEPLOYMENT_GUIDE = """# Agent-only deployment (MCP only, no gcloud, no local repo)

This is the **supported contract** for autonomous agents that only have **MCP tool access** (no shell, no `gcloud`, no direct file reads of a developer laptop).

## Use the Host Wise MCP Proxy

- Call tools on the **aggregated MCP Proxy** (same base URL and API key as the rest of Host Wise MCP), with the **prefixed** tool name `cloud_run_deployer_*`.
- **Do not** call the raw Cloud Run deployer URL as the primary path: **Managed Apps** in the Proxy UI and Firestore sync expect traffic **through the proxy** so the proxy can attribute the user and run `upsert_managed_app` after successful writes.

## Source: upload through MCP

| `source.type` | What the agent does |
|---------------|---------------------|
| MCP source upload | Call `start_source_upload`, send base64 chunks with `upload_source_chunk`, then call `finalize_source_upload`; the deployer writes `source.tgz` to GCS and returns a deploy-ready `gcs_archive` manifest. |
| `gcs_archive` | Put `source.gcs_archive` to an existing `gs://...` object when one already exists. |
| `image` | Set `source.image` to a container **already pushed** to Artifact Registry (e.g. by CI). No Cloud Build in the deployer is required for that path. |
| `dashboard_app` + `source.path` | Only valid on a deployer host that has that path; **remote agents** should use `gcs_archive` or `image`. |

## Tool order (typical)

1. Read `cloud-run-deployer://help/index` and this page; read manifest + safety resources.
2. `start_source_upload` with declared files, then `upload_source_chunk` for every chunk.
3. `finalize_source_upload(execute=true)` to create the GCS archive and manifest.
4. `validate_deployment_manifest` → `plan_deployment` (review output).
5. `deploy_app` with `execute=true`, `summary` / `description` as required by policy.
6. If you use **`async_build=true`**: poll `get_deployment_status` until the build is **SUCCESS**, then `complete_deployment` with the **same manifest** and `build_id`, `execute=true`.
7. `get_app_status` / `get_app_logs` if something fails; `delete_app` to tear down when asked.

**Sync deploy** (`async_build=false`) runs build + deploy + IAP in one pipeline (long request; your MCP client may need a generous timeout).

## Auth and “client context” errors

The deployer HTTP server accepts **(a)** a shared **`X-API-Key`** when configured, or **(b)** **OIDC** from an allowlisted caller (e.g. the MCP Proxy runtime service account). If `complete_deployment` or other writes fail with an authorization or **client context** error, the fix is in **connectivity and identity**: use the **Proxy** MCP entrypoint the product documents, and ensure the deployer’s **`ALLOWED_INVOKER_SA_EMAILS`** and Proxy **upstream IAM** match your deployment. Agents cannot “fix” GCP IAM from inside MCP tools alone.

## Mutations

Write tools only run if **`CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED=true`** on the deployer service.
"""


SAFETY_CHECKLIST = """# Host Wise Web App Safety Checklist

- Read `cloud-run-deployer://help/index`.
- Build from `cloud-run-deployer://templates/dashboard-app`.
- Keep BigQuery credentials, production DB credentials, raw SQL, and secret names out of frontend code.
- Prefer app-owned SQLite and uploaded files.
- For MCP-only agents, upload source with `start_source_upload`, `upload_source_chunk`, and `finalize_source_upload`.
- Exclude node_modules, build outputs, local env files, credentials, caches, and virtualenvs from uploaded source.
- Call `validate_deployment_manifest`, then `plan_deployment`.
- Call `deploy_app` for a production deploy.
- Managed apps are listed in MCP Proxy for metadata and cleanup; deploy already enables Google sign-in (IAP).
"""


@mcp.resource("cloud-run-deployer://help/index")
def help_index() -> str:
    """Overview of the Cloud Run deployer MCP workflow."""
    return """# Cloud Run Deployer MCP

This server deploys and deletes simple Host Wise internal web apps on Google Cloud Run.

Required workflow:
1. Build or receive a supported web app: Next.js, React/Vite, Node/Express, or Python/FastAPI.
2. Use app-owned SQLite and uploads for simple data.
3. Run local smoke checks.
4. Call `validate_deployment_manifest`, then `plan_deployment`.
5. Call `deploy_app` for a production deploy (enables Google sign-in / IAP on the service URL when `execute=true`).
6. The app is recorded in MCP Proxy; use the Apps page to review or delete when no longer needed.

Write tools:
- `deploy_app`: deploys a production app and applies IAP so the URL requires Google sign-in.
- `approve_app`: re-apply IAP if the service was changed manually (rare).
- App secrets are stored by MCP Proxy in encrypted Firestore, not by this deployer.
- `delete_app`: deletes Cloud Run resources when `execute=true` and mutations are enabled.

Read-only status tool:
- `get_app_status` and `get_deployment_status`: check Cloud Build and Cloud Run state without mutating GCP.
- `get_app_logs`: reads scoped Cloud Logging entries.

**Managed Apps UI (MCP Proxy):** The **Apps** list in the proxy web UI is updated only when `deploy_app` runs **through the MCP proxy** (server id `cloud_run_deployer` on the **aggregated** proxy endpoint), not when Cursor or another client calls this deployer’s URL **directly**. Use the same API key and proxy `MCP` URL you use for other tools. Use `execute=true` for a live deploy; dry runs do not create a record.

If you already deployed directly, run `deploy_app` again with the same `app_id` and `execute=true` **via the proxy** to create or refresh the Firestore record.

**Autonomous agents (MCP only):** Read `cloud-run-deployer://help/agent-deployment` for the full contract (source via `gcs_archive` or `image`, async vs sync, no local shell).
"""


@mcp.resource("cloud-run-deployer://help/managed-dashboard-workflow")
def managed_dashboard_workflow() -> str:
    """Detailed web app workflow for agents."""
    return """# Host Wise Web App Workflow

Clarify:
- Who will use the app?
- What simple workflow should it support?
- Does it need app-owned SQLite, uploads, or secrets?

Build:
- Supported frameworks: Next.js, React/Vite, Node/Express, Python/FastAPI.
- Keep data app-owned by default: SQLite plus uploads.
- Do not connect directly to BigQuery or Host Wise production databases.

Test:
- Run backend and frontend checks locally.
- Smoke test `/api/health`.
- Smoke test the app with representative uploaded data if imports are used.

Deploy:
- Include a clear purpose `summary`.
- Call `plan_source_archive` for local repo app folders, run its returned commands, then use
  `source.type="gcs_archive"` for remote deploys.
- Call `deploy_app` with `execute=true`.
- The production deploy enables the default Run URL with Google sign-in (IAP). Use MCP Proxy Apps to remove apps you no longer need.
- Do not deploy with ad hoc shell commands.
"""


@mcp.resource("cloud-run-deployer://templates/dashboard-app")
def dashboard_app_template() -> str:
    """Standard FastAPI + React dashboard app structure."""
    return MANAGED_DASHBOARD_TEMPLATE


@mcp.resource("cloud-run-deployer://schemas/deployment-manifest")
def deployment_manifest_schema() -> str:
    """Deployment manifest schema and example."""
    return DEPLOYMENT_MANIFEST_SCHEMA


@mcp.resource("cloud-run-deployer://schemas/query-registry")
def query_registry_schema() -> str:
    """App-owned data rules."""
    return QUERY_REGISTRY_SCHEMA


@mcp.resource("cloud-run-deployer://safety/checklist")
def safety_checklist() -> str:
    """Pre-deploy safety checklist for Host Wise web apps."""
    return SAFETY_CHECKLIST


@mcp.resource("cloud-run-deployer://help/agent-deployment")
def agent_deployment_guide() -> str:
    """MCP-only agent contract: no gcloud, source via GCS or image, Proxy entrypoint."""
    return AGENT_DEPLOYMENT_GUIDE


@mcp.prompt()
def managed_dashboard_app_builder(task: str) -> str:
    """Prompt for building a managed Host Wise web app."""
    return (
        "Build a simple Host Wise managed web app for the task below.\n\n"
        "First read these MCP resources: cloud-run-deployer://help/index, "
        "cloud-run-deployer://help/agent-deployment, "
        "cloud-run-deployer://help/managed-dashboard-workflow, "
        "cloud-run-deployer://templates/dashboard-app, and cloud-run-deployer://safety/checklist.\n\n"
        "Rules: use a supported framework, keep data app-owned with SQLite/uploads by default, "
        "no BigQuery or production DB credentials in generated code, upload source through "
        "start_source_upload/upload_source_chunk/finalize_source_upload when you only have MCP access, "
        "and deploy through validate_deployment_manifest, plan_deployment, and deploy_app.\n\n"
        f"Task:\n{task}"
    )


@mcp.prompt()
def managed_dashboard_review_checklist(app_summary: str, data_access_summary: str) -> str:
    """Prompt for reviewing a managed web app before approval."""
    return (
        "Review this managed web app for approval readiness.\n\n"
        f"Purpose summary:\n{app_summary}\n\n"
        f"Data access summary:\n{data_access_summary}\n\n"
        "Check that it has no direct BigQuery or production database access, secrets are not in source, "
        "the purpose is clear enough for MCP Proxy admins, and approval/delete expectations are documented."
    )


@mcp.prompt()
def deployment_manifest_writer(task: str, app_id: str) -> str:
    """Prompt for writing a safe deployment manifest."""
    return (
        "Write a Cloud Run deployer manifest for a simple Host Wise managed web app.\n\n"
        "Read cloud-run-deployer://schemas/deployment-manifest and "
        "cloud-run-deployer://schemas/query-registry first. The manifest must include summary, "
        "a supported framework, app-owned SQLite/upload data settings, a GCS source archive, and IAP access. "
        "For MCP-only agents, call start_source_upload, upload_source_chunk, and finalize_source_upload, "
        "then use the returned source.type='gcs_archive' manifest; "
        "do not point the remote deployer at a local-only repo path. Deploy directly to production with "
        "deploy_app(execute=true); the deploy enables IAP and is recorded in MCP Proxy.\n\n"
        f"app_id: {app_id}\n"
        f"Task:\n{task}"
    )


def _validation_error(details: str, *, errors: list[str] | None = None) -> CallToolResult:
    return structured_result(
        tool_error(
            "validation_error",
            details=details,
            cause="validation",
            retryable=False,
            errors=errors,
        )
    )


def _source_upload_result(result, *, tool: str) -> CallToolResult:
    if result.ok:
        return structured_result(with_response_meta(result.payload, tool=tool))
    payload = result.payload
    error_code = payload.get("error")
    cause = (
        "not_found"
        if error_code == "not_found"
        else "validation" if error_code == "validation_error" else "upstream_error"
    )
    return structured_result(
        tool_error(
            error_code or "source_upload_error",
            details=payload.get("details") or "source upload failed",
            cause=cause,
            retryable=error_code not in {"validation_error", "not_found"},
            errors=payload.get("errors"),
            **{k: v for k, v in payload.items() if k not in {"error", "details", "errors"}},
        )
    )


def _default_source_bucket(project_id: str) -> str:
    return os.getenv("CLOUD_RUN_DEPLOYER_SOURCE_ARCHIVE_BUCKET", f"{project_id}-cloud-run-deployer-staging")


def _run_commands(commands: list[list[str]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    command_results = []
    for command in commands:
        shell_line = " ".join(command)
        logger.info("Executing deployer command: %s", shell_line)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        command_results.append(
            {
                "command": render_shell([command])[0],
                "returncode": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
        )
        if completed.returncode != 0:
            command_results.append({"diagnostics": runtime_diagnostics()})
            return command_results, {
                "error": "deployment_command_failed",
                "details": f"Command failed with return code {completed.returncode}",
            }
    return command_results, None


def _run_probe(command: list[str], *, timeout: int = 30) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "command": render_shell([command])[0],
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def _extract_build_id(command_results: list[dict[str, Any]]) -> str | None:
    for result in reversed(command_results):
        text = "\n".join([str(result.get("stdout") or ""), str(result.get("stderr") or "")])
        try:
            parsed = json.loads(str(result.get("stdout") or "{}"))
        except Exception:
            parsed = {}
        candidates = [
            parsed.get("id") if isinstance(parsed, dict) else None,
            parsed.get("name") if isinstance(parsed, dict) else None,
            parsed.get("metadata", {}).get("build", {}).get("id") if isinstance(parsed, dict) else None,
            parsed.get("metadata", {}).get("build", {}).get("build_id") if isinstance(parsed, dict) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                match = BUILD_ID_RE.search(candidate)
                return match.group(0) if match else candidate.strip()
        match = BUILD_ID_RE.search(text)
        if match:
            return match.group(0)
    return None


def _describe_build(*, build_id: str, project_id: str, region: str) -> dict[str, Any]:
    result = _run_probe(
        [
            "gcloud",
            "builds",
            "describe",
            build_id,
            "--project",
            project_id,
            "--region",
            region,
            "--format",
            "json",
        ],
        timeout=60,
    )
    parsed: dict[str, Any] = {}
    if result["returncode"] == 0:
        try:
            parsed = json.loads(result.get("stdout") or "{}")
        except Exception:
            parsed = {}
    return {
        "ok": result["returncode"] == 0,
        "build_id": build_id,
        "status": parsed.get("status"),
        "log_url": parsed.get("logUrl"),
        "create_time": parsed.get("createTime"),
        "finish_time": parsed.get("finishTime"),
        "raw": parsed,
        "probe": result,
    }


def _describe_cloud_run_service(*, service_name: str, project_id: str, region: str) -> dict[str, Any]:
    if not service_name:
        return {"ok": False, "status": "not_requested"}
    result = _run_probe(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            service_name,
            "--project",
            project_id,
            "--region",
            region,
            "--format",
            "json",
        ],
        timeout=60,
    )
    parsed: dict[str, Any] = {}
    if result["returncode"] == 0:
        try:
            parsed = json.loads(result.get("stdout") or "{}")
        except Exception:
            parsed = {}
    return {
        "ok": result["returncode"] == 0,
        "service_name": service_name,
        "url": (parsed.get("status") or {}).get("url"),
        "conditions": (parsed.get("status") or {}).get("conditions") or [],
        "latest_ready_revision": (parsed.get("status") or {}).get("latestReadyRevisionName"),
        "probe": result,
    }


def _cloud_run_deploy_completed(command_results: list[dict[str, Any]], service_name: str) -> bool:
    for result in command_results:
        command = str(result.get("command") or "")
        if result.get("returncode") == 0 and "gcloud run deploy" in command and service_name in command:
            return True
    return False


def _extract_service_url_from_command_results(
    command_results: list[dict[str, Any]], *, service_name: str
) -> str | None:
    for result in command_results:
        if result.get("returncode") != 0:
            continue
        cmd = str(result.get("command") or "")
        if "gcloud" not in cmd or "run" not in cmd or "deploy" not in cmd:
            continue
        text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
        matches = RUN_APP_URL_RE.findall(text)
        if not matches:
            continue
        norm = service_name.replace("_", "-")
        for m in reversed(matches):
            low = m.lower()
            if norm in low or service_name in m:
                return m.rstrip("/")
        return matches[-1].rstrip("/")
    return None


def _resolve_service_url(
    *,
    service_name: str,
    project_id: str,
    region: str,
    command_results: list[dict[str, Any]] | None = None,
) -> str | None:
    url = _describe_service_url(service_name=service_name, project_id=project_id, region=region)
    if url:
        return url
    if command_results:
        from_cmd = _extract_service_url_from_command_results(command_results, service_name=service_name)
        if from_cmd:
            return from_cmd
    return None


def _partial_deployment_payload(
    *,
    normalized_manifest: dict[str, Any],
    warnings: list[str],
    command_results: list[dict[str, Any]],
    error: dict[str, Any],
    build_id: str | None = None,
) -> dict[str, Any]:
    service_url = _resolve_service_url(
        service_name=normalized_manifest["service_name"],
        project_id=normalized_manifest["project_id"],
        region=normalized_manifest["region"],
        command_results=command_results,
    )
    app = app_metadata(normalized_manifest, service_url=service_url, approved_url=service_url)
    return {
        "ok": False,
        "executed": True,
        "service_deployed": True,
        "partial_failure": True,
        "service_url": service_url,
        "approved_url": service_url,
        "warnings": warnings,
        "build_id": build_id,
        "normalized_manifest": normalized_manifest,
        "app": app,
        "command_results": command_results,
        "error": error["error"],
        "details": error["details"],
        "cause": "post_deploy_binding_failed",
        "retryable": True,
        "next_steps": [
            "The Cloud Run service deployed, but one or more post-deploy IAM binding commands failed.",
            "Fix the failing IAM permission or binding target, then rerun complete_deployment with execute=true.",
        ],
    }


def _metadata_service_account_email() -> str:
    try:
        request = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8").strip()
    except Exception as exc:
        return f"unavailable: {exc}"


def runtime_diagnostics(project_id: str | None = None, bucket: str | None = None) -> dict[str, Any]:
    project = project_id or os.getenv("GCP_PROJECT_ID", "")
    source_bucket = bucket or (f"{project}_cloudbuild" if project else "")
    probes = {
        "gcloud_auth_list": _run_probe(["gcloud", "auth", "list", "--format", "json"]),
        "gcloud_config_list": _run_probe(["gcloud", "config", "list", "--format", "json"]),
    }
    if project:
        probes["project_describe"] = _run_probe(
            ["gcloud", "projects", "describe", project, "--format", "json(projectId,projectNumber)"]
        )
        probes["cloud_build_sa_roles"] = _run_probe(
            [
                "bash",
                "-lc",
                (
                    f"PROJECT_NUMBER=$(gcloud projects describe {project} --format='value(projectNumber)') && "
                    "gcloud projects get-iam-policy "
                    f"{project} --flatten='bindings[].members' "
                    '"--filter=bindings.members:serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" '
                    "--format='table(bindings.role,bindings.members)'"
                ),
            ]
        )
    if source_bucket:
        probes["cloudbuild_bucket_ls"] = _run_probe(["gcloud", "storage", "ls", f"gs://{source_bucket}"])
        probes["cloudbuild_bucket_iam"] = _run_probe(
            ["gcloud", "storage", "buckets", "get-iam-policy", f"gs://{source_bucket}", "--format", "json"]
        )
    return {
        "metadata_service_account_email": _metadata_service_account_email(),
        "env": {
            "GCP_PROJECT_ID": os.getenv("GCP_PROJECT_ID", ""),
            "CLOUD_RUN_DEPLOYER_DEFAULT_IAP_PRINCIPALS": os.getenv("CLOUD_RUN_DEPLOYER_DEFAULT_IAP_PRINCIPALS", ""),
            "CLOUD_RUN_DEPLOYER_DASHBOARD_RUNTIME_SERVICE_ACCOUNT": os.getenv(
                "CLOUD_RUN_DEPLOYER_DASHBOARD_RUNTIME_SERVICE_ACCOUNT", ""
            ),
            "CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED": os.getenv("CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED", ""),
        },
        "project_id": project,
        "bucket": source_bucket,
        "probes": probes,
    }


def _describe_service_url(*, service_name: str, project_id: str, region: str) -> str | None:
    completed = subprocess.run(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            service_name,
            "--project",
            project_id,
            "--region",
            region,
            "--format",
            "value(status.url)",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    u = (completed.stdout or "").strip()
    if completed.returncode == 0 and u:
        return u
    if completed.returncode != 0:
        logger.warning(
            "gcloud value(status.url) for %s failed: rc=%s %s",
            service_name,
            completed.returncode,
            (completed.stderr or "")[-500:],
        )
    svc = _describe_cloud_run_service(service_name=service_name, project_id=project_id, region=region)
    u2 = svc.get("url")
    if isinstance(u2, str) and u2.strip():
        return u2.strip()
    return None


def _source_commit_sha(source_path: str) -> str:
    if not source_path or str(source_path).startswith("gs://"):
        return ""
    completed = subprocess.run(
        ["git", "-C", source_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _slugify_app_id(value: str) -> str:
    raw = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower()).strip("-")
    raw = re.sub(r"-+", "-", raw)
    if not raw:
        return ""
    if not raw[0].isalpha():
        raw = f"app-{raw}"
    return raw[:33].rstrip("-")


def _simple_manifest(
    *,
    manifest: dict[str, Any] | None,
    source_path: str = "",
    app_id: str = "",
    name: str = "",
    description: str = "",
    framework: str = "auto",
    data: dict[str, Any] | None = None,
    secrets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if isinstance(manifest, dict) and manifest:
        return manifest
    app = _slugify_app_id(app_id or name or os.path.basename(str(source_path).rstrip("/")))
    source = (
        {"type": "gcs_archive", "gcs_archive": source_path}
        if str(source_path).startswith("gs://")
        else {"type": "dashboard_app", "path": source_path}
    )
    return {
        "app_id": app,
        "name": name or app,
        "summary": description or f"{name or app} internal web app.",
        "framework": framework or "auto",
        "source": source,
        "data": data or {"sqlite": True, "uploads": True},
        "secrets": secrets or [],
        "access": {"mode": "iap"},
        "runtime": {"max_instances": 1},
        "data_connections": [],
    }


def _app_manifest_from_ids(
    *,
    app_id: str,
    service_name: str = "",
    project_id: str = "",
    region: str = "",
    runtime_service_account: str = "",
) -> dict[str, Any]:
    app = _slugify_app_id(app_id)
    project = project_id or os.getenv("GCP_PROJECT_ID", "it-team-hw-project")
    return {
        "app_id": app,
        "name": app,
        "summary": f"{app} internal web app.",
        "project_id": project,
        "region": region or os.getenv("CLOUD_RUN_DEPLOYER_REGION", "europe-west1"),
        "service_name": service_name or f"app-{app}",
        "runtime_service_account": runtime_service_account,
        "source": {"type": "image", "image": "placeholder"},
        "access": {"mode": "iap"},
        "data_connections": [],
    }


def _run_command_with_input(command: list[str], value: str, *, timeout: int = 120) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        input=value,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "command": render_shell([command])[0],
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


@mcp.tool(structured_output=False)
def validate_deployment_manifest(manifest: dict[str, Any]) -> CallToolResult:
    """Validate a Cloud Run app deployment manifest against Host Wise safety policy.

    Args:
        manifest: Deployment manifest. Required: app_id, source, access, data_connections, summary,
            and data_access_summary. For managed dashboards use source.type="dashboard_app" and source.path.

    Returns:
        A structured validation response with ok, errors, warnings, and normalized_manifest.
    """
    result = validate_manifest(manifest)
    payload = {
        "ok": result.ok,
        "errors": result.errors,
        "warnings": result.warnings,
        "normalized_manifest": result.normalized,
    }
    return structured_result(with_response_meta(payload, tool="cloud_run_deployer_validate_deployment_manifest"))


@mcp.tool(structured_output=False)
def plan_deployment(manifest: dict[str, Any]) -> CallToolResult:
    """Render the secure deployment plan and gcloud commands without mutating GCP.

    Use this tool before any deployment. The output is intentionally auditable and includes the exact
    commands that deploy_app would execute when mutations are enabled.
    """
    plan = build_plan(manifest)
    return structured_result(with_response_meta(plan, tool="cloud_run_deployer_plan_deployment"))


@mcp.tool(structured_output=False)
def plan_source_archive(app_id: str, source_path: str, project_id: str = "", bucket: str = "") -> CallToolResult:
    """Read-only helper that tells agents how to package a local dashboard folder for remote deploy.

    Remote MCP servers cannot see Codex's local repo checkout. Agents should call this before live deploys
    for repo-backed dashboard apps, run the returned commands locally, then put the printed GCS URI in the
    deployment manifest as `source.type="gcs_archive"` and `source.gcs_archive="<printed-uri>"`.
    """
    app = str(app_id or "").strip()
    src = str(source_path or "").strip().rstrip("/")
    project = str(project_id or os.getenv("GCP_PROJECT_ID", "it-team-hw-project")).strip()
    target_bucket = str(bucket or _default_source_bucket(project)).strip().replace("gs://", "").strip("/")
    if not app:
        return _validation_error("app_id is required")
    if not src:
        return _validation_error("source_path is required")
    if ".." in app or "/" in app:
        return _validation_error("app_id must be a simple app id, not a path")
    archive = f"/tmp/{app}-source.tgz"
    gcs_uri_template = f"gs://{target_bucket}/apps/{app}/source-$(date -u +%Y%m%d%H%M%S).tgz"
    commands = [
        f'test -d "{src}"',
        f'tar -C "{src}" -czf "{archive}" .',
        f'GCS_URI="{gcs_uri_template}"',
        f'gcloud storage cp "{archive}" "$GCS_URI"',
        'echo "$GCS_URI"',
    ]
    payload = {
        "ok": True,
        "app_id": app,
        "source_path": src,
        "project_id": project,
        "bucket": target_bucket,
        "commands": commands,
        "manifest_source_template": {
            "type": "gcs_archive",
            "gcs_archive": f"gs://{target_bucket}/apps/{app}/source-YYYYMMDDHHMMSS.tgz",
        },
        "next_steps": [
            "Run the commands locally from the repo workspace.",
            "Copy the printed GCS URI.",
            "Use that URI as manifest.source.gcs_archive.",
            "Then call validate_deployment_manifest, plan_deployment, and deploy_app.",
        ],
    }
    return structured_result(with_response_meta(payload, tool="cloud_run_deployer_plan_source_archive"))


@mcp.tool(structured_output=False)
def start_source_upload(
    app_id: str,
    files: list[dict[str, Any]],
    name: str = "",
    summary: str = "",
    framework: str = "auto",
    data: dict[str, Any] | None = None,
    access: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    data_connections: list[dict[str, Any]] | None = None,
    project_id: str = "",
    region: str = "",
) -> CallToolResult:
    """WRITE TOOL: start a resumable source upload for an MCP-only app deployment.

    Declared files must include path, size_bytes, sha256, and content_type ("text" or "binary").
    The returned upload_id is used with upload_source_chunk and finalize_source_upload.
    """
    result = start_upload_session(
        app_id=app_id,
        name=name,
        summary=summary,
        framework=framework,
        files=files,
        data=data,
        access=access,
        runtime=runtime,
        data_connections=data_connections,
        project_id=project_id,
        region=region,
    )
    return _source_upload_result(result, tool="cloud_run_deployer_start_source_upload")


@mcp.tool(structured_output=False)
def upload_source_chunk(
    upload_id: str,
    path: str,
    chunk_index: int,
    total_chunks: int,
    content_base64: str,
    sha256: str = "",
) -> CallToolResult:
    """WRITE TOOL: upload one base64-encoded source chunk for a started upload session."""
    result = upload_chunk(
        upload_id=upload_id,
        path=path,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        content_base64=content_base64,
        sha256=sha256,
    )
    return _source_upload_result(result, tool="cloud_run_deployer_upload_source_chunk")


@mcp.tool(structured_output=False)
def finalize_source_upload(upload_id: str, execute: bool = True) -> CallToolResult:
    """WRITE TOOL: validate uploaded chunks, create a source.tgz in GCS, and return a deploy-ready manifest."""
    result = finalize_upload_session(upload_id, execute=execute)
    return _source_upload_result(result, tool="cloud_run_deployer_finalize_source_upload")


@mcp.tool(structured_output=False)
def diagnose_runtime(project_id: str = "", bucket: str = "") -> CallToolResult:
    """Read-only diagnostic for the deployer Cloud Run runtime.

    Use this when Cloud Build or Cloud Run IAM behaves differently from the outside IAM policy.
    It reports the metadata service-account email, gcloud active auth/config, Cloud Build bucket
    access checks, and selected deployer environment variables. It never mutates GCP.
    """
    payload = {
        "ok": True,
        "diagnostics": runtime_diagnostics(project_id=project_id or None, bucket=bucket or None),
    }
    return structured_result(with_response_meta(payload, tool="cloud_run_deployer_diagnose_runtime"))


@mcp.tool(structured_output=False)
def get_deployment_status(
    build_id: str = "",
    service_name: str = "",
    project_id: str = "",
    region: str = "",
) -> CallToolResult:
    """Read-only status check for async managed app deployments.

    Agents should poll this after `deploy_app(..., async_build=true)` until the build status is SUCCESS
    or a terminal failure. When build status is SUCCESS, call `complete_deployment` with the same manifest.
    """
    project = project_id or os.getenv("GCP_PROJECT_ID", "it-team-hw-project")
    loc = region or os.getenv("CLOUD_RUN_DEPLOYER_REGION", "europe-west1")
    build = _describe_build(build_id=build_id, project_id=project, region=loc) if build_id else None
    service = (
        _describe_cloud_run_service(service_name=service_name, project_id=project, region=loc) if service_name else None
    )
    build_status = build.get("status") if isinstance(build, dict) else None
    payload = {
        "ok": True,
        "project_id": project,
        "region": loc,
        "build": build,
        "service": service,
        "ready_to_complete": build_status == "SUCCESS",
        "terminal": build_status in {"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"},
        "next_steps": (
            ["Call complete_deployment with the original manifest."]
            if build_status == "SUCCESS"
            else ["Poll get_deployment_status again until build status is SUCCESS or terminal failure."]
        ),
    }
    return structured_result(with_response_meta(payload, tool="cloud_run_deployer_get_deployment_status"))


@mcp.tool(structured_output=False)
def complete_deployment(manifest: dict[str, Any], build_id: str = "", execute: bool = False) -> CallToolResult:
    """WRITE TOOL: finish an async managed app deployment after Cloud Build succeeds.

    Use after `deploy_app(..., async_build=true)` and `get_deployment_status` reports build status SUCCESS.
    This runs the Cloud Run deploy, IAP binding, and declared data access binding commands.
    """
    result = validate_manifest(manifest)
    if not result.ok:
        return _validation_error("manifest failed deployment policy validation", errors=result.errors)
    commands = render_deploy_commands(result.normalized)
    if not execute:
        payload = {
            "ok": True,
            "executed": False,
            "dry_run": True,
            "warnings": result.warnings,
            "normalized_manifest": result.normalized,
            "app": app_metadata(result.normalized),
            "commands": render_shell(commands),
        }
        return structured_result(with_response_meta(payload, tool="cloud_run_deployer_complete_deployment"))
    if not MUTATIONS_ENABLED:
        return structured_result(
            tool_error(
                "mutations_disabled",
                details="Deployment execution is disabled. Set CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED=true.",
                cause="permission",
                retryable=False,
                commands=render_shell(commands),
            )
        )
    if build_id:
        build = _describe_build(
            build_id=build_id,
            project_id=result.normalized["project_id"],
            region=result.normalized["region"],
        )
        if build.get("status") != "SUCCESS":
            return structured_result(
                tool_error(
                    "build_not_ready",
                    details=f"Cloud Build status is {build.get('status') or 'unknown'}; not deploying Cloud Run yet.",
                    cause="upstream_error",
                    retryable=True,
                    build=build,
                )
            )
    command_results, error = _run_commands(commands)
    if error:
        if _cloud_run_deploy_completed(command_results, result.normalized["service_name"]):
            payload = _partial_deployment_payload(
                normalized_manifest=result.normalized,
                warnings=result.warnings,
                command_results=command_results,
                error=error,
                build_id=build_id or None,
            )
            return structured_result(with_response_meta(payload, tool="cloud_run_deployer_complete_deployment"))
        return structured_result(
            tool_error(
                error["error"],
                details=error["details"],
                cause="upstream_error",
                retryable=True,
                command_results=command_results,
            )
        )
    service_url = _resolve_service_url(
        service_name=result.normalized["service_name"],
        project_id=result.normalized["project_id"],
        region=result.normalized["region"],
        command_results=command_results,
    )
    app = app_metadata(result.normalized, service_url=service_url, approved_url=service_url)
    payload = {
        "ok": True,
        "executed": True,
        "service_deployed": True,
        "service_url": service_url,
        "approved_url": service_url,
        "warnings": result.warnings,
        "build_id": build_id or None,
        "normalized_manifest": result.normalized,
        "app": app,
        "command_results": command_results,
    }
    return structured_result(with_response_meta(payload, tool="cloud_run_deployer_complete_deployment"))


@mcp.tool(structured_output=False)
def deploy_app(
    manifest: dict[str, Any] | None = None,
    source_path: str = "",
    app_id: str = "",
    name: str = "",
    description: str = "",
    framework: str = "auto",
    data: dict[str, Any] | None = None,
    secrets: list[dict[str, Any]] | None = None,
    execute: bool = False,
    async_build: bool = False,
) -> CallToolResult:
    """WRITE TOOL: deploy a production web app and enable Google sign-in (IAP) on the Run URL.

    Preferred simple args are source_path, optional app_id/name/description/framework, data, and secrets.
    Existing manifest-based calls remain supported for compatibility. The app is deployed, then the same
    policy pipeline applies IAP so the URL is not anonymously reachable.
    """
    deployment_manifest = _simple_manifest(
        manifest=manifest,
        source_path=source_path,
        app_id=app_id,
        name=name,
        description=description,
        framework=framework,
        data=data,
        secrets=secrets,
    )
    result = validate_manifest(deployment_manifest)
    if not result.ok:
        return _validation_error("manifest failed deployment policy validation", errors=result.errors)

    commands = render_commands(result.normalized)
    if not execute:
        payload = {
            "ok": True,
            "executed": False,
            "dry_run": True,
            "warnings": result.warnings,
            "normalized_manifest": result.normalized,
            "app": app_metadata(result.normalized),
            "commands": render_shell(commands),
        }
        return structured_result(with_response_meta(payload, tool="cloud_run_deployer_deploy_app"))

    if not MUTATIONS_ENABLED:
        return structured_result(
            tool_error(
                "mutations_disabled",
                details=(
                    "Deployment execution is disabled. Set CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED=true "
                    "on the deployer service after the IAM and approval workflow are ready."
                ),
                cause="permission",
                retryable=False,
                commands=render_shell(commands),
            )
        )

    if result.normalized["source"]["type"] == "dashboard_app" and not ALLOW_LOCAL_SOURCE_PATHS:
        return structured_result(
            tool_error(
                "local_source_not_available",
                details=(
                    "This deployer runs remotely and cannot access manifest.source.path. "
                    "Call plan_source_archive, run the returned local packaging/upload commands from the repo "
                    "workspace, then deploy with source.type='gcs_archive' and source.gcs_archive set to the "
                    "uploaded GCS URI."
                ),
                cause="validation",
                retryable=False,
                normalized_manifest=result.normalized,
                next_steps=[
                    "Call plan_source_archive(app_id, source_path).",
                    "Run the returned commands locally.",
                    "Replace manifest.source with the printed GCS archive URI.",
                    "Call deploy_app again.",
                ],
            )
        )

    if async_build and result.normalized["source"]["type"] in {"gcs_archive", "dashboard_app"}:
        build_commands = render_build_commands(result.normalized, async_build=True)
        command_results, error = _run_commands(build_commands)
        if error:
            return structured_result(
                tool_error(
                    error["error"],
                    details=error["details"],
                    cause="upstream_error",
                    retryable=True,
                    command_results=command_results,
                )
            )
        build_id = _extract_build_id(command_results)
        build = (
            _describe_build(
                build_id=build_id,
                project_id=result.normalized["project_id"],
                region=result.normalized["region"],
            )
            if build_id
            else None
        )
        payload = {
            "ok": True,
            "executed": True,
            "async_build": True,
            "service_deployed": False,
            "build_submitted": True,
            "build_id": build_id,
            "build": build,
            "warnings": result.warnings,
            "normalized_manifest": result.normalized,
            "app": app_metadata(result.normalized),
            "command_results": command_results,
            "next_steps": [
                "Poll get_deployment_status with this build_id until status is SUCCESS.",
                "Then call complete_deployment with the original manifest, build_id, and execute=true.",
            ],
        }
        return structured_result(with_response_meta(payload, tool="cloud_run_deployer_deploy_app"))

    command_results, error = _run_commands(commands)
    if error:
        if _cloud_run_deploy_completed(command_results, result.normalized["service_name"]):
            payload = _partial_deployment_payload(
                normalized_manifest=result.normalized,
                warnings=result.warnings,
                command_results=command_results,
                error=error,
            )
            payload["async_build"] = False
            return structured_result(with_response_meta(payload, tool="cloud_run_deployer_deploy_app"))
        return structured_result(
            tool_error(
                error["error"],
                details=error["details"],
                cause="upstream_error",
                retryable=True,
                command_results=command_results,
            )
        )

    service_url = _resolve_service_url(
        service_name=result.normalized["service_name"],
        project_id=result.normalized["project_id"],
        region=result.normalized["region"],
        command_results=command_results,
    )
    normalized_manifest = dict(result.normalized)
    normalized_manifest["commit_sha"] = normalized_manifest.get("commit_sha") or _source_commit_sha(
        normalized_manifest["source"].get("path", "")
    )
    app = app_metadata(normalized_manifest, service_url=service_url, approved_url=service_url)
    payload = {
        "ok": True,
        "executed": True,
        "async_build": False,
        "service_deployed": True,
        "service_url": service_url,
        "approved_url": service_url,
        "warnings": result.warnings,
        "normalized_manifest": normalized_manifest,
        "app": app,
        "command_results": command_results,
    }
    return structured_result(with_response_meta(payload, tool="cloud_run_deployer_deploy_app"))


@mcp.tool(structured_output=False)
def approve_app(
    app_id: str,
    service_name: str = "",
    project_id: str = "",
    region: str = "",
    runtime_service_account: str = "",
    execute: bool = False,
) -> CallToolResult:
    """WRITE TOOL: expose an already deployed app behind Host Wise Google auth.

    MCP Proxy admins call this after reviewing a pending app. Approval enables Cloud Run IAP,
    grants Host Wise domain access, and switches ingress to the load-balancer/proxy path.
    """
    manifest = _app_manifest_from_ids(
        app_id=app_id,
        service_name=service_name,
        project_id=project_id,
        region=region,
        runtime_service_account=runtime_service_account,
    )
    try:
        commands = render_approve_commands(manifest)
    except ValueError as exc:
        return _validation_error(str(exc))
    if not execute:
        return structured_result(
            with_response_meta(
                {
                    "ok": True,
                    "executed": False,
                    "dry_run": True,
                    "app_id": manifest["app_id"],
                    "service_name": manifest["service_name"],
                    "approved_url": None,
                    "commands": render_shell(commands),
                },
                tool="cloud_run_deployer_approve_app",
            )
        )
    if not MUTATIONS_ENABLED:
        return structured_result(
            tool_error(
                "mutations_disabled",
                details="Approval execution is disabled. Set CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED=true.",
                cause="permission",
                retryable=False,
                commands=render_shell(commands),
            )
        )
    command_results, error = _run_commands(commands)
    if error:
        return structured_result(
            tool_error(
                error["error"],
                details=error["details"],
                cause="upstream_error",
                retryable=True,
                command_results=command_results,
            )
        )
    normalized = validate_manifest(manifest).normalized
    service_url = _resolve_service_url(
        service_name=normalized["service_name"],
        project_id=normalized["project_id"],
        region=normalized["region"],
        command_results=command_results,
    )
    return structured_result(
        with_response_meta(
            {
                "ok": True,
                "executed": True,
                "approved": True,
                "app": app_metadata(normalized, service_url=service_url, approved_url=service_url),
                "service_url": service_url,
                "approved_url": service_url,
                "command_results": command_results,
            },
            tool="cloud_run_deployer_approve_app",
        )
    )


@mcp.tool(structured_output=False)
def get_app_status(
    app_id: str,
    service_name: str = "",
    project_id: str = "",
    region: str = "",
) -> CallToolResult:
    """Read-only status check for one production app."""
    manifest = _app_manifest_from_ids(app_id=app_id, service_name=service_name, project_id=project_id, region=region)
    service = _describe_cloud_run_service(
        service_name=manifest["service_name"],
        project_id=manifest["project_id"],
        region=manifest["region"],
    )
    payload = {
        "ok": True,
        "app_id": manifest["app_id"],
        "service_name": manifest["service_name"],
        "project_id": manifest["project_id"],
        "region": manifest["region"],
        "approved_url": service.get("url"),
        "service": service,
    }
    return structured_result(with_response_meta(payload, tool="cloud_run_deployer_get_app_status"))


@mcp.tool(structured_output=False)
def get_app_logs(
    app_id: str,
    service_name: str = "",
    project_id: str = "",
    region: str = "",
    limit: int = 100,
    severity: str = "default",
) -> CallToolResult:
    """Read recent Cloud Logging entries scoped to one app service."""
    manifest = _app_manifest_from_ids(app_id=app_id, service_name=service_name, project_id=project_id, region=region)
    sev = str(severity or "default").lower()
    severity_filter = "" if sev == "default" else f' AND severity>="{sev.upper()}"'
    log_filter = (
        'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{manifest["service_name"]}"'
        f"{severity_filter}"
    )
    result = _run_probe(
        [
            "gcloud",
            "logging",
            "read",
            log_filter,
            "--project",
            manifest["project_id"],
            "--limit",
            str(max(1, min(int(limit or 100), 500))),
            "--format",
            "json",
        ],
        timeout=60,
    )
    entries: list[Any] = []
    if result["returncode"] == 0:
        try:
            entries = json.loads(result.get("stdout") or "[]")
        except Exception:
            entries = []
    return structured_result(
        with_response_meta(
            {
                "ok": result["returncode"] == 0,
                "app_id": manifest["app_id"],
                "service_name": manifest["service_name"],
                "entries": entries,
                "probe": result,
            },
            tool="cloud_run_deployer_get_app_logs",
        )
    )


@mcp.tool(structured_output=False)
def delete_app(
    app_id: str = "",
    service_name: str = "",
    project_id: str = "",
    region: str = "",
    delete_mode: str = "cloud_run_only",
    image: str = "",
    runtime_service_account: str = "",
    storage_bucket: str = "",
    execute: bool = False,
) -> CallToolResult:
    """WRITE TOOL: delete a managed Cloud Run app.

    MCP Proxy calls this when an admin deletes an app in the Apps UI. `cloud_run_only` deletes the
    Cloud Run service and keeps audit/source history. `full_cleanup` additionally attempts safe image
    and app-owned service-account cleanup when those values are provided.
    """
    try:
        if not service_name and app_id:
            service_name = f"app-{_slugify_app_id(app_id)}"
        commands = render_delete_commands(
            service_name=service_name,
            project_id=project_id or None,
            region=region or None,
            delete_mode=delete_mode,
            image=image or None,
            runtime_service_account=runtime_service_account or None,
            storage_bucket=storage_bucket or None,
        )
    except ValueError as exc:
        return _validation_error(str(exc))

    if not execute:
        return structured_result(
            with_response_meta(
                {
                    "ok": True,
                    "executed": False,
                    "dry_run": True,
                    "service_name": service_name,
                    "project_id": project_id,
                    "region": region,
                    "delete_mode": delete_mode,
                    "commands": render_shell(commands),
                },
                tool="cloud_run_deployer_delete_app",
            )
        )

    if not MUTATIONS_ENABLED:
        return structured_result(
            tool_error(
                "mutations_disabled",
                details="Deletion execution is disabled. Set CLOUD_RUN_DEPLOYER_MUTATIONS_ENABLED=true.",
                cause="permission",
                retryable=False,
                commands=render_shell(commands),
            )
        )

    command_results, error = _run_commands(commands)
    already_missing = False
    if error:
        combined = "\n".join(str(r.get("stderr") or "") for r in command_results).lower()
        if "not found" in combined or "does not exist" in combined:
            already_missing = True
        else:
            return structured_result(
                tool_error(
                    error["error"],
                    details=error["details"],
                    cause="upstream_error",
                    retryable=True,
                    command_results=command_results,
                )
            )
    return structured_result(
        with_response_meta(
            {
                "ok": True,
                "executed": True,
                "deleted": True,
                "already_missing_upstream": already_missing,
                "service_name": service_name,
                "project_id": project_id,
                "region": region,
                "delete_mode": delete_mode,
                "command_results": command_results,
            },
            tool="cloud_run_deployer_delete_app",
        )
    )
