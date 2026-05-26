"""REST API handlers for MCP server config CRUD."""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from api.audit_urls import log_explorer_audit_url
from api.deps import require_admin, verify_csrf_for_unsafe_methods
from api.diagnostics import run_server_diagnostics
from audit.logger import log_audit
from fastapi import APIRouter, Depends, HTTPException, Query, status
from firestore_store import (
    create_server,
    delete_server,
    get_cloud_run_deployer_server,
    get_server,
    list_servers,
    update_server,
)
from improvement_requests_schemas import ImprovementRequestInDB
from improvement_requests_store import (
    approve_improvement_request,
    delete_improvement_request,
    get_improvement_request,
    list_pending_improvement_requests,
)
from managed_app_secrets_store import (
    delete_app_secret,
    delete_app_secrets,
    list_app_secrets,
    set_app_secret,
)
from managed_apps_cloud_run import prune_stale_managed_apps
from managed_apps_store import (
    approve_managed_app,
    get_managed_app,
    list_managed_apps,
    mark_managed_app_deleted,
)
from managed_workflows_store import (
    get_managed_workflow,
    list_managed_workflows,
    mark_managed_workflow_deleted,
)
from mcp_utils import parse_mcp_response
from metrics_counters import snapshot as metrics_snapshot
from models import (
    ManagedAppRecord,
    ManagedAppSecretMetadata,
    ManagedAppSecretSetRequest,
    ManagedWorkflowRecord,
    ServerConfig,
    ServerConfigBatchResponse,
    ServerConfigCreate,
    ServerConfigUpdate,
)
from skill_update_requests_schemas import SkillUpdateRequestInDB
from skill_update_requests_store import (
    approve_skill_update_request,
    delete_skill_update_request,
    get_skill_update_request,
    list_pending_skill_update_requests,
)
from upstream_headers import resolve_upstream_headers
from users import store as users_store
from users.schemas import AccessRequestInDB, UserInDB

logger = logging.getLogger(__name__)
MCP_SESSION_ID_HEADER = "mcp-session-id"


def _handle_upstream_http_error(exc: httpx.HTTPStatusError, url: str) -> HTTPException:
    """Convert httpx HTTP error to user-friendly HTTPException."""
    status_code = exc.response.status_code
    try:
        body = exc.response.json()
        detail = body.get("detail") or body.get("message") or str(body)
    except Exception:
        detail = exc.response.text or exc.response.reason_phrase or str(exc)

    if status_code == 401:
        return HTTPException(status_code=400, detail=f"Upstream auth failed: {detail}")
    if status_code == 403:
        return HTTPException(status_code=400, detail=f"Upstream forbidden: {detail}")
    if status_code == 404:
        return HTTPException(status_code=400, detail=f"Upstream endpoint not found: {url}")
    if status_code == 406:
        return HTTPException(
            status_code=400,
            detail="Upstream returned 406 Not Acceptable. The server may require specific headers (e.g. Accept).",
        )
    if 400 <= status_code < 500:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upstream error ({status_code}): {detail}",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Upstream server error ({status_code})",
    )


def _extract_error_message(detail: Any) -> str:
    """Best-effort extraction of a readable message from nested upstream error payloads."""
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail") or detail.get("error")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return json.dumps(detail)
    if not isinstance(detail, str):
        return str(detail)

    text = detail.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except Exception:
        return text
    return _extract_error_message(parsed)


def _is_upstream_workflow_missing_error(exc: HTTPException) -> bool:
    """Return True when the upstream delete failed only because the workflow is already gone."""
    detail = exc.detail
    if isinstance(detail, str) and detail.startswith("Upstream tool error:"):
        detail = detail.split(":", 1)[1].strip()
    message = _extract_error_message(detail).lower()
    return "workflow with id" in message and "not found" in message


