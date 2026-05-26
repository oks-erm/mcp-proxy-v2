"""Policy and command rendering for simple Host Wise web app deployments."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,32}$")
PRINCIPAL_RE = re.compile(r"^(user|group|domain|serviceAccount):[^@\s]+@?[^@\s]*$")

DEFAULT_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "it-team-hw-project")
DEFAULT_REGION = os.getenv("CLOUD_RUN_DEPLOYER_REGION", "europe-west1")
DEFAULT_REPOSITORY = os.getenv("CLOUD_RUN_DEPLOYER_REPOSITORY", "docker")
DEFAULT_SERVICE_PREFIX = os.getenv("CLOUD_RUN_DEPLOYER_SERVICE_PREFIX", "app")
DEFAULT_RUNTIME_SA_PREFIX = os.getenv("CLOUD_RUN_DEPLOYER_RUNTIME_SA_PREFIX", "app")
DEFAULT_GCS_SOURCE_STAGING_DIR = os.getenv("CLOUD_RUN_DEPLOYER_GCS_SOURCE_STAGING_DIR", "").strip()
DEFAULT_GITHUB_ORG = os.getenv("CLOUD_RUN_DEPLOYER_GITHUB_ORG", "").strip()
DEFAULT_GITHUB_REPO_PREFIX = os.getenv("CLOUD_RUN_DEPLOYER_GITHUB_REPO_PREFIX", "app").strip()
DEFAULT_GITHUB_BOT_NAME = os.getenv("CLOUD_RUN_DEPLOYER_GITHUB_BOT_NAME", "hostwise-codex-bot").strip()
DEFAULT_GITHUB_BOT_EMAIL = os.getenv("CLOUD_RUN_DEPLOYER_GITHUB_BOT_EMAIL", "codex-bot@hostwise.pt").strip()
DEFAULT_DASHBOARD_RUNTIME_SA = os.getenv(
    "CLOUD_RUN_DEPLOYER_DASHBOARD_RUNTIME_SERVICE_ACCOUNT",
    f"dashboard-runtime-sa@{DEFAULT_PROJECT_ID}.iam.gserviceaccount.com",
)
# When unset, every IAP app allow-lists the Hostwise Workspace domain (overridable via env or manifest).
_DEFAULT_IAP_ORG_DOMAIN = "domain:hostwise.pt"
REMOVED_IAP_PRINCIPALS = frozenset({"domain:hostwise.co"})
_IAP_PRINCIPALS_FROM_ENV = [
    p.strip() for p in os.getenv("CLOUD_RUN_DEPLOYER_DEFAULT_IAP_PRINCIPALS", "").split(",") if p.strip()
]
DEFAULT_IAP_PRINCIPALS = [
    p for p in (_IAP_PRINCIPALS_FROM_ENV or [_DEFAULT_IAP_ORG_DOMAIN]) if p not in REMOVED_IAP_PRINCIPALS
] or [_DEFAULT_IAP_ORG_DOMAIN]

ALLOWED_SOURCE_TYPES = {"image", "gcs_archive", "dashboard_app"}
ALLOWED_ACCESS_MODES = {"iap", "cloud_run_iam"}
ALLOWED_FRAMEWORKS = {"auto", "nextjs", "vite", "express", "fastapi", ""}
ALLOWED_DATA_TYPES = {
    "firestore_gateway",
    "sql_gateway",
    "mcp_proxy",
    "cloud_sql",
    "none",
}
ALLOWED_DATA_ACCESS = {"read_only", "read_write"}
ALLOWED_DELETE_MODES = {"cloud_run_only", "full_cleanup"}

_LOGIN_FILENAME = re.compile(r"^login\.(tsx|jsx)$", re.I)
_AUTH_ROUTE_IN_SOURCE = re.compile(
    r"([\"'])/login\1|path\s*=\s*\{?[\"']/login|to=\{?[\"']/login|path:\s*[\"']/login|route\([^)]*[\"']/login",
    re.I,
)


def verify_dashboard_includes_auth_page(root: Path) -> list[str]:
    """When source.path exists on the deployer host, require a /login entry surface in the React tree."""
    if not root.is_dir():
        return [f"dashboard source path is not a directory: {root}"]
    fe = root / "frontend"
    if not fe.is_dir():
        return [
            "managed dashboard must include a frontend/ directory with a dedicated auth page "
            "(e.g. frontend/src/pages/Login.tsx) and a /login route; see the managed template"
        ]
    _skip_dir = ("node_modules", "dist", "build")

    def _skip(p: Path) -> bool:
        return any(x in p.parts for x in _skip_dir)

    for path in fe.rglob("*"):
        if not path.is_file() or _skip(path):
            continue
        if _LOGIN_FILENAME.match(path.name):
            return []
    for path in fe.rglob("*"):
        if not path.is_file() or _skip(path):
            continue
        if path.suffix.lower() not in {".tsx", ".jsx", ".ts", ".js"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _AUTH_ROUTE_IN_SOURCE.search(text):
            return []
    return [
        "managed dashboard must include a /login auth page: add frontend/**/Login.tsx (or .jsx) "
        "or register a '/login' route in your React router; see cloud-run-deployer://templates/dashboard-app"
    ]


def _dashboard_auth_policy_notes(source_type: str, source_path: str) -> tuple[list[str], list[str]]:
    """Returns compatibility warnings for legacy local-source manifests.

    Auth is now enforced by the Host Wise proxy/IAP layer, not by requiring every
    generated app to implement a dedicated login page.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if source_type == "dashboard_app" and source_path:
        p = Path(source_path).expanduser()
        if not p.is_dir():
            warnings.append(
                "source.path is not a directory on this deployer host; remote deployments should use a GCS archive"
            )
    if source_type in ("gcs_archive", "image"):
        warnings.append(
            "Image and GCS archive sources are not scanned here; ensure secrets and production data credentials "
            "are not committed before deploying."
        )
    return errors, warnings


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    normalized: dict[str, Any]


