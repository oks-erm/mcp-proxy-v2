"""MCP Proxy: Streamable HTTP transport, session management, JSON-RPC forwarding.

Implements the same Streamable HTTP transport as upstream MCP servers (sql-gateway,
n8n-mcp, firestore-gateway) - single /mcp endpoint for POST (JSON-RPC) and GET (SSE),
mcp-session-id header, compatible with MCP clients (e.g. Cursor).
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
import httpx
from audit.logger import log_audit
from credentials.generator import resolve_user_by_key
from firestore_store import list_servers
from help_catalog import (
    HELP_URI_PREFIX,
    list_help_resource_descriptors,
    list_help_resource_templates,
    list_proxy_prompt_descriptors,
    read_help_resource,
    read_proxy_prompt,
)
from improvement_requests_schemas import ImprovementRequestCreate
from improvement_requests_store import (
    ImprovementRequestRateLimitError,
    create_improvement_request,
)
from managed_app_secrets_store import (
    delete_app_secret,
    delete_app_secrets,
    list_app_secrets,
    set_app_secret,
)
from managed_apps_store import (
    get_managed_app,
    mark_managed_app_deleted,
    upsert_managed_app,
)
from managed_workflows_store import (
    mark_managed_workflow_deleted,
    upsert_managed_workflow,
)
from mcp_utils import dedupe_tool_call_jsonrpc, parse_mcp_response
from metrics_counters import (
    increment_denied_invalid_user,
    increment_denied_missing_key,
    increment_errors,
    increment_method,
    increment_requests_authorized,
)
from permissions.models import PermissionItem
from permissions.store import get_user_permissions
from permissions.write_tools import upstream_tool_requires_write
from pydantic import ValidationError
from skill_catalog import (
    build_install_manifest,
    get_catalog_skill,
    list_catalog_skills,
    skill_manifest,
)
from skill_update_requests_schemas import SkillUpdateRequestCreate
from skill_update_requests_store import (
    SkillUpdateRequestRateLimitError,
    create_skill_update_request,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from upstream_headers import resolve_upstream_headers
from upstream_oauth.tokens import UpstreamOAuthNotConnectedError
from users.schemas import UserInDB
from users.usage_store import record_authorized_request, record_tool_call

logger = logging.getLogger(__name__)

# MCP Streamable HTTP transport header (matches mcp.server.streamable_http)
MCP_SESSION_ID_HEADER = "mcp-session-id"

# n8n tools that support user attribution via the user_name argument
_N8N_USER_ATTRIBUTION_TOOLS = frozenset({"n8n_create_workflow", "n8n_update_workflow"})
_LIST_SKILLS_TOOL_NAME = "mcp_proxy_list_skills"
_INSTALL_SKILL_TOOL_NAME = "mcp_proxy_install_skill"
_CHECK_SKILL_UPDATES_TOOL_NAME = "mcp_proxy_check_skill_updates"
_REQUEST_SKILL_UPDATE_TOOL_NAME = "mcp_proxy_request_skill_update"
_IMPROVEMENT_REQUEST_TOOL_NAME = "mcp_proxy_request_improvement"
_SET_APP_SECRET_TOOL_NAME = "mcp_proxy_set_app_secret"
_LIST_APP_SECRETS_TOOL_NAME = "mcp_proxy_list_app_secrets"
_DELETE_APP_SECRET_TOOL_NAME = "mcp_proxy_delete_app_secret"
_TOOL_ALIASES: Dict[str, str] = {
    # Shorter proxy-facing alias for the most common Zendesk reservation -> ticket resolver.
    "find_ticket_by_reservation_id": "zendesk_find_ticket_by_reservation_id",
}

_LOCAL_MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": _LIST_SKILLS_TOOL_NAME,
        "description": (
            "List the shared Codex skills published by the MCP proxy. "
            "Use this before installing a skill so you can inspect names, descriptions, and bundle URLs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_files": {
                    "type": "boolean",
                    "description": "When true, include the file list for each published skill.",
                    "default": False,
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": _INSTALL_SKILL_TOOL_NAME,
        "description": (
            "Prepare installation metadata for one shared Codex skill, including the downloadable tar.gz bundle "
            "and safe install commands for ~/.codex/skills/<skill-name>."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Published skill name exactly as returned by mcp_proxy_list_skills.",
                    "minLength": 1,
                    "maxLength": 120,
                }
            },
            "required": ["skill_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": _CHECK_SKILL_UPDATES_TOOL_NAME,
        "description": (
            "Compare locally installed Codex skill metadata against the proxy catalog and report which skills are "
            "up to date, outdated, missing locally, or unknown to the catalog."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "installed": {
                    "type": "array",
                    "description": (
                        "Local installed skills to compare against the proxy catalog. "
                        "Provide content_sha256 when available; version is optional."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Installed skill name.",
                                "minLength": 1,
                                "maxLength": 120,
                            },
                            "version": {
                                "type": "string",
                                "description": "Optional local skill version string.",
                                "maxLength": 120,
                            },
                            "content_sha256": {
                                "type": "string",
                                "description": "Optional local deterministic content hash for the installed skill.",
                                "minLength": 8,
                                "maxLength": 128,
                            },
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["installed"],
            "additionalProperties": False,
        },
    },
    {
        "name": _REQUEST_SKILL_UPDATE_TOOL_NAME,
        "description": (
            "Submit a request to improve a published Codex skill when the shared instructions are missing rules, "
            "examples, or safety guidance needed by multiple users."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Published skill name exactly as returned by mcp_proxy_list_skills.",
                    "minLength": 1,
                    "maxLength": 120,
                },
                "summary": {
                    "type": "string",
                    "description": "Short description of the change requested for the shared skill.",
                    "minLength": 1,
                    "maxLength": 300,
                },
                "details": {
                    "type": "string",
                    "description": "Optional extra context, examples, or missing instructions.",
                    "maxLength": 4000,
                },
                "desired_outcome": {
                    "type": "string",
                    "description": "Optional statement of the preferred shared behavior after the update.",
                    "maxLength": 1000,
                },
            },
            "required": ["skill_name", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": _IMPROVEMENT_REQUEST_TOOL_NAME,
        "description": (
            "Submit a product improvement request for one or more MCP tools or upstream servers. "
            "Use this when a tool is missing capability, repeatedly errors, or a server should expose more tools. "
            "Include `tool_names` and/or `server_ids`, plus `error_context` when relevant. "
            "Requests are rate limited and reviewed in the MCP Proxy Approvals UI."
        ),
        "inputSchema": {
            "type": "object",
            "description": (
                "Provide `summary` plus at least one of `tool_names` or `server_ids`. "
                "The server validates that requirement at runtime."
            ),
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Short description of the improvement being requested.",
                    "minLength": 1,
                    "maxLength": 300,
                },
                "details": {
                    "type": "string",
                    "description": "Optional extra context, desired behavior, or concrete examples.",
                    "maxLength": 4000,
                },
                "tool_names": {
                    "type": "array",
                    "description": "Affected proxied tool names exactly as returned by tools/list.",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "server_ids": {
                    "type": "array",
                    "description": "Affected upstream server ids (for missing tools or broader server improvements).",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "error_context": {
                    "type": "object",
                    "description": "Optional recent failure details when the request comes from an error.",
                    "properties": {
                        "failed_tool_name": {"type": "string"},
                        "error_message": {"type": "string"},
                        "error_code": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": _SET_APP_SECRET_TOOL_NAME,
        "description": (
            "Set a write-only secret for a Host Wise managed app. MCP Proxy stores the value encrypted in "
            "Firestore and never returns the plaintext or ciphertext."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "name": {
                    "type": "string",
                    "description": "Environment-style secret name, e.g. API_TOKEN.",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "value": {
                    "type": "string",
                    "description": "Secret value. Write-only; never returned by tools or UI.",
                    "minLength": 1,
                },
            },
            "required": ["app_id", "name", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": _LIST_APP_SECRETS_TOOL_NAME,
        "description": "List secret metadata for a Host Wise managed app. Values are never returned.",
        "inputSchema": {
            "type": "object",
            "properties": {"app_id": {"type": "string", "minLength": 1, "maxLength": 64}},
            "required": ["app_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": _DELETE_APP_SECRET_TOOL_NAME,
        "description": "Delete one encrypted secret for a Host Wise managed app.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "name": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["app_id", "name"],
            "additionalProperties": False,
        },
    },
]


def _resolve_user_display_name(user: "UserInDB") -> str:
    """Derive a short display name from an authenticated user for workflow attribution tags."""
    if user.kind == "agent" and user.agent_name:
        return user.agent_name
    if user.email:
        local = user.email.split("@")[0] if "@" in user.email else user.email
        return local or user.id
    return user.id


def _local_tools_list() -> List[Dict[str, Any]]:
    return [dict(tool) for tool in _LOCAL_MCP_TOOLS]


def _tool_structured_content(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return JSON-RPC tool structuredContent when present."""
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    return None