def _is_upstream_cloud_run_missing_error(exc: HTTPException) -> bool:
    """Return True when the upstream delete failed only because the Cloud Run app is already gone."""
    detail = exc.detail
    if isinstance(detail, str) and detail.startswith("Upstream tool error:"):
        detail = detail.split(":", 1)[1].strip()
    message = _extract_error_message(detail).lower()
    return "not found" in message or "does not exist" in message


# Shared with validate_upstream and call_upstream_mcp_tool; must stay in sync.
UNREACHABLE_UPSTREAM_MSG = "Cannot reach upstream (connection refused, timeout, or DNS error)"


def _is_upstream_unreachable_error(exc: HTTPException) -> bool:
    """True when httpx could not connect (see UNREACHABLE_UPSTREAM_MSG)."""
    if exc.status_code != status.HTTP_400_BAD_REQUEST:
        return False
    detail = exc.detail
    return isinstance(detail, str) and detail == UNREACHABLE_UPSTREAM_MSG


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(verify_csrf_for_unsafe_methods)],
)

INITIALIZE_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-proxy", "version": "0.1.0"},
    },
}


async def validate_upstream(url: str, headers: dict) -> None:
    """Validate that the upstream MCP server is reachable by sending initialize."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json=INITIALIZE_PAYLOAD,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    **headers,
                },
            )
            logger.debug(
                "Upstream response %s %s: status=%s headers=%s body=%r",
                url,
                response.request.method,
                response.status_code,
                dict(response.headers),
                response.text[:500] if response.text else "(empty)",
            )
            response.raise_for_status()
            data = parse_mcp_response(response.text)
            if data is None:
                logger.warning(
                    "Upstream returned non-JSON/non-SSE for %s (body=%r)",
                    url,
                    response.text[:500] if response.text else "(empty)",
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Upstream did not return valid JSON or SSE (MCP servers must respond with JSON-RPC)",
                )
            if "error" in data:
                err = data["error"]
                msg = err.get("message", err) if isinstance(err, dict) else str(err)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Upstream returned error: {msg}",
                )
            if "result" not in data:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Upstream did not return a valid InitializeResult",
                )
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        logger.debug(
            "Upstream HTTP error %s: status=%s body=%r",
            url,
            e.response.status_code,
            e.response.text[:500] if e.response.text else "(empty)",
        )
        logger.warning("Upstream HTTP error for %s: %s %s", url, e.response.status_code, e)
        raise _handle_upstream_http_error(e, url) from e
    except httpx.RequestError as e:
        logger.warning("Upstream validation failed for %s: %s", url, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=UNREACHABLE_UPSTREAM_MSG,
        ) from e


async def call_upstream_mcp_tool(
    *,
    config: ServerConfig,
    user_id: str,
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Initialize an upstream MCP session and call one tool, returning structuredContent."""
    try:
        headers = await resolve_upstream_headers(config, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            base_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **headers,
            }
            init_response = await client.post(config.url, json=INITIALIZE_PAYLOAD, headers=base_headers)
            init_response.raise_for_status()
            init_payload = parse_mcp_response(init_response.text)
            if not isinstance(init_payload, dict) or "error" in init_payload:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Upstream MCP initialize failed",
                )
            session_id = init_response.headers.get(MCP_SESSION_ID_HEADER)
            tool_headers = dict(base_headers)
            if session_id:
                tool_headers[MCP_SESSION_ID_HEADER] = session_id
            tool_response = await client.post(
                config.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments or {}},
                },
                headers=tool_headers,
            )
            tool_response.raise_for_status()
            tool_payload = parse_mcp_response(tool_response.text)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise _handle_upstream_http_error(e, config.url) from e
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=UNREACHABLE_UPSTREAM_MSG,
        ) from e

    if not isinstance(tool_payload, dict):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Invalid MCP tool response from upstream")
    if isinstance(tool_payload.get("error"), dict):
        err = tool_payload["error"]
        detail = err.get("message") or err.get("details") or str(err)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Upstream tool error: {detail}")
    result = tool_payload.get("result")
    if not isinstance(result, dict):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Upstream tool did not return a result")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream tool did not return structuredContent",
        )
    if structured.get("error"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upstream tool error: {structured.get('details') or structured.get('error')}",
        )
    return structured