def validate_manifest(manifest: dict[str, Any]) -> ValidationResult:
    """Validate and normalize a Cloud Run app deployment manifest."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(manifest, dict):
        return ValidationResult(False, ["manifest must be an object"], [], {})

    app_id = _clean(manifest.get("app_id"))
    if not app_id or not APP_ID_RE.match(app_id):
        errors.append("app_id must match ^[a-z][a-z0-9-]{2,32}$")

    project_id = _clean(manifest.get("project_id")) or DEFAULT_PROJECT_ID
    region = _clean(manifest.get("region")) or DEFAULT_REGION
    service_name = _clean(manifest.get("service_name")) or f"{DEFAULT_SERVICE_PREFIX}-{app_id}" if app_id else ""
    name = _clean(manifest.get("name")) or app_id
    summary = _clean(manifest.get("summary")) or _clean(manifest.get("description"))
    data_access_summary = _clean(manifest.get("data_access_summary")) or _default_data_access_summary(manifest)
    framework = _clean(manifest.get("framework")).lower() or "auto"
    if framework not in ALLOWED_FRAMEWORKS:
        errors.append(f"framework must be one of {sorted(f for f in ALLOWED_FRAMEWORKS if f)}")
    if not summary:
        errors.append("summary is required and must explain what the app does")
    if not data_access_summary:
        errors.append("data_access_summary is required and must explain what data the app accesses")

    source = manifest.get("source") or {}
    if not isinstance(source, dict):
        errors.append("source must be an object")
        source = {}
    source_type = _clean(source.get("type")) or "image"
    runtime_sa = _clean(manifest.get("runtime_service_account"))
    if not runtime_sa and app_id:
        runtime_sa = f"{_runtime_service_account_id(app_id)}@{project_id}.iam.gserviceaccount.com"
    if source_type not in ALLOWED_SOURCE_TYPES:
        errors.append(f"source.type must be one of {sorted(ALLOWED_SOURCE_TYPES)}")
    image = _clean(source.get("image"))
    gcs_archive = _clean(source.get("gcs_archive"))
    source_path = _clean(source.get("path"))
    if source_type == "image" and not image:
        errors.append("source.image is required when source.type is image")
    if source_type == "gcs_archive" and not gcs_archive:
        errors.append("source.gcs_archive is required when source.type is gcs_archive")
    if source_type == "dashboard_app" and not source_path:
        errors.append("source.path is required when source.type is dashboard_app")

    access = manifest.get("access") or {}
    if not isinstance(access, dict):
        errors.append("access must be an object")
        access = {}
    mode = _clean(access.get("mode")) or "iap"
    if mode not in ALLOWED_ACCESS_MODES:
        errors.append("access.mode must be iap or cloud_run_iam; public access is not supported")
    if access.get("allow_public") is True or mode == "public":
        errors.append("public access is forbidden by policy")
    requested_iap_principals = _clean_list(access.get("iap_principals"))
    removed_iap_principals = [p for p in requested_iap_principals if p in REMOVED_IAP_PRINCIPALS]
    if removed_iap_principals:
        warnings.append(
            "Removed unsupported IAP principal(s): "
            + ", ".join(removed_iap_principals)
            + ". Use domain:hostwise.pt or explicit users/groups instead."
        )
    iap_principals = [p for p in requested_iap_principals if p not in REMOVED_IAP_PRINCIPALS] or DEFAULT_IAP_PRINCIPALS
    if mode == "iap" and not iap_principals:
        errors.append("access.iap_principals is required for IAP apps (no default principals are configured)")
    for principal in iap_principals:
        if not PRINCIPAL_RE.match(principal):
            errors.append(f"invalid IAP principal: {principal}")

    runtime = manifest.get("runtime") or {}
    if not isinstance(runtime, dict):
        errors.append("runtime must be an object")
        runtime = {}
    port = _coerce_int(runtime.get("port"), 8080)
    max_instances = _coerce_int(runtime.get("max_instances"), 1)
    min_instances = _coerce_int(runtime.get("min_instances"), 0)
    cpu = _clean(runtime.get("cpu")) or "1000m"
    memory = _clean(runtime.get("memory")) or "512Mi"
    timeout = _clean(runtime.get("timeout")) or "300s"
    if max_instances < 1 or max_instances > 20:
        errors.append("runtime.max_instances must be between 1 and 20")
    if min_instances < 0 or min_instances > max_instances:
        errors.append("runtime.min_instances must be between 0 and max_instances")

    data_connections = manifest.get("data_connections") or []
    if not isinstance(data_connections, list):
        errors.append("data_connections must be a list")
        data_connections = []
    normalized_connections: list[dict[str, Any]] = []
    for idx, raw in enumerate(data_connections):
        if not isinstance(raw, dict):
            errors.append(f"data_connections[{idx}] must be an object")
            continue
        conn_type = _clean(raw.get("type"))
        access_level = _clean(raw.get("access")) or "read_only"
        if conn_type not in ALLOWED_DATA_TYPES:
            errors.append(f"data_connections[{idx}].type must be one of {sorted(ALLOWED_DATA_TYPES)}")
        if access_level not in ALLOWED_DATA_ACCESS:
            errors.append(f"data_connections[{idx}].access must be read_only or read_write")
        if access_level == "read_write":
            warnings.append(f"data_connections[{idx}] requests read_write; require human approval before execute")
        normalized_connections.append(
            {
                "id": _clean(raw.get("id")) or f"connection-{idx + 1}",
                "type": conn_type,
                "access": access_level,
                "target": _clean(raw.get("target")),
                "secret_id": _clean(raw.get("secret_id")),
                "env": _clean(raw.get("env")),
            }
        )

    normalized = {
        "app_id": app_id,
        "name": name,
        "summary": summary,
        "data_access_summary": data_access_summary,
        "project_id": project_id,
        "region": region,
        "service_name": service_name,
        "runtime_service_account": runtime_sa,
        "framework": framework,
        "source": {
            "type": source_type,
            "image": image,
            "gcs_archive": gcs_archive,
            "path": source_path,
        },
        "access": {
            "mode": mode,
            "iap_principals": iap_principals,
        },
        "runtime": {
            "port": port,
            "cpu": cpu,
            "memory": memory,
            "timeout": timeout,
            "min_instances": min_instances,
            "max_instances": max_instances,
        },
        "data_connections": normalized_connections,
        "labels": {
            "managed-by": "cloud-run-deployer-mcp",
            "hostwise-app-id": app_id,
        },
        "repo_url": _github_repo_url(app_id),
        "approved_url": "",
        "data": _normalize_data_config(manifest.get("data")),
        "secrets": _normalize_secrets(manifest.get("secrets")),
    }
    auth_err, auth_warn = _dashboard_auth_policy_notes(source_type, source_path)
    errors.extend(auth_err)
    warnings.extend(auth_warn)
    return ValidationResult(not errors, errors, warnings, normalized)


def render_commands(manifest: dict[str, Any]) -> list[list[str]]:
    """Render gcloud commands for a validated manifest."""
    v = validate_manifest(manifest)
    if not v.ok:
        raise ValueError("; ".join(v.errors))
    n = v.normalized
    return [*render_github_commands(n), *render_build_commands(n), *render_deploy_commands(n)]


def render_github_commands(manifest: dict[str, Any]) -> list[list[str]]:
    """Render optional GitHub private repo create/update commands for local app sources."""
    v = validate_manifest(manifest)
    if not v.ok:
        raise ValueError("; ".join(v.errors))
    n = v.normalized
    if not DEFAULT_GITHUB_ORG or n["source"]["type"] != "dashboard_app" or not n["source"]["path"]:
        return []
    repo = _github_repo_name(n["app_id"])
    repo_full = f"{DEFAULT_GITHUB_ORG}/{repo}"
    repo_url = _github_repo_url(n["app_id"])
    source_path = n["source"]["path"]
    script = (
        f"set -euo pipefail; cd {shlex.quote(source_path)}; "
        "git init >/dev/null; "
        "git checkout -B main >/dev/null; "
        "git add .; "
        f"git -c user.name={shlex.quote(DEFAULT_GITHUB_BOT_NAME)} "
        f"-c user.email={shlex.quote(DEFAULT_GITHUB_BOT_EMAIL)} "
        f"commit -m {shlex.quote('Deploy ' + n['app_id'])} >/dev/null || true; "
        f"gh repo view {shlex.quote(repo_full)} >/dev/null 2>&1 || "
        f"gh repo create {shlex.quote(repo_full)} --private >/dev/null; "
        "git remote remove hostwise-origin >/dev/null 2>&1 || true; "
        f"git remote add hostwise-origin {shlex.quote(repo_url)}; "
        "git push hostwise-origin HEAD:main --force"
    )
    return [["bash", "-lc", script]]


def render_build_commands(manifest: dict[str, Any], *, async_build: bool = False) -> list[list[str]]:
    """Render Cloud Build commands for manifests that need image builds."""
    v = validate_manifest(manifest)
    if not v.ok:
        raise ValueError("; ".join(v.errors))
    n = v.normalized
    project = n["project_id"]
    region = n["region"]
    if n["source"]["type"] not in {"gcs_archive", "dashboard_app"}:
        return []
    image = _resolved_image(n)
    source_dir = n["source"]["gcs_archive"] if n["source"]["type"] == "gcs_archive" else n["source"]["path"]
    command = _with_gcs_source_staging_dir(
        [
            "gcloud",
            "builds",
            "submit",
            source_dir,
            "--project",
            project,
            "--region",
            region,
            "--tag",
            image,
            "--quiet",
        ]
    )
    if async_build:
        command.extend(["--async", "--format", "json"])
    return [command]


def render_deploy_commands(manifest: dict[str, Any]) -> list[list[str]]:
    """Render Cloud Run deploy, data bindings, then Host Wise Google auth (IAP) exposure.

    First `gcloud run deploy` uses private ingress, then the same flow applies the same
    steps as :func:`render_approve_commands` so the default service URL is reachable with
    Google sign-in (IAP + principals), without a separate manual approval in MCP Proxy.
    """
    v = validate_manifest(manifest)
    if not v.ok:
        raise ValueError("; ".join(v.errors))
    n = v.normalized
    project = n["project_id"]
    region = n["region"]
    service_name = n["service_name"]
    runtime_sa = n["runtime_service_account"]
    commands: list[list[str]] = [_runtime_service_account_command(n)]
    image = _resolved_image(n)
    labels = ",".join(f"{k}={v}" for k, v in n["labels"].items() if v)
    deploy = [
        "gcloud",
        "run",
        "deploy",
        service_name,
        "--project",
        project,
        "--region",
        region,
        "--image",
        image,
        "--port",
        str(n["runtime"]["port"]),
        "--service-account",
        runtime_sa,
        "--cpu",
        n["runtime"]["cpu"],
        "--memory",
        n["runtime"]["memory"],
        "--timeout",
        n["runtime"]["timeout"],
        "--min-instances",
        str(n["runtime"]["min_instances"]),
        "--max-instances",
        str(n["runtime"]["max_instances"]),
        "--labels",
        labels,
        "--ingress",
        "internal",
        "--no-allow-unauthenticated",
        "--quiet",
    ]
    commands.append(deploy)

    for conn in n["data_connections"]:
        commands.extend(_data_connection_commands(conn, project, region, runtime_sa))
    commands.extend(render_approve_commands(n))
    return [command for command in commands if command]


def render_approve_commands(manifest: dict[str, Any]) -> list[list[str]]:
    """Render commands that expose an already deployed app behind Host Wise auth."""
    v = validate_manifest(manifest)
    if not v.ok:
        raise ValueError("; ".join(v.errors))
    n = v.normalized
    project = n["project_id"]
    region = n["region"]
    service_name = n["service_name"]
    commands: list[list[str]] = [
        [
            "gcloud",
            "run",
            "services",
            "update",
            service_name,
            "--project",
            project,
            "--region",
            region,
            "--ingress",
            "all",
            "--iap",
            "--quiet",
        ],
        [
            "bash",
            "-lc",
            (
                f"PROJECT_NUMBER=$(gcloud projects describe {shlex.quote(project)} "
                "--format='value(projectNumber)') && "
                f"gcloud run services add-iam-policy-binding {shlex.quote(service_name)} "
                f"--project {shlex.quote(project)} "
                f"--region {shlex.quote(region)} "
                '--member "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" '
                "--role roles/run.invoker "
                "--quiet"
            ),
        ],
    ]
    commands.extend(_remove_iap_principal_commands(n))
    for principal in n["access"]["iap_principals"]:
        commands.append(
            [
                "gcloud",
                "iap",
                "web",
                "add-iam-policy-binding",
                "--project",
                project,
                "--region",
                region,
                "--resource-type",
                "cloud-run",
                "--service",
                service_name,
                "--member",
                principal,
                "--role",
                "roles/iap.httpsResourceAccessor",
                "--quiet",
            ]
        )
    return commands


def render_shell(commands: list[list[str]]) -> list[str]:
    return [" ".join(shlex.quote(part) for part in command) for command in commands]


def build_plan(manifest: dict[str, Any]) -> dict[str, Any]:
    v = validate_manifest(manifest)
    if not v.ok:
        return {"ok": False, "errors": v.errors, "warnings": v.warnings, "normalized_manifest": v.normalized}
    commands = render_commands(v.normalized)
    return {
        "ok": True,
        "warnings": v.warnings,
        "normalized_manifest": v.normalized,
        "app": app_metadata(v.normalized),
        "security_defaults": [
            "The deploy pipeline enables IAP and the default Run URL is only reachable with Google sign-in (Host Wise principals; domain:hostwise.pt if none set)",
            "Unauthenticated public access to Cloud Run is not enabled; IAP is required to reach the service",
            "App runtime service accounts start with minimal permissions",
            "App-owned SQLite/uploads are preferred over production data connections",
        ],
        "commands": render_shell(commands),
        "approval_commands": [],
    }


def app_metadata(
    manifest: dict[str, Any], *, service_url: str | None = None, approved_url: str | None = None
) -> dict[str, Any]:
    """Return managed-app metadata for mcp-proxy governance records."""
    return {
        "app_id": manifest.get("app_id"),
        "name": manifest.get("name") or manifest.get("app_id"),
        "summary": manifest.get("summary"),
        "data_access_summary": manifest.get("data_access_summary"),
        "data_connections": manifest.get("data_connections") or [],
        "service_name": manifest.get("service_name"),
        "service_url": service_url,
        "project_id": manifest.get("project_id"),
        "region": manifest.get("region"),
        "runtime_service_account": manifest.get("runtime_service_account"),
        "framework": manifest.get("framework"),
        "repo_url": manifest.get("repo_url"),
        "approved_url": approved_url or manifest.get("approved_url") or None,
        "version": manifest.get("version"),
        "commit_sha": manifest.get("commit_sha"),
        "build_id": manifest.get("build_id"),
        "image_digest": manifest.get("image_digest"),
        "cloud_run_revision": manifest.get("cloud_run_revision"),
    }


def render_delete_commands(
    *,
    service_name: str,
    project_id: str | None = None,
    region: str | None = None,
    delete_mode: str = "cloud_run_only",
    image: str | None = None,
    runtime_service_account: str | None = None,
    storage_bucket: str | None = None,
) -> list[list[str]]:
    """Render gcloud commands to delete a managed Cloud Run app."""
    project = _clean(project_id) or DEFAULT_PROJECT_ID
    loc = _clean(region) or DEFAULT_REGION
    mode = _clean(delete_mode) or "cloud_run_only"
    svc = _clean(service_name)
    if not svc:
        raise ValueError("service_name is required")
    if mode not in ALLOWED_DELETE_MODES:
        raise ValueError(f"delete_mode must be one of {sorted(ALLOWED_DELETE_MODES)}")
    commands: list[list[str]] = [
        [
            "gcloud",
            "run",
            "services",
            "delete",
            svc,
            "--project",
            project,
            "--region",
            loc,
            "--quiet",
        ]
    ]
    if mode == "full_cleanup":
        if image:
            commands.append(
                ["gcloud", "artifacts", "docker", "images", "delete", image, "--project", project, "--quiet"]
            )
        if runtime_service_account and not runtime_service_account.startswith("dashboard-runtime-sa@"):
            commands.append(
                [
                    "gcloud",
                    "iam",
                    "service-accounts",
                    "delete",
                    runtime_service_account,
                    "--project",
                    project,
                    "--quiet",
                ]
            )
        if storage_bucket:
            commands.append(["gcloud", "storage", "rm", "-r", f"gs://{storage_bucket}", "--project", project])
    return commands


def _data_connection_commands(conn: dict[str, Any], project: str, region: str, runtime_sa: str) -> list[list[str]]:
    conn_type = conn.get("type")
    target = conn.get("target")
    if conn_type in {"none", "", None}:
        return []
    if conn_type in {"firestore_gateway", "sql_gateway", "mcp_proxy"}:
        if not target:
            return []
        return [
            [
                "gcloud",
                "run",
                "services",
                "add-iam-policy-binding",
                target,
                "--project",
                project,
                "--region",
                region,
                "--member",
                f"serviceAccount:{runtime_sa}",
                "--role",
                "roles/run.invoker",
                "--quiet",
            ]
        ]
    if conn_type == "cloud_sql":
        return [
            [
                "gcloud",
                "projects",
                "add-iam-policy-binding",
                project,
                "--member",
                f"serviceAccount:{runtime_sa}",
                "--role",
                "roles/cloudsql.client",
                "--condition",
                f"expression=true,title=CloudSQL_{conn.get('id', 'app')},description=App approved Cloud SQL access",
                "--quiet",
            ]
        ]
    return []


def _remove_iap_principal_commands(n: dict[str, Any]) -> list[list[str]]:
    project = n["project_id"]
    region = n["region"]
    service_name = n["service_name"]
    commands: list[list[str]] = []
    for principal in sorted(REMOVED_IAP_PRINCIPALS):
        commands.append(
            [
                "bash",
                "-lc",
                (
                    "gcloud iap web remove-iam-policy-binding "
                    f"--project {shlex.quote(project)} "
                    f"--region {shlex.quote(region)} "
                    "--resource-type cloud-run "
                    f"--service {shlex.quote(service_name)} "
                    f"--member {shlex.quote(principal)} "
                    "--role roles/iap.httpsResourceAccessor "
                    "--quiet >/dev/null 2>&1 || true"
                ),
            ]
        )
    return commands


def _runtime_service_account_command(n: dict[str, Any]) -> list[str]:
    project = n["project_id"]
    service_name = n["service_name"]
    runtime_sa = n["runtime_service_account"]
    if runtime_sa == DEFAULT_DASHBOARD_RUNTIME_SA.replace(DEFAULT_PROJECT_ID, project):
        return []
    account_id = runtime_sa.split("@", 1)[0]
    return [
        "bash",
        "-lc",
        (
            f"gcloud iam service-accounts describe {shlex.quote(runtime_sa)} "
            f"--project {shlex.quote(project)} >/dev/null 2>&1 || "
            f"gcloud iam service-accounts create {shlex.quote(account_id)} "
            f"--project {shlex.quote(project)} "
            f"--display-name {shlex.quote(f'Runtime SA for {service_name}')} "
            "--quiet"
        ),
    ]


def _resolved_image(n: dict[str, Any]) -> str:
    if n["source"]["type"] in {"gcs_archive", "dashboard_app"}:
        return f"{n['region']}-docker.pkg.dev/{n['project_id']}/{DEFAULT_REPOSITORY}/hostwise/apps/{n['app_id']}:latest"
    return n["source"]["image"]


def _github_repo_name(app_id: str) -> str:
    app = _clean(app_id)
    prefix = _clean(DEFAULT_GITHUB_REPO_PREFIX)
    return f"{prefix}-{app}" if prefix else app


def _github_repo_url(app_id: str) -> str:
    app = _clean(app_id)
    if not app or not DEFAULT_GITHUB_ORG:
        return ""
    return f"https://github.com/{DEFAULT_GITHUB_ORG}/{_github_repo_name(app)}"


def _default_data_access_summary(manifest: dict[str, Any]) -> str:
    data = _normalize_data_config(manifest.get("data"))
    parts = []
    if data.get("sqlite"):
        parts.append("app-owned SQLite database")
    if data.get("uploads"):
        parts.append("user-uploaded files")
    if not parts:
        parts.append("no declared external data sources")
    return "Uses " + " and ".join(parts) + "; no direct BigQuery or production database access."


def _normalize_data_config(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {"sqlite": True, "uploads": True}
    return {
        "sqlite": bool(value.get("sqlite", True)),
        "uploads": bool(value.get("uploads", True)),
    }


def _normalize_secrets(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        if name:
            out.append({"name": name})
    return out


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [_clean(v) for v in value if _clean(v)]
    return []


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _runtime_service_account_id(app_id: str) -> str:
    raw = f"{DEFAULT_RUNTIME_SA_PREFIX}-{app_id}-sa"
    if len(raw) <= 30:
        return raw
    digest = hashlib.sha1(app_id.encode("utf-8")).hexdigest()[:6]
    prefix = f"{DEFAULT_RUNTIME_SA_PREFIX}-{app_id[:19]}"
    return f"{prefix}-{digest}"[:30].rstrip("-")


def _with_gcs_source_staging_dir(command: list[str]) -> list[str]:
    """Attach an explicit Cloud Build source staging bucket when configured."""
    if not DEFAULT_GCS_SOURCE_STAGING_DIR:
        return command
    return [*command, "--gcs-source-staging-dir", DEFAULT_GCS_SOURCE_STAGING_DIR]