def _sync_managed_n8n_workflow_record(
    *,
    user: "UserInDB",
    upstream_tool_name: str,
    arguments: Dict[str, Any],
    response_payload: Dict[str, Any],
) -> None:
    """Persist managed-workflow metadata after successful n8n create/delete tool calls."""
    try:
        structured = _tool_structured_content(response_payload) or {}
        if upstream_tool_name == "n8n_create_workflow":
            workflow_id = structured.get("id")
            name = structured.get("name") or arguments.get("name")
            summary = arguments.get("summary") or structured.get("summary")
            if workflow_id and isinstance(name, str) and name.strip() and isinstance(summary, str) and summary.strip():
                upsert_managed_workflow(
                    workflow_id=str(workflow_id),
                    name=name.strip(),
                    summary=summary.strip(),
                    editor_url=structured.get("editor_url"),
                    user=user,
                )
        elif upstream_tool_name == "n8n_delete_workflow":
            workflow_id = arguments.get("workflow_id") or structured.get("workflow_id")
            if workflow_id:
                mark_managed_workflow_deleted(str(workflow_id))
    except Exception:
        logger.exception("Failed to sync managed n8n workflow metadata for tool %s", upstream_tool_name)


def _sync_managed_app_record(
    *,
    user: "UserInDB",
    upstream_tool_name: str,
    arguments: Dict[str, Any],
    response_payload: Dict[str, Any],
) -> None:
    """Persist managed-app metadata after successful deploy/delete tool calls."""
    try:
        structured = _tool_structured_content(response_payload) or {}
        if upstream_tool_name in {"deploy_app", "complete_deployment"}:
            if structured.get("executed") is not True:
                return
            # Async `deploy_app` returns service_deployed=false until `complete_deployment` runs; still persist
            # a Firestore row (live / approved) so the Apps UI lists the app and shows build_id.
            async_build_pending = (
                upstream_tool_name == "deploy_app"
                and structured.get("async_build") is True
                and structured.get("build_submitted") is True
                and structured.get("service_deployed") is False
            )
            if structured.get("service_deployed") is False and not async_build_pending:
                return
            manifest = arguments.get("manifest") if isinstance(arguments.get("manifest"), dict) else {}
            normalized = (
                structured.get("normalized_manifest") if isinstance(structured.get("normalized_manifest"), dict) else {}
            )
            app = structured.get("app") if isinstance(structured.get("app"), dict) else {}

            def pick(key: str) -> Any:
                if key == "build_id" and structured.get("build_id"):
                    return structured.get("build_id")
                # Top-level success payloads also carry service_url / approved_url for the proxy
                if key in ("service_url", "approved_url"):
                    for bucket in (structured, app, normalized, manifest):
                        if not isinstance(bucket, dict):
                            continue
                        val = bucket.get(key)
                        if isinstance(val, str) and val.strip():
                            return val
                    return None
                return app.get(key) or normalized.get(key) or manifest.get(key)

            app_id = pick("app_id")
            summary = pick("summary")
            data_access_summary = pick("data_access_summary")
            service_name = pick("service_name")
            project_id = pick("project_id")
            region = pick("region")
            if (
                isinstance(app_id, str)
                and app_id.strip()
                and isinstance(summary, str)
                and summary.strip()
                and isinstance(data_access_summary, str)
                and data_access_summary.strip()
                and isinstance(service_name, str)
                and service_name.strip()
                and isinstance(project_id, str)
                and project_id.strip()
                and isinstance(region, str)
                and region.strip()
            ):
                data_connections = pick("data_connections")
                if not isinstance(data_connections, list):
                    data_connections = []
                upsert_managed_app(
                    app_id=app_id.strip(),
                    name=str(pick("name") or app_id).strip(),
                    summary=summary.strip(),
                    data_access_summary=data_access_summary.strip(),
                    data_connections=[item for item in data_connections if isinstance(item, dict)],
                    service_name=service_name.strip(),
                    service_url=pick("service_url") if isinstance(pick("service_url"), str) else None,
                    project_id=project_id.strip(),
                    region=region.strip(),
                    runtime_service_account=str(pick("runtime_service_account") or "").strip(),
                    framework=str(pick("framework") or "").strip(),
                    repo_url=str(pick("repo_url") or "").strip(),
                    approved_url=pick("approved_url") if isinstance(pick("approved_url"), str) else None,
                    version=str(pick("version") or "").strip(),
                    commit_sha=str(pick("commit_sha") or "").strip(),
                    build_id=str(pick("build_id") or "").strip(),
                    image_digest=str(pick("image_digest") or "").strip(),
                    cloud_run_revision=str(pick("cloud_run_revision") or "").strip(),
                    user=user,
                )
        elif upstream_tool_name == "delete_app":
            app_id = arguments.get("app_id")
            delete_mode = str(arguments.get("delete_mode") or "full_cleanup")
            if app_id:
                if delete_mode == "full_cleanup":
                    delete_app_secrets(str(app_id))
                mark_managed_app_deleted(str(app_id), delete_mode=delete_mode, status="deleted")
    except Exception:
        logger.exception("Failed to sync managed app metadata for tool %s", upstream_tool_name)


def _ensure_proxy_capabilities(merged: Dict[str, Any]) -> Dict[str, Any]:
    """Advertise proxy-local capabilities even when no upstream exposes them."""
    out = dict(merged or {})
    if "resources" not in out or not isinstance(out.get("resources"), dict):
        out["resources"] = {}
    if "tools" not in out or not isinstance(out.get("tools"), dict):
        out["tools"] = {}
    return out


def _bootstrap_instructions(*, allowed_server_ids: List[str]) -> str:
    lines = [
        "Start by reading mcp_proxy://help/index and mcp_proxy://help/capabilities.json to understand the proxy "
        "layout, routing, and tool usage patterns.",
        "Before using an unfamiliar proxied tool, read mcp_proxy://help/tool/{tool_name}.",
        "For shared Codex skills, use mcp_proxy_list_skills to browse the catalog, mcp_proxy_install_skill to fetch "
        "install metadata, and mcp_proxy_check_skill_updates to verify whether local installed skills match the latest "
        "catalog content.",
    ]
    if "n8n" in set(allowed_server_ids or []):
        lines.append(
            "Before calling n8n_* workflow tools, first read mcp_proxy://help/n8n and "
            "mcp_proxy://help/by-task/n8n-create-workflow, then fetch MCP prompt "
            "mcp_proxy_n8n_workflow_assistant via prompts/get."
        )
    if any(_is_cloud_run_deployer_server(sid) for sid in (allowed_server_ids or [])):
        lines.append(
            "Before calling cloud_run_deployer deploy/delete tools, read "
            "cloud-run-deployer://help/index and cloud-run-deployer://help/managed-dashboard-workflow, "
            "then use validate_deployment_manifest, plan_deployment, and deploy_app. Successful deploys apply Google "
            "sign-in (IAP) on the app URL. The MCP Proxy Apps page is for metadata and removing apps you no longer need."
        )
    if set(allowed_server_ids or []).intersection({"guesty", "zendesk", "breezeway", "sql", "firestore"}):
        lines.append(
            "For vague ops or booking questions across MCP tools, first read the relevant help pages and then fetch "
            "MCP prompt mcp_proxy_investigation_assistant via prompts/get."
        )
    return " ".join(lines)


def _enrich_tool_description(server_id: str, tool_name: str, description: str) -> str:
    desc = (description or "").strip()
    if _is_cloud_run_deployer_server(server_id) and tool_name in {
        "deploy_app",
        "complete_deployment",
        "approve_app",
        "delete_app",
    }:
        base = (
            " Proxy guidance: this is a write tool. Read `cloud-run-deployer://help/index`, "
            "`cloud-run-deployer://help/managed-dashboard-workflow`, and `cloud-run-deployer://safety/checklist` "
            "before use. `deploy_app` requires a useful summary or description. A successful production deploy "
            "enables the Run URL with Google sign-in (IAP). The Apps UI is for metadata and `delete_app` (full "
            "cleanup) when you need to remove an app."
        )
        if base.strip() in desc:
            return desc
        return (desc + base).strip()
    if server_id != "n8n" or not tool_name.startswith("n8n_"):
        return desc
    base = " Proxy guidance: read `mcp_proxy://help/n8n` and " f"`mcp_proxy://help/tool/{tool_name}` before use."
    authoring_tools = {
        "n8n_find_workflow_by_name",
        "n8n_get_workflow",
        "n8n_get_workflows",
        "n8n_validate_workflow_definition",
        "n8n_get_credentials",
        "n8n_get_credential_schema",
        "n8n_create_credential",
        "n8n_create_workflow",
        "n8n_update_workflow",
        "n8n_list_nodes",
        "n8n_list_node_files",
        "n8n_get_node_source",
        "n8n_find_node_by_name_or_capability",
        "n8n_search_nodes",
    }
    if tool_name in authoring_tools:
        base += (
            " For workflow authoring/editing, first fetch MCP prompt "
            "`mcp_proxy_n8n_workflow_assistant` and follow `mcp_proxy://help/by-task/n8n-create-workflow`."
        )
    if base.strip() in desc:
        return desc
    return (desc + base).strip()