@router.get("/servers", response_model=List[ServerConfig])
async def list_admin_servers(admin: UserInDB = Depends(require_admin)):
    """List all configured MCP servers."""
    try:
        return list_servers(enabled_only=False)
    except Exception as e:
        logger.exception("Failed to list servers: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable",
        ) from e


def _normalize_credentials(payload: ServerConfigCreate | ServerConfigUpdate) -> tuple[str, str]:
    """Ensure header-like values end up in credentials_header, not credentials_secret_id."""
    secret_id = getattr(payload, "credentials_secret_id", None) or ""
    header = getattr(payload, "credentials_header", None) or ""
    # If secret_id looks like "Header: value", user put it in wrong field
    if secret_id and ": " in secret_id and not header:
        return ("", secret_id)
    return (secret_id, header)


@router.post("/servers", response_model=ServerConfigBatchResponse)
async def add_servers(payload: List[ServerConfigCreate], admin: UserInDB = Depends(require_admin)):
    """Add MCP servers. Accepts a list of server configs. Validates each upstream before persisting."""
    if not payload:
        raise HTTPException(status_code=400, detail="Provide at least one server")
    created: List[ServerConfig] = []
    errors: List[dict] = []
    for i, item in enumerate(payload):
        try:
            if get_server(item.id):
                errors.append({"index": i, "id": item.id, "detail": "Server already exists"})
                continue
            secret_id, header = _normalize_credentials(item)
            probe = ServerConfig(
                id=item.id,
                url=item.url,
                credentials_secret_id=secret_id,
                credentials_header=header,
                upstream_auth=item.upstream_auth,
                oauth=item.oauth,
                enabled=item.enabled,
            )
            if item.upstream_auth != "oauth2":
                try:
                    headers = await resolve_upstream_headers(probe)
                except ValueError as e:
                    errors.append({"index": i, "id": item.id, "detail": str(e)})
                    continue
                except RuntimeError as e:
                    errors.append({"index": i, "id": item.id, "detail": f"Invalid credentials: {e}"})
                    continue
                await validate_upstream(item.url, headers)
            config = create_server(
                server_id=item.id,
                url=item.url,
                credentials_secret_id=secret_id,
                credentials_header=header,
                enabled=item.enabled,
                upstream_auth=item.upstream_auth,
                oauth=item.oauth,
            )
            created.append(config)
        except HTTPException as e:
            errors.append({"index": i, "id": item.id, "detail": e.detail})
        except Exception as e:
            logger.exception("Failed to add server %s: %s", item.id, e)
            errors.append({"index": i, "id": item.id, "detail": str(e)})
    return ServerConfigBatchResponse(created=created, errors=errors)


@router.get("/servers/{server_id}", response_model=ServerConfig)
async def get_admin_server(server_id: str, admin: UserInDB = Depends(require_admin)):
    """Get a single MCP server config."""
    try:
        config = get_server(server_id)
    except Exception as e:
        logger.exception("Failed to get server %s: %s", server_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable",
        ) from e
    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    return config