def _validate_schedule_trigger_frequency(node: Dict[str, Any]) -> Optional[str]:
    """Reject obvious schedule-trigger intervals that violate the >=2h proxy policy."""
    if not isinstance(node, dict):
        return None
    if node.get("type") != "n8n-nodes-base.scheduleTrigger":
        return None
    parameters = node.get("parameters")
    if not isinstance(parameters, dict):
        return None
    rule = parameters.get("rule")
    if not isinstance(rule, dict):
        return None
    intervals = rule.get("interval")
    if not isinstance(intervals, list):
        return None
    node_name = str(node.get("name") or node.get("id") or "schedule trigger")
    for interval in intervals:
        if not isinstance(interval, dict):
            continue
        field = str(interval.get("field") or "").strip().lower()
        if field in {"minute", "minutes"}:
            return (
                f"n8n schedule trigger `{node_name}` uses minute-based intervals. "
                "The proxy blocks schedules under 2 hours."
            )
        if field in {"hour", "hours"}:
            hours_value = interval.get("hoursInterval")
            try:
                hours_interval = int(hours_value)
            except (TypeError, ValueError):
                continue
            if hours_interval < 2:
                return (
                    f"n8n schedule trigger `{node_name}` uses hoursInterval={hours_interval}. "
                    "The proxy blocks schedules under 2 hours."
                )
    return None


def _validate_n8n_workflow_write_arguments(tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
    """Proxy-side enforcement for objective n8n workflow authoring rules."""
    if tool_name == "n8n_create_workflow":
        summary = arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return "n8n_create_workflow requires a non-empty `summary`."
    nodes = arguments.get("nodes")
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        violation = _validate_schedule_trigger_frequency(node)
        if violation:
            return violation
    return None


def _validate_cloud_run_deployer_arguments(tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
    """Proxy-side enforcement for managed app deploy metadata."""
    if tool_name not in {"deploy_app", "complete_deployment"}:
        return None
    manifest = arguments.get("manifest")
    if isinstance(manifest, dict):
        summary = manifest.get("summary") or manifest.get("description")
        if not isinstance(summary, str) or not summary.strip():
            return f"{tool_name} requires manifest.summary describing what the app does."
        return None
    if tool_name == "deploy_app":
        source_path = arguments.get("source_path")
        if not isinstance(source_path, str) or not source_path.strip():
            return "deploy_app requires source_path or a manifest object."
        summary = arguments.get("description") or arguments.get("name") or arguments.get("app_id")
        if not isinstance(summary, str) or not summary.strip():
            return "deploy_app requires description, name, or app_id describing what the app does."
        return None
    return f"{tool_name} requires a manifest object."
    return None


def _tool_result_response(req_id: Any, *, text: str, structured: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
        },
    }


def _summarize_tool_arguments(arguments: Any) -> Dict[str, Any]:
    """Return audit-safe tool argument metadata without logging raw values or secrets."""
    if arguments is None:
        return {
            "argument_type": "null",
            "argument_count": 0,
            "argument_keys": [],
            "has_arguments": False,
        }
    if isinstance(arguments, dict):
        keys = sorted(str(k) for k in arguments.keys())
        return {
            "argument_type": "object",
            "argument_count": len(arguments),
            "argument_keys": keys[:50],
            "has_arguments": bool(arguments),
        }
    if isinstance(arguments, list):
        return {
            "argument_type": "array",
            "argument_count": len(arguments),
            "has_arguments": bool(arguments),
        }
    return {
        "argument_type": type(arguments).__name__,
        "has_arguments": True,
    }


def _log_tool_call_audit(
    *,
    user: UserInDB,
    req_id: Any,
    tool_name: str,
    upstream_tool_name: Optional[str],
    server_id: Optional[str],
    arguments: Any,
    result: str,
    reason: str,
    started_at: float,
    error_code: Optional[int] = None,
    upstream_error_detail_present: Optional[bool] = None,
) -> None:
    timestamp = datetime.now(timezone.utc)
    extra: Dict[str, Any] = {
        "user_kind": user.kind,
        "tool_name": tool_name or None,
        "upstream_tool_name": upstream_tool_name,
        "server_id": server_id,
        "reason": reason,
        "latency_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
        **_summarize_tool_arguments(arguments),
    }
    if server_id and upstream_tool_name:
        extra["write_tool"] = upstream_tool_requires_write(server_id, upstream_tool_name)
    if error_code is not None:
        extra["error_code"] = error_code
    if upstream_error_detail_present is not None:
        extra["upstream_error_detail_present"] = upstream_error_detail_present
    log_audit(
        "mcp_tool_call",
        user_id=user.id,
        user_email=user.email or None,
        role=user.role,
        result=result,
        request_id=str(req_id) if req_id is not None else None,
        **extra,
    )
    record_tool_call(
        user.id,
        tool_name=tool_name or upstream_tool_name,
        server_id=server_id,
        result=result,
        timestamp=timestamp,
    )


def resolve_allowed_server_ids(user: UserInDB) -> List[str]:
    """Enabled upstream server IDs this user may connect to (admin/power_user: all enabled)."""
    if not user or user.status != "active":
        return []
    if user.role in ("admin", "power_user"):
        return [s.id for s in list_servers(enabled_only=True)]
    return [p.server_id for p in get_user_permissions(user.id) if p.read or p.write]


def user_may_invoke_upstream_tool(
    user: UserInDB,
    server_id: str,
    upstream_tool_name: str,
    *,
    perms_by_server: Optional[Dict[str, PermissionItem]] = None,
) -> bool:
    """Regular users: read tools need read or write; registered write tools need write. Admin/power_user: all."""
    if not user or user.status != "active":
        return False
    if user.role in ("admin", "power_user"):
        return True
    perms_map = perms_by_server
    if perms_map is None:
        perms_map = {p.server_id: p for p in get_user_permissions(user.id)}
    p = perms_map.get(server_id)
    if not p:
        return False
    if upstream_tool_requires_write(server_id, upstream_tool_name):
        return bool(p.write)
    return bool(p.read or p.write)


# Session store: proxy_session_id -> { server_id: upstream_session_id }
_sessions: Dict[str, Dict[str, str]] = {}
SESSION_TTL_SECONDS = 30 * 60  # 30 minutes


def _get_enabled_servers() -> List[Any]:
    """Get list of enabled upstream servers from Firestore."""
    try:
        return list_servers(enabled_only=True)
    except Exception as e:
        logger.warning(
            "Failed to list servers from Firestore (project=%s, database=%s): %s",
            config.GCP_PROJECT_ID,
            config.MCP_PROXY_DATABASE,
            e,
        )
        return []


def _normalize_id(server_id: str) -> str:
    """Normalize server ID for use in prefixes (e.g. sql-gateway -> sql_gateway)."""
    return server_id.replace("-", "_")


def _is_cloud_run_deployer_server(server_id: str) -> bool:
    """True for the Cloud Run deployer upstream whether Firestore id uses hyphens or underscores."""
    return _normalize_id(server_id) == "cloud_run_deployer"


# Firestore server ids whose upstreams already name tools `{normalized_id}_*` (avoid n8n_n8n_*).
_SERVERS_WITH_SELF_PREFIXED_TOOLS = frozenset(
    {"n8n", "zendesk", "stripe", "breezeway", "moloni", "pipedrive", "absence"}
)


def _prefix_tool(server_id: str, tool_name: str) -> str:
    norm = _normalize_id(server_id)
    if server_id in _SERVERS_WITH_SELF_PREFIXED_TOOLS and tool_name.startswith(f"{norm}_"):
        return tool_name
    return f"{norm}_{tool_name}"


def _prefix_resource(server_id: str, uri: str) -> str:
    path = uri.replace("://", "/", 1) if "://" in uri else uri
    return f"{_normalize_id(server_id)}://{path}"


def _prefix_prompt(server_id: str, name: str) -> str:
    norm = _normalize_id(server_id)
    if server_id in _SERVERS_WITH_SELF_PREFIXED_TOOLS and name.startswith(f"{norm}_"):
        return name
    return f"{norm}_{name}"


def _parse_prefixed_name(prefixed: str, servers: List[Any]) -> Optional[Tuple[str, str]]:
    """Parse 'server_id_toolname' or 'server_id_promptname' -> (server_id, original_name)."""
    for s in sorted(servers, key=lambda x: len(_normalize_id(x.id)), reverse=True):
        norm = _normalize_id(s.id)
        prefix = norm + "_"
        if not prefixed.startswith(prefix):
            continue
        if s.id in _SERVERS_WITH_SELF_PREFIXED_TOOLS:
            return (s.id, prefixed)
        return (s.id, prefixed[len(prefix) :])
    return None


def _resolve_tool_alias(tool_name: str) -> str:
    """Map proxy-local shorthand aliases to canonical public tool names."""
    return _TOOL_ALIASES.get(tool_name, tool_name)


def _build_tool_alias_descriptors(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expose selected shorthand aliases in tools/list without changing upstream tool contracts."""
    by_name = {str(t.get("name", "")): t for t in tools if isinstance(t, dict)}
    aliases: List[Dict[str, Any]] = []
    for alias_name, canonical_name in _TOOL_ALIASES.items():
        target = by_name.get(canonical_name)
        if not target or alias_name in by_name:
            continue
        alias_tool = dict(target)
        alias_tool["name"] = alias_name
        desc = str(target.get("description", "")).strip()
        alias_tool["description"] = f"Alias of `{canonical_name}`. {desc}" if desc else f"Alias of `{canonical_name}`."
        aliases.append(alias_tool)
    return aliases


def _parse_prefixed_uri(prefixed: str, servers: List[Any]) -> Optional[Tuple[str, str]]:
    """Parse 'server_id://schema/models' -> (server_id, 'schema://models')."""
    for s in servers:
        prefix = f"{_normalize_id(s.id)}://"
        if prefixed.startswith(prefix):
            rest = prefixed[len(prefix) :]
            original = rest.replace("/", "://", 1) if "/" in rest else rest
            return (s.id, original)
    return None


def _sanitize_merged_capabilities(merged: Dict[str, Any]) -> Dict[str, Any]:
    """Omit resource subscription flags: proxy SSE does not relay upstream resource updates."""
    if not merged:
        return merged
    out = dict(merged)
    res = out.get("resources")
    if isinstance(res, dict):
        r = {k: v for k, v in res.items() if k not in ("subscribe", "listChanged")}
        if r:
            out["resources"] = r
        else:
            del out["resources"]
    return out


async def _forward_to_upstream(
    server_id: str,
    url: str,
    headers: Dict[str, str],
    session_id: Optional[str],
    method: str,
    params: Optional[Dict[str, Any]] = None,
    id_val: Any = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Forward a JSON-RPC request to an upstream. Returns (response_body, new_session_id, error_detail)."""
    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "id": id_val,
    }
    if params is not None:
        payload["params"] = params
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **headers,
    }
    if session_id:
        req_headers["Mcp-Session-Id"] = session_id
    logger.debug(
        "Upstream request %s %s %s: payload=%s",
        server_id,
        method,
        url,
        payload,
    )
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers=req_headers)
            logger.debug(
                "Upstream response %s %s: status=%s body=%r",
                server_id,
                method,
                resp.status_code,
                resp.text[:500] if resp.text else "(empty)",
            )
            data = parse_mcp_response(resp.text)
            new_sid = resp.headers.get("Mcp-Session-Id")
            if resp.is_success:
                return (data, new_sid, None)
            # 4xx/5xx: try to surface upstream error
            detail = None
            if data and isinstance(data.get("error"), dict):
                err = data["error"]
                detail = err.get("data") or err.get("message") or resp.text
            else:
                detail = (resp.text and resp.text.strip())[:500] or f"HTTP {resp.status_code}"
            logger.warning("Upstream %s %s: %s", server_id, method, detail)
            return (None, None, detail)
    except httpx.HTTPStatusError as e:
        detail = None
        if e.response is not None:
            try:
                data = parse_mcp_response(e.response.text)
                if data and isinstance(data.get("error"), dict):
                    err = data["error"]
                    detail = err.get("data") or err.get("message")
            except Exception:
                pass
            if detail is None:
                detail = (e.response.text and e.response.text.strip())[:500] or str(e)
        else:
            detail = str(e)
        logger.warning("Upstream %s failed for %s: %s", server_id, method, detail)
        return (None, None, detail)
    except Exception as e:
        detail = str(e)
        logger.warning("Upstream %s failed for %s: %s", server_id, method, detail)
        return (None, None, detail)