@router.patch("/servers/{server_id}", response_model=ServerConfig)
async def patch_server(
    server_id: str,
    payload: ServerConfigUpdate,
    admin: UserInDB = Depends(require_admin),
):
    """Update an MCP server config (partial)."""
    try:
        if not get_server(server_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
        secret_id = payload.credentials_secret_id
        header = payload.credentials_header
        if secret_id is not None or header is not None:
            _secret, _header = _normalize_credentials(payload)
            if secret_id is not None:
                secret_id = _secret
            if header is not None:
                header = _header
        config = update_server(
            server_id=server_id,
            url=payload.url,
            credentials_secret_id=secret_id,
            credentials_header=header,
            enabled=payload.enabled,
            upstream_auth=payload.upstream_auth,
            reset_credentials=payload.reset_credentials or None,
            oauth=payload.oauth,
            reset_oauth=payload.reset_oauth if payload.reset_oauth else None,
        )
        return config
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update server %s: %s", server_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to update server configuration",
        ) from e


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_server(server_id: str, admin: UserInDB = Depends(require_admin)):
    """Remove an MCP server config."""
    try:
        if not delete_server(server_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete server %s: %s", server_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to delete server configuration",
        ) from e


@router.get("/servers/{server_id}/diagnostics", response_model=Dict[str, Any])
async def get_server_diagnostics(
    server_id: str,
    admin: UserInDB = Depends(require_admin),
    user_id: Optional[str] = Query(
        None,
        description="Firestore user id (required for full oauth2 upstream diagnostics)",
    ),
):
    """Live MCP probe: initialize + tools/resources/prompts lists."""
    try:
        cfg = get_server(server_id)
    except Exception as e:
        logger.exception("Failed to get server %s: %s", server_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable",
        ) from e
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    return await run_server_diagnostics(cfg, user_id=user_id)


@router.get("/n8n-workflows", response_model=List[ManagedWorkflowRecord])
async def list_admin_managed_workflows(admin: UserInDB = Depends(require_admin)):
    """List n8n workflows that were created through the MCP proxy."""
    return list_managed_workflows()


@router.get("/apps", response_model=List[ManagedAppRecord])
async def list_admin_managed_apps(admin: UserInDB = Depends(require_admin)):
    """List web apps deployed through the Cloud Run deployer MCP."""
    prune_stale_managed_apps()
    return list_managed_apps(include_deleted=True)


@router.post("/apps/{app_id}/approve", response_model=ManagedAppRecord)
async def approve_admin_managed_app(app_id: str, admin: UserInDB = Depends(require_admin)):
    """Re-apply deployer approve_app (IAP) if a service was changed manually. Normal deploys already include IAP."""
    record = get_managed_app(app_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed app not found")
    if record.status == "deleted":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Deleted apps cannot be approved")
    cfg = get_cloud_run_deployer_server()
    if not cfg or not cfg.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enabled Cloud Run deployer server not found")
    approved_url = record.approved_url
    try:
        structured = await call_upstream_mcp_tool(
            config=cfg,
            user_id=admin.id,
            tool_name="approve_app",
            arguments={
                "app_id": app_id,
                "service_name": record.service_name,
                "project_id": record.project_id,
                "region": record.region,
                "runtime_service_account": record.runtime_service_account or "",
                "execute": True,
            },
        )
        app_payload = structured.get("app") if isinstance(structured.get("app"), dict) else {}
        if isinstance(structured.get("approved_url"), str):
            approved_url = structured.get("approved_url")
        elif isinstance(app_payload.get("approved_url"), str):
            approved_url = app_payload.get("approved_url")
    except HTTPException:
        raise
    approved = approve_managed_app(app_id, reviewed_by=admin.id, approved_url=approved_url)
    if not approved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed app not found")
    return approved


def _secret_metadata_response(item: Any) -> ManagedAppSecretMetadata:
    return ManagedAppSecretMetadata(
        app_id=item.app_id,
        name=item.name,
        key_version=item.key_version,
        created_by_user_id=item.created_by_user_id,
        updated_by_user_id=item.updated_by_user_id,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/apps/{app_id}/secrets", response_model=List[ManagedAppSecretMetadata])
async def list_admin_managed_app_secrets(app_id: str, admin: UserInDB = Depends(require_admin)):
    """List encrypted app secret metadata. Values are never returned."""
    record = get_managed_app(app_id)
    if not record or record.status == "deleted":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed app not found")
    return [_secret_metadata_response(item) for item in list_app_secrets(app_id)]


@router.post("/apps/{app_id}/secrets", response_model=ManagedAppSecretMetadata)
async def set_admin_managed_app_secret(
    app_id: str,
    body: ManagedAppSecretSetRequest,
    admin: UserInDB = Depends(require_admin),
):
    """Set an app secret in encrypted Firestore storage. The value is write-only."""
    record = get_managed_app(app_id)
    if not record or record.status == "deleted":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed app not found")
    try:
        item = set_app_secret(app_id=app_id, name=body.name, value=body.value, user=admin)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    log_audit(
        "managed_app_secret_set",
        user_id=admin.id,
        role=admin.role,
        result="success",
        details={"app_id": app_id, "secret_name": item.name},
    )
    return _secret_metadata_response(item)


@router.delete("/apps/{app_id}/secrets/{name}")
async def delete_admin_managed_app_secret(app_id: str, name: str, admin: UserInDB = Depends(require_admin)):
    """Delete one encrypted app secret."""
    record = get_managed_app(app_id)
    if not record or record.status == "deleted":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed app not found")
    try:
        existed = delete_app_secret(app_id=app_id, name=name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    log_audit(
        "managed_app_secret_deleted",
        user_id=admin.id,
        role=admin.role,
        result="success",
        details={"app_id": app_id, "secret_name": name, "existed": existed},
    )
    return {"app_id": app_id, "secret_name": name, "deleted": True, "existed": existed}


@router.delete("/apps/{app_id}")
async def delete_admin_managed_app(
    app_id: str,
    delete_mode: str = Query("full_cleanup", pattern="^(cloud_run_only|full_cleanup)$"),
    admin: UserInDB = Depends(require_admin),
):
    """Delete a managed web app through the Cloud Run deployer MCP upstream."""
    record = get_managed_app(app_id)
    if not record or record.status == "deleted":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed app not found")
    cfg = get_cloud_run_deployer_server()
    if not cfg or not cfg.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enabled Cloud Run deployer server not found")
    already_missing_upstream = False
    upstream_unreachable = False
    try:
        structured = await call_upstream_mcp_tool(
            config=cfg,
            user_id=admin.id,
            tool_name="delete_app",
            arguments={
                "app_id": app_id,
                "service_name": record.service_name,
                "project_id": record.project_id,
                "region": record.region,
                "delete_mode": delete_mode,
                "image": record.image_digest or "",
                "runtime_service_account": record.runtime_service_account or "",
                "execute": True,
            },
        )
        already_missing_upstream = bool(structured.get("already_missing_upstream"))
    except HTTPException as exc:
        if _is_upstream_unreachable_error(exc):
            upstream_unreachable = True
            logger.warning(
                "Managed app %s: deployer unreachable; removing local record only (Cloud Run may need manual cleanup)",
                app_id,
            )
        elif _is_upstream_cloud_run_missing_error(exc):
            already_missing_upstream = True
            logger.info("Managed app %s was already missing upstream; cleaning up local record", app_id)
        else:
            mark_managed_app_deleted(app_id, delete_mode=delete_mode, status="delete_failed")
            raise
    deleted_secret_count = delete_app_secrets(app_id) if delete_mode == "full_cleanup" else 0
    deleted = mark_managed_app_deleted(app_id, delete_mode=delete_mode, status="deleted")
    return {
        "app_id": app_id,
        "deleted": True,
        "deleted_at": deleted.deleted_at if deleted else None,
        "deleted_secret_count": deleted_secret_count,
        "already_missing_upstream": already_missing_upstream,
        "upstream_unreachable": upstream_unreachable,
        "delete_mode": delete_mode,
    }


@router.delete("/n8n-workflows/{workflow_id}")
async def delete_admin_managed_workflow(workflow_id: str, admin: UserInDB = Depends(require_admin)):
    """Delete a managed n8n workflow through the configured n8n MCP upstream."""
    record = get_managed_workflow(workflow_id)
    if not record or record.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed workflow not found")
    cfg = get_server("n8n")
    if not cfg or not cfg.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enabled n8n server not found")
    already_missing_upstream = False
    try:
        await call_upstream_mcp_tool(
            config=cfg,
            user_id=admin.id,
            tool_name="n8n_delete_workflow",
            arguments={"workflow_id": workflow_id},
        )
    except HTTPException as exc:
        if not _is_upstream_workflow_missing_error(exc):
            raise
        already_missing_upstream = True
        logger.info("Managed n8n workflow %s was already missing upstream; cleaning up local record", workflow_id)
    deleted = mark_managed_workflow_deleted(workflow_id)
    return {
        "workflow_id": workflow_id,
        "deleted": True,
        "deleted_at": deleted.deleted_at if deleted else None,
        "already_missing_upstream": already_missing_upstream,
    }


@router.get("/access-requests/pending", response_model=List[AccessRequestInDB])
async def list_pending_access_requests(admin: UserInDB = Depends(require_admin)):
    """All pending access requests (queue for approvals UI)."""
    return users_store.get_pending_access_requests()


@router.get("/improvement-requests/pending", response_model=List[ImprovementRequestInDB])
async def list_admin_pending_improvement_requests(admin: UserInDB = Depends(require_admin)):
    """All pending MCP improvement requests created by users or agents."""
    return list_pending_improvement_requests()


@router.get("/skill-update-requests/pending", response_model=List[SkillUpdateRequestInDB])
async def list_admin_pending_skill_update_requests(admin: UserInDB = Depends(require_admin)):
    """All pending shared skill update requests created by users or agents."""
    return list_pending_skill_update_requests()


@router.post("/improvement-requests/{request_id}/approve", response_model=ImprovementRequestInDB)
async def approve_admin_improvement_request(request_id: str, admin: UserInDB = Depends(require_admin)):
    """Approve a pending improvement request and keep its record."""
    existing = get_improvement_request(request_id)
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Improvement request not found")
    if existing.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Improvement request is not pending",
        )
    req = approve_improvement_request(request_id, reviewed_by=admin.id)
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Improvement request not found")
    return req


@router.post("/improvement-requests/{request_id}/decline")
async def decline_admin_improvement_request(request_id: str, admin: UserInDB = Depends(require_admin)):
    """Decline an improvement request by deleting it from Firestore."""
    if not delete_improvement_request(request_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Improvement request not found")
    return {"status": "declined", "request_id": request_id}


@router.post("/skill-update-requests/{request_id}/approve", response_model=SkillUpdateRequestInDB)
async def approve_admin_skill_update_request(request_id: str, admin: UserInDB = Depends(require_admin)):
    """Approve a pending skill update request and keep its record."""
    existing = get_skill_update_request(request_id)
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill update request not found")
    if existing.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Skill update request is not pending",
        )
    req = approve_skill_update_request(request_id, reviewed_by=admin.id)
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill update request not found")
    return req


@router.post("/skill-update-requests/{request_id}/decline")
async def decline_admin_skill_update_request(request_id: str, admin: UserInDB = Depends(require_admin)):
    """Decline a skill update request by deleting it from Firestore."""
    if not delete_skill_update_request(request_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill update request not found")
    return {"status": "declined", "request_id": request_id}


@router.get("/audit/log-explorer", response_model=Dict[str, str])
async def get_audit_log_explorer_link(admin: UserInDB = Depends(require_admin)):
    """URL to Cloud Console Log Explorer with an audit-oriented query."""
    return {"url": log_explorer_audit_url()}


@router.get("/metrics")
async def get_admin_metrics(admin: UserInDB = Depends(require_admin)):
    """In-process MCP counters for this Cloud Run instance (resets on deploy; not global across replicas)."""
    return metrics_snapshot()