async def _handle_initialize(
    body: Dict[str, Any],
    user_id: str,
    allowed_server_ids: Optional[List[str]],
) -> Tuple[Dict[str, Any], str]:
    """Initialize with allowed upstreams and create proxy session bound to user_id."""
    params = body.get("params", {})
    req_id = body.get("id")
    servers = _get_enabled_servers()
    if allowed_server_ids is not None:
        allow = set(allowed_server_ids)
        servers = [s for s in servers if s.id in allow]
    if not servers:
        proxy_session_id = str(uuid.uuid4())
        _sessions[proxy_session_id] = {"user_id": user_id, "servers": {}}
        return (
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": _ensure_proxy_capabilities({"resources": {}}),
                    "instructions": _bootstrap_instructions(allowed_server_ids=allowed_server_ids or []),
                    "serverInfo": {"name": "mcp-proxy", "version": "0.1.0"},
                },
            },
            proxy_session_id,
        )
    upstream_sessions: Dict[str, str] = {}
    merged_caps: Dict[str, Any] = {}
    merged_info = {"name": "mcp-proxy", "version": "0.1.0"}
    for s in servers:
        try:
            headers = await resolve_upstream_headers(s, user_id=user_id)
        except UpstreamOAuthNotConnectedError:
            logger.info("Initialize skip %s: OAuth not linked for user", s.id)
            continue
        except ValueError as e:
            logger.warning("Initialize skip %s: %s", s.id, e)
            continue
        data, sid, _ = await _forward_to_upstream(s.id, s.url, headers, None, "initialize", params, req_id)
        if data and "result" in data:
            # Include server even without session ID (e.g. NocoDB/SSE servers)
            upstream_sessions[s.id] = sid or ""
            if not sid:
                logger.debug("Upstream %s returned no Mcp-Session-Id (may use stateless/SSE)", s.id)
            res = data["result"]
            if isinstance(res.get("capabilities"), dict):
                for k, v in res["capabilities"].items():
                    if k not in merged_caps:
                        merged_caps[k] = v
    merged_caps = _ensure_proxy_capabilities(_sanitize_merged_capabilities(merged_caps))
    proxy_session_id = str(uuid.uuid4())
    _sessions[proxy_session_id] = {"user_id": user_id, "servers": upstream_sessions}
    return (
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": merged_caps,
                "instructions": _bootstrap_instructions(allowed_server_ids=allowed_server_ids or []),
                "serverInfo": merged_info,
            },
        },
        proxy_session_id,
    )


async def _handle_tools_list(
    body: Dict[str, Any],
    sessions: Dict[str, str],
    *,
    user: UserInDB,
) -> Dict[str, Any]:
    """Fan-out tools/list, merge and prefix tool names; omit write-classified tools if user lacks write."""
    req_id = body.get("id")
    params = body.get("params") or {}
    servers = _get_enabled_servers()
    server_map = {s.id: s for s in servers}
    perms_by_server: Optional[Dict[str, PermissionItem]] = None
    if user.role not in ("admin", "power_user"):
        perms_by_server = {p.server_id: p for p in get_user_permissions(user.id)}
    all_tools: List[Dict[str, Any]] = _local_tools_list()
    for server_id, upstream_sid in sessions.items():
        s = server_map.get(server_id)
        if not s:
            continue
        try:
            headers = await resolve_upstream_headers(s, user_id=user.id)
        except UpstreamOAuthNotConnectedError:
            continue
        except ValueError:
            continue
        data, _, _ = await _forward_to_upstream(server_id, s.url, headers, upstream_sid, "tools/list", params, req_id)
        if data and "result" in data:
            tools = data["result"].get("tools", [])
            for t in tools:
                name = t.get("name", "")
                if not user_may_invoke_upstream_tool(user, server_id, name, perms_by_server=perms_by_server):
                    continue
                t = dict(t)
                t["description"] = _enrich_tool_description(server_id, name, str(t.get("description", "")))
                t["name"] = _prefix_tool(server_id, name)
                all_tools.append(t)
    all_tools.extend(_build_tool_alias_descriptors(all_tools))
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"tools": all_tools},
    }


def _canonical_server_ids_for_request(server_ids: List[str], servers: List[Any]) -> List[str]:
    mapping: Dict[str, str] = {}
    for server in servers:
        mapping[server.id] = server.id
        mapping[_normalize_id(server.id)] = server.id
    resolved: List[str] = []
    seen: set[str] = set()
    for raw in server_ids:
        canonical = mapping.get(raw) or mapping.get(_normalize_id(raw))
        if not canonical:
            raise ValueError(f"Unknown server id: {raw}")
        if canonical in seen:
            continue
        seen.add(canonical)
        resolved.append(canonical)
    return resolved


def _validate_improvement_targets(
    *,
    user: UserInDB,
    payload: ImprovementRequestCreate,
    servers: List[Any],
) -> ImprovementRequestCreate:
    allowed_server_ids = set(resolve_allowed_server_ids(user))
    canonical_server_ids = _canonical_server_ids_for_request(payload.server_ids, servers)
    inferred_server_ids: List[str] = list(canonical_server_ids)
    for tool_name in payload.tool_names:
        parsed = _parse_prefixed_name(tool_name, servers)
        if not parsed:
            raise ValueError(f"Unknown tool: {tool_name}")
        server_id, upstream_tool_name = parsed
        if user.role not in ("admin", "power_user") and (
            server_id not in allowed_server_ids
            or not user_may_invoke_upstream_tool(user, server_id, upstream_tool_name)
        ):
            raise ValueError(f"Tool not available to this user: {tool_name}")
        if server_id not in inferred_server_ids:
            inferred_server_ids.append(server_id)
    if user.role not in ("admin", "power_user"):
        for server_id in canonical_server_ids:
            if server_id not in allowed_server_ids:
                raise ValueError(f"Server not available to this user: {server_id}")
    return ImprovementRequestCreate(
        summary=payload.summary,
        details=payload.details,
        tool_names=payload.tool_names,
        server_ids=inferred_server_ids,
        error_context=payload.error_context,
    )


def _handle_local_improvement_request_tool(
    *,
    req_id: Any,
    arguments: Any,
    user: UserInDB,
) -> Dict[str, Any]:
    try:
        payload = ImprovementRequestCreate.model_validate(arguments or {})
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    servers = _get_enabled_servers()
    validated = _validate_improvement_targets(user=user, payload=payload, servers=servers)
    request = create_improvement_request(user=user, payload=validated)
    text = (
        f"Improvement request submitted as `{request.id}` for "
        f"{len(request.tool_names)} tool(s) across {len(request.server_ids)} server(s)."
    )
    return _tool_result_response(
        req_id,
        text=text,
        structured={
            "request_id": request.id,
            "status": request.status,
            "summary": request.summary,
            "tool_names": request.tool_names,
            "server_ids": request.server_ids,
            "created_at": request.created_at.isoformat() if request.created_at else None,
        },
    )


def _handle_local_list_skills_tool(*, req_id: Any, arguments: Any) -> Dict[str, Any]:
    if arguments is not None and not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    include_files = bool((arguments or {}).get("include_files"))
    skills = [skill_manifest(skill, include_files=include_files) for skill in list_catalog_skills()]
    return _tool_result_response(
        req_id,
        text=f"{len(skills)} shared skill(s) available from the MCP proxy catalog.",
        structured={
            "count": len(skills),
            "skills": skills,
            "install_tool": _INSTALL_SKILL_TOOL_NAME,
            "check_updates_tool": _CHECK_SKILL_UPDATES_TOOL_NAME,
            "request_update_tool": _REQUEST_SKILL_UPDATE_TOOL_NAME,
        },
    )


def _handle_local_install_skill_tool(*, req_id: Any, arguments: Any) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    skill_name = str(arguments.get("skill_name") or "").strip()
    if not skill_name:
        raise ValueError("skill_name is required")
    manifest = build_install_manifest(skill_name)
    if manifest is None:
        raise ValueError(f"Unknown shared skill: {skill_name}")
    return _tool_result_response(
        req_id,
        text=f"Installation bundle prepared for `{skill_name}`. Restart Codex after installing it.",
        structured=manifest,
    )


def _handle_local_check_skill_updates_tool(*, req_id: Any, arguments: Any) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    installed = arguments.get("installed")
    if not isinstance(installed, list):
        raise ValueError("installed must be an array")

    catalog = {skill.name: skill for skill in list_catalog_skills()}
    results: List[Dict[str, Any]] = []
    outdated_count = 0

    for entry in installed:
        if not isinstance(entry, dict):
            raise ValueError("installed entries must be objects")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError("installed entry name is required")
        local_version_raw = entry.get("version")
        local_version = str(local_version_raw).strip() if local_version_raw is not None else None
        local_sha_raw = entry.get("content_sha256")
        local_sha = str(local_sha_raw).strip() if local_sha_raw is not None else None

        skill = catalog.get(name)
        if skill is None:
            results.append(
                {
                    "name": name,
                    "present_in_catalog": False,
                    "status": "unknown_skill",
                    "outdated": None,
                    "reason": "not_in_catalog",
                    "local": {"version": local_version, "content_sha256": local_sha},
                }
            )
            continue

        latest = skill_manifest(skill, include_files=False)
        reason = "up_to_date"
        status = "up_to_date"
        outdated = False
        latest_version = latest.get("version")
        latest_sha = latest.get("content_sha256")

        if local_sha and latest_sha and local_sha != latest_sha:
            reason = "content_sha256_mismatch"
            status = "outdated"
            outdated = True
        elif local_version is not None and latest_version is not None and local_version != latest_version:
            reason = "version_mismatch"
            status = "outdated"
            outdated = True
        elif local_sha is None and local_version is None:
            reason = "missing_local_metadata"
            status = "unknown"
            outdated = None

        if outdated:
            outdated_count += 1

        results.append(
            {
                "name": name,
                "present_in_catalog": True,
                "status": status,
                "outdated": outdated,
                "reason": reason,
                "local": {"version": local_version, "content_sha256": local_sha},
                "latest": latest,
            }
        )

    text = f"Checked {len(results)} installed skill(s); {outdated_count} need update."
    return _tool_result_response(
        req_id,
        text=text,
        structured={
            "count": len(results),
            "outdated_count": outdated_count,
            "skills": results,
            "install_tool": _INSTALL_SKILL_TOOL_NAME,
        },
    )


def _handle_local_skill_update_request_tool(
    *,
    req_id: Any,
    arguments: Any,
    user: UserInDB,
) -> Dict[str, Any]:
    try:
        payload = SkillUpdateRequestCreate.model_validate(arguments or {})
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    if get_catalog_skill(payload.skill_name) is None:
        raise ValueError(f"Unknown shared skill: {payload.skill_name}")
    request = create_skill_update_request(user=user, payload=payload)
    return _tool_result_response(
        req_id,
        text=f"Skill update request submitted as `{request.id}` for `{request.skill_name}`.",
        structured={
            "request_id": request.id,
            "skill_name": request.skill_name,
            "status": request.status,
            "summary": request.summary,
            "created_at": request.created_at.isoformat() if request.created_at else None,
        },
    )


def _require_managed_app_secret_access(app_id: str, user: UserInDB) -> None:
    record = get_managed_app(app_id)
    if not record or record.status == "deleted":
        raise ValueError("Managed app not found")
    if user.role in ("admin", "power_user"):
        return
    if record.created_by_user_id == user.id:
        return
    raise ValueError("Not permitted for this managed app")


def _secret_metadata_payload(item: Any) -> Dict[str, Any]:
    return {
        "app_id": item.app_id,
        "name": item.name,
        "key_version": item.key_version,
        "created_by_user_id": item.created_by_user_id,
        "updated_by_user_id": item.updated_by_user_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _handle_local_set_app_secret_tool(*, req_id: Any, arguments: Any, user: UserInDB) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    app_id = str(arguments.get("app_id") or "").strip()
    name = str(arguments.get("name") or "").strip()
    value = arguments.get("value")
    if not app_id:
        raise ValueError("app_id is required")
    if not name:
        raise ValueError("name is required")
    if value is None or str(value) == "":
        raise ValueError("value is required")
    _require_managed_app_secret_access(app_id, user)
    item = set_app_secret(app_id=app_id, name=name, value=str(value), user=user)
    return _tool_result_response(
        req_id,
        text=f"Secret `{item.name}` stored for app `{app_id}`. The value is write-only.",
        structured={"ok": True, "secret": _secret_metadata_payload(item)},
    )


def _handle_local_list_app_secrets_tool(*, req_id: Any, arguments: Any, user: UserInDB) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    app_id = str(arguments.get("app_id") or "").strip()
    if not app_id:
        raise ValueError("app_id is required")
    _require_managed_app_secret_access(app_id, user)
    secrets = [_secret_metadata_payload(item) for item in list_app_secrets(app_id)]
    return _tool_result_response(
        req_id,
        text=f"{len(secrets)} secret(s) stored for app `{app_id}`.",
        structured={"ok": True, "app_id": app_id, "secrets": secrets},
    )


def _handle_local_delete_app_secret_tool(*, req_id: Any, arguments: Any, user: UserInDB) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    app_id = str(arguments.get("app_id") or "").strip()
    name = str(arguments.get("name") or "").strip()
    if not app_id:
        raise ValueError("app_id is required")
    if not name:
        raise ValueError("name is required")
    _require_managed_app_secret_access(app_id, user)
    existed = delete_app_secret(app_id=app_id, name=name)
    return _tool_result_response(
        req_id,
        text=f"Secret `{name}` deleted for app `{app_id}`.",
        structured={"ok": True, "app_id": app_id, "secret_name": name, "deleted": True, "existed": existed},
    )


def _handle_local_tool_call(
    *,
    req_id: Any,
    name: str,
    arguments: Any,
    user: UserInDB,
) -> Dict[str, Any]:
    if name == _LIST_SKILLS_TOOL_NAME:
        return _handle_local_list_skills_tool(req_id=req_id, arguments=arguments)
    if name == _INSTALL_SKILL_TOOL_NAME:
        return _handle_local_install_skill_tool(req_id=req_id, arguments=arguments)
    if name == _CHECK_SKILL_UPDATES_TOOL_NAME:
        return _handle_local_check_skill_updates_tool(req_id=req_id, arguments=arguments)
    if name == _REQUEST_SKILL_UPDATE_TOOL_NAME:
        return _handle_local_skill_update_request_tool(req_id=req_id, arguments=arguments, user=user)
    if name == _IMPROVEMENT_REQUEST_TOOL_NAME:
        return _handle_local_improvement_request_tool(req_id=req_id, arguments=arguments, user=user)
    if name == _SET_APP_SECRET_TOOL_NAME:
        return _handle_local_set_app_secret_tool(req_id=req_id, arguments=arguments, user=user)
    if name == _LIST_APP_SECRETS_TOOL_NAME:
        return _handle_local_list_app_secrets_tool(req_id=req_id, arguments=arguments, user=user)
    if name == _DELETE_APP_SECRET_TOOL_NAME:
        return _handle_local_delete_app_secret_tool(req_id=req_id, arguments=arguments, user=user)
    raise ValueError(f"Unknown local tool: {name}")


def _local_tool_audit_reason(name: str) -> str:
    if name == _LIST_SKILLS_TOOL_NAME:
        return "listed_skills"
    if name == _INSTALL_SKILL_TOOL_NAME:
        return "prepared_skill_install"
    if name == _CHECK_SKILL_UPDATES_TOOL_NAME:
        return "checked_skill_updates"
    if name == _REQUEST_SKILL_UPDATE_TOOL_NAME:
        return "submitted_skill_update_request"
    if name == _IMPROVEMENT_REQUEST_TOOL_NAME:
        return "submitted_improvement_request"
    if name == _SET_APP_SECRET_TOOL_NAME:
        return "set_managed_app_secret"
    if name == _LIST_APP_SECRETS_TOOL_NAME:
        return "listed_managed_app_secrets"
    if name == _DELETE_APP_SECRET_TOOL_NAME:
        return "deleted_managed_app_secret"
    return "completed"


async def _handle_tools_call(
    body: Dict[str, Any],
    sessions: Dict[str, str],
    *,
    user: UserInDB,
) -> Dict[str, Any]:
    """Route tools/call to the correct upstream by prefixed name."""
    started_at = time.perf_counter()
    req_id = body.get("id")
    params = body.get("params") or {}
    name = params.get("name", "")
    arguments = params.get("arguments")
    if name in {
        _LIST_SKILLS_TOOL_NAME,
        _INSTALL_SKILL_TOOL_NAME,
        _CHECK_SKILL_UPDATES_TOOL_NAME,
        _REQUEST_SKILL_UPDATE_TOOL_NAME,
        _IMPROVEMENT_REQUEST_TOOL_NAME,
        _SET_APP_SECRET_TOOL_NAME,
        _LIST_APP_SECRETS_TOOL_NAME,
        _DELETE_APP_SECRET_TOOL_NAME,
    }:
        try:
            result = _handle_local_tool_call(req_id=req_id, name=name, arguments=arguments, user=user)
        except (ImprovementRequestRateLimitError, SkillUpdateRequestRateLimitError) as exc:
            _log_tool_call_audit(
                user=user,
                req_id=req_id,
                tool_name=name,
                upstream_tool_name=None,
                server_id=None,
                arguments=arguments,
                result="error",
                reason="rate_limited",
                started_at=started_at,
                error_code=-32003,
            )
            detail = str(exc)
            if exc.retry_after_seconds is not None:
                detail = f"{detail} (retry_after_seconds={exc.retry_after_seconds})"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32003, "message": detail},
            }
        except ValueError as exc:
            _log_tool_call_audit(
                user=user,
                req_id=req_id,
                tool_name=name,
                upstream_tool_name=None,
                server_id=None,
                arguments=arguments,
                result="error",
                reason="invalid_arguments",
                started_at=started_at,
                error_code=-32602,
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": str(exc)},
            }
        _log_tool_call_audit(
            user=user,
            req_id=req_id,
            tool_name=name,
            upstream_tool_name=None,
            server_id=None,
            arguments=arguments,
            result="success",
            reason=_local_tool_audit_reason(name),
            started_at=started_at,
        )
        return result
    servers = _get_enabled_servers()
    resolved_name = _resolve_tool_alias(name)
    parsed = _parse_prefixed_name(resolved_name, servers)
    if not parsed:
        _log_tool_call_audit(
            user=user,
            req_id=req_id,
            tool_name=name,
            upstream_tool_name=None,
            server_id=None,
            arguments=arguments,
            result="error",
            reason="unknown_tool",
            started_at=started_at,
            error_code=-32602,
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Unknown tool: {name}"},
        }
    server_id, original_name = parsed
    if not user_may_invoke_upstream_tool(user, server_id, original_name):
        log_audit(
            "mcp_tool_denied",
            user_id=user.id,
            result="denied",
            details={"server_id": server_id, "tool": original_name},
        )
        _log_tool_call_audit(
            user=user,
            req_id=req_id,
            tool_name=name,
            upstream_tool_name=original_name,
            server_id=server_id,
            arguments=arguments,
            result="denied",
            reason="permission_denied",
            started_at=started_at,
            error_code=-32003,
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32003,
                "message": (
                    "Write permission required for this tool"
                    if upstream_tool_requires_write(server_id, original_name)
                    else "Not permitted for this server or tool"
                ),
            },
        }
    if server_id not in sessions:
        _log_tool_call_audit(
            user=user,
            req_id=req_id,
            tool_name=name,
            upstream_tool_name=original_name,
            server_id=server_id,
            arguments=arguments,
            result="error",
            reason="no_upstream_session",
            started_at=started_at,
            error_code=-32602,
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"No session for server: {server_id}"},
        }
    upstream_sid = sessions.get(server_id) or ""
    s = next((x for x in servers if x.id == server_id), None)
    if not s:
        _log_tool_call_audit(
            user=user,
            req_id=req_id,
            tool_name=name,
            upstream_tool_name=original_name,
            server_id=server_id,
            arguments=arguments,
            result="error",
            reason="server_not_found",
            started_at=started_at,
            error_code=-32602,
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Server not found: {server_id}"},
        }
    call_params = dict(params)
    call_params["name"] = original_name
    # Upstream MCP servers expect arguments to be an object; default to {} when missing/null
    if call_params.get("arguments") is None:
        call_params["arguments"] = {}
    if not isinstance(call_params.get("arguments"), dict):
        call_params["arguments"] = {}
    # Inject caller identity for n8n workflow attribution tags (transparent to the AI)
    if server_id == "n8n" and original_name in _N8N_USER_ATTRIBUTION_TOOLS:
        if not call_params["arguments"].get("user_name"):
            call_params["arguments"]["user_name"] = _resolve_user_display_name(user)
        violation = _validate_n8n_workflow_write_arguments(original_name, call_params["arguments"])
        if violation:
            _log_tool_call_audit(
                user=user,
                req_id=req_id,
                tool_name=name,
                upstream_tool_name=original_name,
                server_id=server_id,
                arguments=call_params.get("arguments"),
                result="error",
                reason="n8n_policy_violation",
                started_at=started_at,
                error_code=-32602,
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": violation},
            }
    if _is_cloud_run_deployer_server(server_id) and original_name in {
        "deploy_app",
        "complete_deployment",
        "approve_app",
        "delete_app",
    }:
        violation = _validate_cloud_run_deployer_arguments(original_name, call_params["arguments"])
        if violation:
            _log_tool_call_audit(
                user=user,
                req_id=req_id,
                tool_name=name,
                upstream_tool_name=original_name,
                server_id=server_id,
                arguments=call_params.get("arguments"),
                result="error",
                reason="managed_app_policy_violation",
                started_at=started_at,
                error_code=-32602,
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": violation},
            }
    try:
        headers = await resolve_upstream_headers(s, user_id=user.id)
    except UpstreamOAuthNotConnectedError:
        _log_tool_call_audit(
            user=user,
            req_id=req_id,
            tool_name=name,
            upstream_tool_name=original_name,
            server_id=server_id,
            arguments=call_params.get("arguments"),
            result="error",
            reason="oauth_not_connected",
            started_at=started_at,
            error_code=-32003,
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32003,
                "message": f"OAuth not connected for server {server_id}; link your account in the proxy web UI",
            },
        }
    except ValueError as e:
        _log_tool_call_audit(
            user=user,
            req_id=req_id,
            tool_name=name,
            upstream_tool_name=original_name,
            server_id=server_id,
            arguments=call_params.get("arguments"),
            result="error",
            reason="invalid_upstream_headers",
            started_at=started_at,
            error_code=-32603,
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": str(e)},
        }
    data, new_sid, err_detail = await _forward_to_upstream(
        server_id, s.url, headers, upstream_sid, "tools/call", call_params, req_id
    )
    if new_sid:
        sessions[server_id] = new_sid
    if not data:
        _log_tool_call_audit(
            user=user,
            req_id=req_id,
            tool_name=name,
            upstream_tool_name=original_name,
            server_id=server_id,
            arguments=call_params.get("arguments"),
            result="error",
            reason="upstream_transport_error",
            started_at=started_at,
            error_code=-32603,
            upstream_error_detail_present=bool(err_detail),
        )
        err = {"code": -32603, "message": "Upstream request failed"}
        if err_detail:
            err["data"] = err_detail
        return {"jsonrpc": "2.0", "id": req_id, "error": err}
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        _log_tool_call_audit(
            user=user,
            req_id=req_id,
            tool_name=name,
            upstream_tool_name=original_name,
            server_id=server_id,
            arguments=call_params.get("arguments"),
            result="error",
            reason="upstream_jsonrpc_error",
            started_at=started_at,
            error_code=data["error"].get("code"),
        )
        return data
    if server_id == "n8n" and original_name in {"n8n_create_workflow", "n8n_delete_workflow"}:
        _sync_managed_n8n_workflow_record(
            user=user,
            upstream_tool_name=original_name,
            arguments=call_params.get("arguments") or {},
            response_payload=data,
        )
    if _is_cloud_run_deployer_server(server_id) and original_name in {
        "deploy_app",
        "complete_deployment",
        "delete_app",
    }:
        _sync_managed_app_record(
            user=user,
            upstream_tool_name=original_name,
            arguments=call_params.get("arguments") or {},
            response_payload=data,
        )
    _log_tool_call_audit(
        user=user,
        req_id=req_id,
        tool_name=name,
        upstream_tool_name=original_name,
        server_id=server_id,
        arguments=call_params.get("arguments"),
        result="success",
        reason="completed",
        started_at=started_at,
    )
    return data


async def _handle_resources_list(body: Dict[str, Any], sessions: Dict[str, str], *, user: UserInDB) -> Dict[str, Any]:
    """Fan-out resources/list, merge and prefix URIs; append built-in mcp_proxy://help/*."""
    req_id = body.get("id")
    params = body.get("params") or {}
    servers = _get_enabled_servers()
    server_map = {s.id: s for s in servers}
    all_resources: List[Dict[str, Any]] = []
    for server_id, upstream_sid in sessions.items():
        s = server_map.get(server_id)
        if not s:
            continue
        try:
            headers = await resolve_upstream_headers(s, user_id=user.id)
        except (UpstreamOAuthNotConnectedError, ValueError):
            continue
        data, _, _ = await _forward_to_upstream(
            server_id, s.url, headers, upstream_sid, "resources/list", params, req_id
        )
        if data and "result" in data:
            resources = data["result"].get("resources", [])
            for r in resources:
                r = dict(r)
                uri = r.get("uri", "")
                r["uri"] = _prefix_resource(server_id, uri)
                all_resources.append(r)
    allowed = resolve_allowed_server_ids(user)
    all_resources.extend(list_help_resource_descriptors(user=user, allowed_server_ids=allowed))
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"resources": all_resources},
    }


async def _handle_resource_templates_list(
    body: Dict[str, Any], sessions: Dict[str, str], *, user: UserInDB
) -> Dict[str, Any]:
    """Fan-out resources/templates/list, merge and prefix uriTemplate; append proxy help templates."""
    req_id = body.get("id")
    params = body.get("params") or {}
    servers = _get_enabled_servers()
    server_map = {s.id: s for s in servers}
    all_templates: List[Dict[str, Any]] = []
    for server_id, upstream_sid in sessions.items():
        s = server_map.get(server_id)
        if not s:
            continue
        try:
            headers = await resolve_upstream_headers(s, user_id=user.id)
        except (UpstreamOAuthNotConnectedError, ValueError):
            continue
        data, _, _ = await _forward_to_upstream(
            server_id, s.url, headers, upstream_sid, "resources/templates/list", params, req_id
        )
        if not data or not isinstance(data.get("result"), dict):
            continue
        for t in data["result"].get("resourceTemplates", []):
            if not isinstance(t, dict):
                continue
            t = dict(t)
            ut = t.get("uriTemplate", "")
            if ut:
                t["uriTemplate"] = _prefix_resource(server_id, ut)
            all_templates.append(t)
    allowed = resolve_allowed_server_ids(user)
    all_templates.extend(list_help_resource_templates(user=user, allowed_server_ids=allowed))
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"resourceTemplates": all_templates},
    }


async def _handle_resources_read(body: Dict[str, Any], sessions: Dict[str, str], *, user: UserInDB) -> Dict[str, Any]:
    """Route resources/read by prefixed URI; serve built-in help under mcp_proxy://help/* locally."""
    req_id = body.get("id")
    params = body.get("params") or {}
    uri = params.get("uri", "")
    if isinstance(uri, str) and (uri.startswith(f"{HELP_URI_PREFIX}/") or uri == HELP_URI_PREFIX):
        allowed = resolve_allowed_server_ids(user)
        result, err = read_help_resource(uri, user=user, allowed_server_ids=allowed)
        if err:
            return {"jsonrpc": "2.0", "id": req_id, "error": err}
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    servers = _get_enabled_servers()
    parsed = _parse_prefixed_uri(uri, servers)
    if not parsed:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Unknown resource: {uri}"},
        }
    server_id, original_uri = parsed
    if server_id not in sessions:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"No session for server: {server_id}"},
        }
    upstream_sid = sessions.get(server_id) or ""
    s = next((x for x in servers if x.id == server_id), None)
    if not s:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Server not found: {server_id}"},
        }
    call_params = dict(params)
    call_params["uri"] = original_uri
    try:
        headers = await resolve_upstream_headers(s, user_id=user.id)
    except UpstreamOAuthNotConnectedError:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32003,
                "message": f"OAuth not connected for server {server_id}; link your account in the proxy web UI",
            },
        }
    except ValueError as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}
    data, new_sid, err_detail = await _forward_to_upstream(
        server_id, s.url, headers, upstream_sid, "resources/read", call_params, req_id
    )
    if new_sid:
        sessions[server_id] = new_sid
    if not data:
        err = {"code": -32603, "message": "Upstream request failed"}
        if err_detail:
            err["data"] = err_detail
        return {"jsonrpc": "2.0", "id": req_id, "error": err}
    return data


async def _forward_resources_subscription_rpc(
    body: Dict[str, Any],
    sessions: Dict[str, str],
    rpc_method: str,
    *,
    user_id: str,
) -> Dict[str, Any]:
    """Forward resources/subscribe or resources/unsubscribe by prefixed URI; no-op if upstream lacks support."""
    req_id = body.get("id")
    params = body.get("params") or {}
    uri = params.get("uri", "")
    servers = _get_enabled_servers()
    parsed = _parse_prefixed_uri(uri, servers)
    if not parsed:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Unknown resource: {uri}"},
        }
    server_id, original_uri = parsed
    if server_id not in sessions:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"No session for server: {server_id}"},
        }
    upstream_sid = sessions.get(server_id) or ""
    s = next((x for x in servers if x.id == server_id), None)
    if not s:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Server not found: {server_id}"},
        }
    call_params = dict(params)
    call_params["uri"] = original_uri
    try:
        headers = await resolve_upstream_headers(s, user_id=user_id)
    except UpstreamOAuthNotConnectedError:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32003,
                "message": f"OAuth not connected for server {server_id}; link your account in the proxy web UI",
            },
        }
    except ValueError as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}
    data, new_sid, err_detail = await _forward_to_upstream(
        server_id, s.url, headers, upstream_sid, rpc_method, call_params, req_id
    )
    if new_sid:
        sessions[server_id] = new_sid
    if data and isinstance(data.get("error"), dict):
        logger.debug(
            "Upstream %s %s JSON-RPC error (treating as ok for client): %s",
            server_id,
            rpc_method,
            data.get("error"),
        )
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if data and "result" in data and "error" not in data:
        return data
    if err_detail:
        logger.debug("Upstream %s %s HTTP failure (treating as ok): %s", server_id, rpc_method, err_detail)
    return {"jsonrpc": "2.0", "id": req_id, "result": {}}


async def _handle_resources_subscribe(
    body: Dict[str, Any], sessions: Dict[str, str], *, user_id: str
) -> Dict[str, Any]:
    return await _forward_resources_subscription_rpc(body, sessions, "resources/subscribe", user_id=user_id)


async def _handle_resources_unsubscribe(
    body: Dict[str, Any], sessions: Dict[str, str], *, user_id: str
) -> Dict[str, Any]:
    return await _forward_resources_subscription_rpc(body, sessions, "resources/unsubscribe", user_id=user_id)


async def _handle_prompts_list(body: Dict[str, Any], sessions: Dict[str, str], *, user: UserInDB) -> Dict[str, Any]:
    """Fan-out prompts/list, merge and prefix prompt names."""
    req_id = body.get("id")
    params = body.get("params") or {}
    servers = _get_enabled_servers()
    server_map = {s.id: s for s in servers}
    all_prompts: List[Dict[str, Any]] = list_proxy_prompt_descriptors(
        user=user,
        allowed_server_ids=resolve_allowed_server_ids(user),
    )
    for server_id, upstream_sid in sessions.items():
        s = server_map.get(server_id)
        if not s:
            continue
        try:
            headers = await resolve_upstream_headers(s, user_id=user.id)
        except (UpstreamOAuthNotConnectedError, ValueError):
            continue
        data, _, _ = await _forward_to_upstream(server_id, s.url, headers, upstream_sid, "prompts/list", params, req_id)
        if data and "result" in data:
            prompts = data["result"].get("prompts", [])
            for p in prompts:
                p = dict(p)
                p["name"] = _prefix_prompt(server_id, p.get("name", ""))
                all_prompts.append(p)
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"prompts": all_prompts},
    }


async def _handle_prompts_get(body: Dict[str, Any], sessions: Dict[str, str], *, user: UserInDB) -> Dict[str, Any]:
    """Route prompts/get by prefixed name."""
    req_id = body.get("id")
    params = body.get("params") or {}
    name = params.get("name", "")
    result, err = read_proxy_prompt(name, user=user, allowed_server_ids=resolve_allowed_server_ids(user))
    if result is not None:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if err and not _parse_prefixed_name(name, _get_enabled_servers()):
        return {"jsonrpc": "2.0", "id": req_id, "error": err}
    servers = _get_enabled_servers()
    parsed = _parse_prefixed_name(name, servers)
    if not parsed:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Unknown prompt: {name}"},
        }
    server_id, original_name = parsed
    if server_id not in sessions:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"No session for server: {server_id}"},
        }
    upstream_sid = sessions.get(server_id) or ""
    s = next((x for x in servers if x.id == server_id), None)
    if not s:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Server not found: {server_id}"},
        }
    call_params = dict(params)
    call_params["name"] = original_name
    try:
        headers = await resolve_upstream_headers(s, user_id=user.id)
    except UpstreamOAuthNotConnectedError:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32003,
                "message": f"OAuth not connected for server {server_id}; link your account in the proxy web UI",
            },
        }
    except ValueError as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}
    data, new_sid, err_detail = await _forward_to_upstream(
        server_id, s.url, headers, upstream_sid, "prompts/get", call_params, req_id
    )
    if new_sid:
        sessions[server_id] = new_sid
    if not data:
        err = {"code": -32603, "message": "Upstream request failed"}
        if err_detail:
            err["data"] = err_detail
        return {"jsonrpc": "2.0", "id": req_id, "error": err}
    return data


async def _handle_notification(body: Dict[str, Any], sessions: Dict[str, str], *, user_id: str) -> None:
    """Pass notifications through to all upstreams (fire-and-forget)."""
    method = body.get("method", "")
    params = body.get("params") or {}
    servers = _get_enabled_servers()
    server_map = {s.id: s for s in servers}
    tasks = []
    for server_id, upstream_sid in sessions.items():
        s = server_map.get(server_id)
        if not s:
            continue
        try:
            headers = await resolve_upstream_headers(s, user_id=user_id)
        except (UpstreamOAuthNotConnectedError, ValueError):
            continue
        tasks.append(_forward_to_upstream(server_id, s.url, headers, upstream_sid, method, params, None))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def handle_mcp_get(request: Request) -> Response:
    """Handle GET /mcp - SSE stream (Streamable HTTP transport, same as upstream MCPs)."""
    user_key = request.headers.get("X-API-Key") or request.headers.get("Authorization") or ""
    if user_key.startswith("Bearer "):
        user_key = user_key[7:].strip()
    if not user_key:
        increment_denied_missing_key()
        return JSONResponse(status_code=401, content={"detail": "X-API-Key required"})
    user = resolve_user_by_key(user_key)
    if not user:
        increment_denied_invalid_user()
        return JSONResponse(status_code=401, content={"detail": "Invalid API key or user not active"})
    raw_session = request.headers.get(MCP_SESSION_ID_HEADER) or request.headers.get("Mcp-Session-Id") or ""
    session_id = "".join(c for c in raw_session.strip() if c.isalnum() or c == "-")
    if not session_id:
        return JSONResponse(status_code=400, content={"detail": f"{MCP_SESSION_ID_HEADER} required"})
    entry = _sessions.get(session_id)
    if entry is None:
        return JSONResponse(status_code=404, content={"detail": "Session not found or expired"})
    if entry.get("user_id") != user.id:
        return JSONResponse(status_code=403, content={"detail": "Session does not belong to this user"})

    async def sse_stream():
        # Minimal SSE: send open event, then end. Proxy does not forward upstream SSE.
        yield "event: open\ndata: {}\n\n"

    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={
            MCP_SESSION_ID_HEADER: session_id,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def handle_mcp_post(request: Request) -> Response:
    """Handle POST /mcp - JSON-RPC request."""
    try:
        body = await request.json()
    except Exception as e:
        logger.debug("Invalid JSON body: %s", e)
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
        )
    method = body.get("method", "")
    req_id = body.get("id")
    user_key = request.headers.get("X-API-Key") or request.headers.get("Authorization") or ""
    if user_key.startswith("Bearer "):
        user_key = user_key[7:].strip()
    if not user_key:
        increment_denied_missing_key()
        log_audit("mcp_connection_attempt", result="denied", details="Missing X-API-Key")
        return JSONResponse(status_code=401, content={"detail": "X-API-Key required"})
    user = resolve_user_by_key(user_key)
    if not user:
        increment_denied_invalid_user()
        log_audit("mcp_connection_attempt", result="denied", details="Invalid or inactive user")
        return JSONResponse(status_code=401, content={"detail": "Invalid API key or user not active"})
    increment_requests_authorized()
    allowed_server_ids = resolve_allowed_server_ids(user)
    log_audit(
        "mcp_authorization_decision",
        user_id=user.id,
        role=user.role,
        result="success",
        request_id=str(req_id),
    )
    record_authorized_request(user.id)
    logger.debug("MCP request: method=%s id=%s user=%s", method, req_id, user.id)
    raw_session = request.headers.get(MCP_SESSION_ID_HEADER) or request.headers.get("Mcp-Session-Id") or ""
    session_id = "".join(c for c in raw_session.strip() if c.isalnum() or c == "-")
    try:
        return await _handle_mcp_request(
            method,
            req_id,
            session_id,
            raw_session,
            body,
            user=user,
            allowed_server_ids=allowed_server_ids,
        )
    except Exception as e:
        increment_errors()
        logger.exception("MCP request failed: %s", e)
        # Do not expose internal error details to client
        return JSONResponse(
            status_code=500,
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Internal server error"},
            },
        )


async def _handle_mcp_request(
    method: str,
    req_id: Any,
    session_id: str,
    raw_session: str,
    body: Dict[str, Any],
    *,
    user: UserInDB,
    allowed_server_ids: List[str],
) -> Response:
    """Dispatch MCP JSON-RPC; initialize filters upstreams by allowed_server_ids."""
    increment_method(method)
    if method == "initialize":
        if session_id:
            logger.info("400: Cannot re-initialize (client sent session_id)")
            return JSONResponse(
                status_code=400,
                content={"detail": "Cannot re-initialize with existing session"},
            )
        result, proxy_sid = await _handle_initialize(body, user.id, allowed_server_ids)
        resp = JSONResponse(content=result)
        resp.headers[MCP_SESSION_ID_HEADER] = proxy_sid
        logger.info("Initialize OK, session=%s", proxy_sid[:8] + "...")
        return resp
    if not session_id:
        logger.info("400: %s required (method=%s)", MCP_SESSION_ID_HEADER, method)
        return JSONResponse(
            status_code=400,
            content={"detail": f"{MCP_SESSION_ID_HEADER} required after initialize"},
        )
    entry = _sessions.get(session_id)
    if entry is None:
        logger.warning(
            "Session not found: id=%r (len=%d), raw=%r, known=%s",
            session_id,
            len(session_id),
            raw_session[:50] if raw_session else "",
            list(_sessions.keys())[:3],
        )
        return JSONResponse(
            status_code=404,
            content={"detail": "Session not found or expired"},
        )
    if entry.get("user_id") != user.id:
        return JSONResponse(
            status_code=403,
            content={"detail": "Session does not belong to this user"},
        )
    sessions: Dict[str, str] = entry.get("servers") or {}
    if method == "tools/list":
        result = await _handle_tools_list(body, sessions, user=user)
        return JSONResponse(content=result)
    if method == "tools/call":
        result = await _handle_tools_call(body, sessions, user=user)
        if isinstance(result, dict):
            result = dedupe_tool_call_jsonrpc(result)
        return JSONResponse(content=result)
    if method == "resources/list":
        result = await _handle_resources_list(body, sessions, user=user)
        return JSONResponse(content=result)
    if method == "resources/templates/list":
        result = await _handle_resource_templates_list(body, sessions, user=user)
        return JSONResponse(content=result)
    if method == "resources/read":
        result = await _handle_resources_read(body, sessions, user=user)
        return JSONResponse(content=result)
    if method == "resources/subscribe":
        result = await _handle_resources_subscribe(body, sessions, user_id=user.id)
        return JSONResponse(content=result)
    if method == "resources/unsubscribe":
        result = await _handle_resources_unsubscribe(body, sessions, user_id=user.id)
        return JSONResponse(content=result)
    if method == "prompts/list":
        result = await _handle_prompts_list(body, sessions, user=user)
        return JSONResponse(content=result)
    if method == "prompts/get":
        result = await _handle_prompts_get(body, sessions, user=user)
        return JSONResponse(content=result)
    if method.startswith("notifications/"):
        await _handle_notification(body, sessions, user_id=user.id)
        return Response(status_code=202)
    logger.info("400: Method not found: %s", method)
    return JSONResponse(
        status_code=400,
        content={
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        },
    )


def create_mcp_proxy_app() -> Starlette:
    """Create Streamable HTTP ASGI app (same structure as mcp.streamable_http_app).

    Single /mcp endpoint for POST (JSON-RPC) and GET (SSE). Mount at /mcp-server
    so full path is /mcp-server/mcp - matches sql-gateway, n8n-mcp, firestore-gateway.
    """
    return Starlette(
        routes=[
            Route("/mcp", handle_mcp_post, methods=["POST"]),
            Route("/mcp", handle_mcp_get, methods=["GET"]),
        ]
    )
