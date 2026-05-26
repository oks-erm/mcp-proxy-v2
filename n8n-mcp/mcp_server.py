"""MCP server for n8n, following the sql-gateway pattern."""

import copy
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
import n8n_client
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.coerce_numeric import coerce_int
from mcp_platform.detail_level import parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result
from n8n_client import (
    GITHUB_API,
    GITHUB_RAW,
    N8N_NODES_BRANCH,
    N8N_NODES_PATH,
    N8N_NODES_REPO,
    ensure_workflow_has_mcp_tag,
    get_admin_client,
    get_client,
    github_headers,
    tag_workflow,
)

logger = logging.getLogger(__name__)

_MINIMUM_SCHEDULE_TRIGGER_INTERVAL_HOURS = 2
_MINIMUM_SCHEDULE_TRIGGER_INTERVAL_MINUTES = _MINIMUM_SCHEDULE_TRIGGER_INTERVAL_HOURS * 60

mcp = FastMCP(
    "n8n",
    instructions=(
        "N8N MCP SERVER — CAPABILITIES & LIMITATIONS\n\n"
        "WHAT THIS SERVER CAN DO — Read-only:\n"
        "- List all workflows (names, IDs, active status) — n8n_get_workflows.\n"
        "- Get full details of a specific workflow (nodes, connections, settings) — n8n_get_workflow.\n"
        "- List executions (history, status, timestamps) and get execution details — n8n_get_executions.\n"
        "- List credentials: names and types only; secrets are NEVER exposed — n8n_get_credentials.\n"
        "- List available nodes/integrations and inspect node source — n8n_list_nodes, n8n_list_node_files, "
        "n8n_get_node_source, n8n_search_nodes.\n\n"
        "WHAT THIS SERVER CAN DO — Write (use with care):\n"
        "- Create workflows — n8n_create_workflow (requires nodes, connections; credentials created and "
        "injected by name).\n"
        "- Update workflows — n8n_update_workflow. The n8n API expects a full workflow on PUT; this tool GETs "
        "the workflow, applies your changes, then PUTs the complete object. You may pass only the top-level "
        "keys you want to change (omit others). For nodes: by default the tool merges each supplied node into "
        "the existing workflow by node id (safe for single-node edits). Set nodes_replace=true only when "
        "replacing the entire nodes array. If you pass connections or settings, they replace those objects "
        "wholly when provided — omit them to keep existing values.\n"
        "- Delete workflows — n8n_delete_workflow.\n"
        "- Activate or deactivate a workflow — pass active=True or active=False to n8n_update_workflow.\n"
        "- Create credentials — n8n_create_credential (call n8n_get_credentials first to avoid duplicates; "
        "call n8n_get_credential_schema for required data fields).\n"
        "- Stop or retry an execution — n8n_stop_execution, n8n_retry_execution (does not start new runs).\n\n"
        "WHAT THIS SERVER CANNOT DO:\n"
        "- Execute or trigger a workflow. There is no tool to start a new run. Use the webhook workaround below.\n"
        "- Delete credentials.\n"
        "- Access credential secrets or sensitive data; only id, name, type, updatedAt are visible.\n\n"
        "HOW TO TRIGGER A WORKFLOW (workaround):\n"
        "To execute an n8n workflow externally, the workflow must have a Webhook trigger node and be active. "
        "Then call: POST https://<n8n-instance>/webhook/<path>. This is the only supported way to start "
        "executions programmatically.\n\n"
        "IMPORTANT API BEHAVIOR:\n"
        "- The n8n REST PUT expects a complete workflow shape (name, nodes, connections, settings); "
        "n8n_update_workflow builds that from GET + your fields.\n"
        "- Node updates: default behavior merges supplied nodes into the existing list by id (partial node "
        "objects are OK). Use nodes_replace=true to set nodes to exactly the list you pass (old behavior).\n"
        "- Connections/settings: if you pass them, they replace the whole object; omit to leave unchanged.\n"
        "- Extra fields (e.g. retryOnFail, read-only node keys) can cause rejection; the tool strips some.\n"
        "- Schedule Trigger guardrail: minute-based schedules are forbidden; the minimum allowed recurrence is "
        "every 2 hours. Create/update requests that set a smaller schedule are rejected.\n"
        "- Create workflow guardrail: every new workflow must include a short `summary`; the tool injects it as "
        "a Sticky Note so the canvas is self-documented.\n"
        "- Workflow metadata: this server does not require a Notion ticket or other business ticket to create a "
        "workflow. Only the tool inputs and the required `mcp` tag policy apply.\n"
        "- Required workflow tag: every workflow created or updated through this server must have the `mcp` tag. "
        "If tag enforcement fails, the write fails.\n"
        "- Always identify workflows by id; GET first if you need full context.\n\n"
        "AUTHORING HELP (mcp-proxy):\n"
        "- Canonical journey: mcp_proxy://help/by-task/n8n-create-workflow\n"
        "- Starter resource: n8n://templates/workflow-starter-minimal\n"
        "- Dry-run: n8n_validate_workflow_definition; resolver: n8n_find_node_by_name_or_capability\n"
        "- Successful create/update responses include mcp_next_steps (suggested follow-up tool calls).\n"
        "- Prompt body inspection: resources n8n://prompts/n8n-minimal-workflow-editor and .../static "
        "(for n8n_minimal_workflow_editor)."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


# -- Workflow tools ---------------------------------------------------------


def _mcp_next_steps_for_workflow(workflow_id: str, *, is_create: bool) -> List[str]:
    """Suggested follow-up tool calls after a workflow write (namespaced elsewhere as mcp_next_steps)."""
    if not workflow_id or not str(workflow_id).strip():
        return []
    wid = str(workflow_id).strip()
    steps: List[str] = [
        f"n8n_get_workflow(workflow_id={wid!r})",
        f"n8n_get_executions(workflow_id={wid!r})",
    ]
    if is_create:
        steps.append(
            "If triggers should run in production, activate with "
            f"n8n_update_workflow(workflow_id={wid!r}, active=True) after credentials and parameters are valid."
        )
    return steps


def _attach_mcp_next_steps(body: Dict[str, Any], *, is_create: bool) -> Dict[str, Any]:
    """Merge mcp_next_steps into a workflow-shaped dict when an id is present."""
    if not isinstance(body, dict):
        return body
    wid = body.get("id")
    if wid is None:
        return body
    out = dict(body)
    out["mcp_next_steps"] = _mcp_next_steps_for_workflow(str(wid), is_create=is_create)
    return out


def _n8n_editor_base_url() -> Optional[str]:
    """Return the human-facing n8n UI base URL when N8N_BASE_URL is configured."""
    base_url = (n8n_client.N8N_BASE_URL or "").strip().rstrip("/")
    if not base_url:
        return None
    return re.sub(r"/api/v\d+$", "", base_url).rstrip("/")


def _workflow_editor_url(workflow_id: Any) -> Optional[str]:
    """Return the n8n editor URL for a workflow id when the base URL is known."""
    base_url = _n8n_editor_base_url()
    if not base_url or workflow_id is None:
        return None
    wid = str(workflow_id).strip()
    if not wid:
        return None
    return f"{base_url}/workflow/{wid}"


def _attach_editor_url(body: Dict[str, Any]) -> Dict[str, Any]:
    """Attach a human-facing editor URL when the payload carries a workflow id."""
    if not isinstance(body, dict):
        return body
    editor_url = _workflow_editor_url(body.get("id"))
    if not editor_url:
        return body
    out = dict(body)
    out["editor_url"] = editor_url
    return out


def _clean_workflow_summary(summary: Optional[str]) -> Optional[str]:
    """Validate and normalize the required workflow summary used in the sticky note."""
    if summary is None:
        return None
    cleaned = str(summary).strip()
    if not cleaned:
        return None
    return cleaned


def _summary_note_position(nodes: List[Dict[str, Any]]) -> List[int]:
    """Place the summary sticky note slightly above the left-most existing node cluster."""
    xs: List[float] = []
    ys: List[float] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        position = node.get("position")
        if (
            isinstance(position, list)
            and len(position) >= 2
            and isinstance(position[0], (int, float))
            and isinstance(position[1], (int, float))
        ):
            xs.append(float(position[0]))
            ys.append(float(position[1]))
    if not xs or not ys:
        return [0, -220]
    return [int(min(xs) - 120), int(min(ys) - 220)]


def _build_workflow_summary_sticky_note(summary: str, nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the required MCP sticky note describing the workflow intent."""
    return {
        "id": str(uuid.uuid4()),
        "name": "Workflow Summary",
        "type": "n8n-nodes-base.stickyNote",
        "typeVersion": 1,
        "position": _summary_note_position(nodes),
        "parameters": {
            "content": f"## Workflow Summary\n\n{summary}",
            "height": 180,
            "width": 320,
            "color": 5,
        },
    }


def _prepend_workflow_summary_note(nodes: List[Dict[str, Any]], summary: str) -> List[Dict[str, Any]]:
    """Add the MCP-required summary sticky note to the workflow canvas."""
    copied_nodes = copy.deepcopy(nodes)
    return [_build_workflow_summary_sticky_note(summary, copied_nodes), *copied_nodes]


def _http_error(context: str, exc: Exception) -> Dict[str, Any]:
    """Standard tool error dict (wrap with structured_result at call sites)."""
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code if exc.response else None
        details = ""
        if exc.response is not None:
            try:
                details = exc.response.text[:2000]
            except Exception:
                details = str(exc)
        retryable = status_code is None or status_code >= 500
        return tool_error(
            "http_error",
            details=details or str(exc),
            context=context,
            status_code=status_code,
            cause="upstream_error",
            retryable=retryable,
            suggested_fix=(
                "Retry later or check n8n service availability." if retryable else "Check the request parameters."
            ),
        )
    return tool_error(
        "request_failed",
        details=str(exc),
        context=context,
        cause="upstream_error",
        retryable=True,
        suggested_fix="Retry later or verify n8n connectivity.",
    )


def _workflow_summary(workflow: Dict[str, Any]) -> Dict[str, Any]:
    """Return concise workflow fields for model planning."""
    tags_raw = workflow.get("tags", [])
    tag_names: List[str] = []
    if isinstance(tags_raw, list):
        for tag in tags_raw:
            if isinstance(tag, dict) and isinstance(tag.get("name"), str):
                tag_names.append(tag["name"])
            elif isinstance(tag, str):
                tag_names.append(tag)
    return {
        "id": workflow.get("id"),
        "name": workflow.get("name"),
        "active": workflow.get("active"),
        "isArchived": workflow.get("isArchived"),
        "updatedAt": workflow.get("updatedAt"),
        "triggerCount": workflow.get("triggerCount"),
        "nodeCount": len(workflow.get("nodes", [])) if isinstance(workflow.get("nodes"), list) else None,
        "tags": tag_names,
    }


def _execution_summary(execution: Dict[str, Any]) -> Dict[str, Any]:
    """Return concise execution fields for model debugging/planning."""
    return {
        "id": execution.get("id"),
        "workflowId": execution.get("workflowId"),
        "status": execution.get("status"),
        "mode": execution.get("mode"),
        "finished": execution.get("finished"),
        "startedAt": execution.get("startedAt"),
        "stoppedAt": execution.get("stoppedAt"),
        "waitTill": execution.get("waitTill"),
        "retryOf": execution.get("retryOf"),
        "retrySuccessId": execution.get("retrySuccessId"),
    }


def _credential_summary(credential: Dict[str, Any]) -> Dict[str, Any]:
    """Return concise credential fields for model planning."""
    return {
        "id": credential.get("id"),
        "name": credential.get("name"),
        "type": credential.get("type"),
        "updatedAt": credential.get("updatedAt"),
    }


@mcp.tool(structured_output=False)
async def n8n_get_workflows(
    tags: Optional[str] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
    active: Optional[bool] = None,
    name: Optional[str] = None,
    summary_only: Optional[bool] = None,
    detail_level: str = "compact",
) -> CallToolResult:
    """List workflows (read/list).

    **Use when:**
        Discovering workflow ids/names or paging (does not execute workflows).

    **Args:**
        tags: Optional filter for n8n list endpoint.
        limit: Page size.
        cursor: Pagination token from prior ``nextCursor``.
        active: Optional active filter.
        name: Optional name filter.
        summary_only: Deprecated; if set, overrides detail_level (false => full list payload).
        detail_level: ``compact`` (default) returns trimmed rows; ``full`` returns raw API payload per item.

    **Returns:**
        ``{"data", "nextCursor", "detail_level"}`` on success.

    **Notes:**
        Prefer ``n8n_find_workflow_by_name`` for a single name lookup without manual paging.

    **Errors:**
        ``{"error", "details"}`` on failure.

    **Example:**
        ``n8n_get_workflows(limit=20)``
    """
    limit = coerce_int(limit, default=50, minimum=1, maximum=250)
    params: Dict[str, Any] = {"limit": limit}
    if tags:
        params["tags"] = tags
    if cursor:
        params["cursor"] = cursor
    if active is not None:
        params["active"] = active
    if name:
        params["name"] = name
    if summary_only is False:
        want_full = True
    elif summary_only is True:
        want_full = False
    else:
        want_full = parse_detail_level(detail_level, default="compact") == "full"
    dl = "full" if want_full else parse_detail_level(detail_level, default="compact")
    try:
        async with get_client() as client:
            response = await client.get("/workflows", params=params)
            response.raise_for_status()
            payload = response.json()
        next_cursor_raw = payload.get("nextCursor") if isinstance(payload, dict) else None
        next_cursor_str = str(next_cursor_raw) if next_cursor_raw else None
        pagination = build_pagination_meta(
            limit=limit,
            cursor=cursor or None,
            has_more=bool(next_cursor_str),
            next_cursor=next_cursor_str,
        )
        if want_full:
            if isinstance(payload, dict):
                return structured_result(
                    with_response_meta(
                        {**payload, "detail_level": "full"}, tool="n8n_get_workflows", pagination=pagination
                    )
                )
            return structured_result(
                with_response_meta(
                    {"data": payload, "detail_level": "full"}, tool="n8n_get_workflows", pagination=pagination
                )
            )
        if isinstance(payload, dict):
            data = payload.get("data", [])
            summarized = [_workflow_summary(item) for item in data if isinstance(item, dict)]
            return structured_result(
                with_response_meta(
                    {"data": summarized, "nextCursor": next_cursor_raw, "detail_level": dl},
                    tool="n8n_get_workflows",
                    pagination=pagination,
                )
            )
        if isinstance(payload, list):
            return structured_result(
                with_response_meta(
                    {
                        "data": [_workflow_summary(item) for item in payload if isinstance(item, dict)],
                        "nextCursor": None,
                        "detail_level": dl,
                    },
                    tool="n8n_get_workflows",
                    pagination=pagination,
                )
            )
        return structured_result(
            with_response_meta(
                {"data": [], "nextCursor": None, "detail_level": dl}, tool="n8n_get_workflows", pagination=pagination
            )
        )
    except Exception as exc:
        return structured_result(_http_error("n8n_get_workflows", exc))


@mcp.tool(structured_output=False)
async def n8n_get_workflow(
    workflow_id: Optional[str] = None,
    summary_only: bool = False,
    detail_level: str = "full",
) -> CallToolResult:
    """Fetch a single n8n workflow definition by ``workflow_id`` (detail / read).

    Prefer ``n8n_find_workflow_by_name`` when the user gave a name; use this once you have an id (e.g. before ``n8n_update_workflow``).

    Input: required ``workflow_id``; ``summary_only`` or ``detail_level`` control payload size.

    **Use when:**
        You need the current workflow definition before n8n_update_workflow.

    **Args:**
        workflow_id: n8n workflow id (required).
        summary_only: When true, same as ``detail_level=compact`` summary shape.
        detail_level: ``compact`` / ``summary`` use _workflow_summary; ``full`` returns full workflow JSON.

    **Returns:**
        Workflow object (full or summary) with ``detail_level`` echoed when applicable.

    **Notes:**
        Full graph can be large — use compact unless editing nodes.

    **Errors:**
        ``{"error", "details"}``.

    **Example:**
        ``n8n_get_workflow(workflow_id="42")``
    """
    if not workflow_id or not str(workflow_id).strip():
        return structured_result(
            tool_error("validation_error", details="workflow_id is required", cause="validation", retryable=False)
        )
    dl = parse_detail_level(detail_level, default="full")
    want_summary = summary_only or dl in ("compact", "summary")
    try:
        async with get_client() as client:
            response = await client.get(f"/workflows/{workflow_id}")
            response.raise_for_status()
            payload = response.json()
            if want_summary and isinstance(payload, dict):
                return structured_result({**_workflow_summary(payload), "detail_level": "compact"})
            if isinstance(payload, dict):
                return structured_result({**payload, "detail_level": "full"})
            return structured_result({"payload": payload, "detail_level": "full"})
    except Exception as exc:
        return structured_result(_http_error("n8n_get_workflow", exc))


@mcp.tool(structured_output=False)
async def n8n_find_workflow_by_name(
    query: str,
    limit: int = 10,
    active: Optional[bool] = None,
    limit_pages: int = 10,
    case_insensitive: bool = True,
) -> CallToolResult:
    """Resolve workflow ids from a human workflow title (resolver / read).

    Prefer this over manually paging ``n8n_get_workflows`` when matching by name; then call ``n8n_get_workflow`` with the chosen id.

    Required: ``query`` (not ``name`` / ``workflow_name``); optional ``active`` filter and scan limits.

    IMPORTANT: The search parameter is named ``query`` — do not pass ``name`` or ``workflow_name``.

    Use when:
        You know a workflow title and need ids without paging ``n8n_get_workflows`` yourself.

    Args:
        query: Required workflow name text; exact case-insensitive matches rank before substring matches.
        limit: Max workflows to return after ranking (default 10).
        active: When set, keep only workflows whose ``active`` flag matches.
        limit_pages: Max list API pages to scan (100 workflows per page).
        case_insensitive: Compare names case-insensitively when true (default).

    Returns:
        ``{"count", "data", "detail_level": "compact"}`` with id, name, active, isArchived, updatedAt per row.

    Notes:
        Prefer this over raw list/search when resolving a human name. Several rows mean ambiguity — do not assume the first.

    Errors:
        ``{"error": "validation_error", "details": ...}`` when ``query`` is empty.

    Example:
        ``n8n_find_workflow_by_name(query="Slack notify", limit=5)``
    """
    needle = (query or "").strip()
    if not needle:
        return structured_result(
            tool_error("validation_error", details="query is required", cause="validation", retryable=False)
        )
    n_norm = needle.lower() if case_insensitive else needle
    cap = max(1, min(limit, 50))

    def _resolver_row(wf: Dict[str, Any]) -> Dict[str, Any]:
        archived = wf.get("isArchived")
        return {
            "id": str(wf.get("id") or ""),
            "name": str(wf.get("name") or ""),
            "active": bool(wf.get("active")),
            "isArchived": archived if isinstance(archived, bool) or archived is None else None,
            "updatedAt": wf.get("updatedAt"),
        }

    ranked: List[Tuple[int, Dict[str, Any]]] = []
    cursor: Optional[str] = None
    try:
        for _ in range(max(1, min(limit_pages, 50))):
            params: Dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            async with get_client() as client:
                response = await client.get("/workflows", params=params)
                response.raise_for_status()
                payload = response.json()
            data = payload.get("data", []) if isinstance(payload, dict) else []
            next_c = (payload.get("nextCursor") if isinstance(payload, dict) else None) or None
            for item in data:
                if not isinstance(item, dict):
                    continue
                if active is not None and bool(item.get("active")) != active:
                    continue
                wname = str(item.get("name") or "")
                wn = wname.lower() if case_insensitive else wname
                row = _resolver_row(item)
                if wn == n_norm:
                    ranked.append((0, row))
                elif n_norm in wn:
                    ranked.append((1, row))
            cursor = next_c if isinstance(next_c, str) and next_c else None
            if not cursor:
                break
        ranked.sort(key=lambda x: x[0])
        seen: set[str] = set()
        out_rows: List[Dict[str, Any]] = []
        for _, row in ranked:
            rid = row.get("id") or ""
            if rid in seen:
                continue
            seen.add(str(rid))
            out_rows.append(row)
            if len(out_rows) >= cap:
                break
        return structured_result(
            with_response_meta(
                {"count": len(out_rows), "data": out_rows, "detail_level": "compact"},
                tool="n8n_find_workflow_by_name",
            )
        )
    except Exception as exc:
        return structured_result(_http_error("n8n_find_workflow_by_name", exc))


async def _list_all_credentials(
    admin_client: httpx.AsyncClient,
    page_limit: int = 250,
) -> List[Dict[str, Any]]:
    """Retrieve all credentials across pages."""
    credentials: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"limit": page_limit}
        if cursor:
            params["cursor"] = cursor
        response = await admin_client.get("/credentials", params=params)
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict):
            batch = payload.get("data", [])
            cursor = payload.get("nextCursor")
        elif isinstance(payload, list):
            batch = payload
            cursor = None
        else:
            batch = []
            cursor = None

        if isinstance(batch, list):
            credentials.extend([item for item in batch if isinstance(item, dict)])
        if not cursor:
            break
    return credentials


@mcp.tool(structured_output=False)
async def n8n_create_workflow(
    name: Optional[str] = None,
    summary: Optional[str] = None,
    nodes: Optional[List[Dict[str, Any]]] = None,
    connections: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
    credentials: Optional[List[Dict[str, Any]]] = None,
    active: bool = False,
    user_name: Optional[str] = None,
) -> CallToolResult:
    """Create a workflow (write).

    **Use when:**
        Authoring a new automation graph via MCP.

    **Args:**
        name, summary, nodes, connections: Required; ``summary`` becomes a sticky note in the workflow canvas and
        should briefly explain the automation for future editors. ``nodes`` must satisfy the n8n POST schema
        (node fields: id, name, type, typeVersion, position, parameters — avoid read-only/extra keys).
        settings: Optional; defaults include execution order.
        credentials: Optional list of ``{name, type, data}`` specs — creates or reuses credentials and patches nodes.
        active: When true, activates after creation.
        user_name: Optional caller identity. When provided, a ``created_by:<user_name>`` tag is added to
            the workflow so that authorship is visible in the n8n UI.

    **Returns:**
        Created workflow JSON (tagged) or error dict.

    **Notes:**
        Never embed raw secrets in chat logs; pass credential data only through tool args. Prefer inspecting nodes via
        ``n8n_list_nodes`` / ``n8n_get_node_source`` and reusing ``n8n_get_credentials`` before creating duplicates.

    **Errors:**
        ``{"error", "details"}`` for validation or HTTP failures.

    **Example:**
        ``n8n_create_workflow(name=\"Demo\", summary=\"Receives leads from Typeform and creates contacts in HubSpot.\", nodes=[...], connections={...}, user_name=\"alice\")``
    """
    if not name or not str(name).strip():
        return structured_result(
            tool_error("validation_error", details="name is required", cause="validation", retryable=False)
        )
    clean_summary = _clean_workflow_summary(summary)
    if not clean_summary:
        return structured_result(
            tool_error("validation_error", details="summary is required", cause="validation", retryable=False)
        )
    if nodes is None:
        return structured_result(
            tool_error("validation_error", details="nodes is required", cause="validation", retryable=False)
        )
    if connections is None:
        return structured_result(
            tool_error("validation_error", details="connections is required", cause="validation", retryable=False)
        )
    schedule_violations = _schedule_trigger_policy_violations(nodes)
    if schedule_violations:
        return structured_result(
            tool_error(
                "validation_error",
                details=" ".join(schedule_violations),
                cause="validation",
                retryable=False,
                suggested_fix=(
                    f"Use Schedule Trigger intervals of at least " f"{_MINIMUM_SCHEDULE_TRIGGER_INTERVAL_HOURS} hours."
                ),
            )
        )
    try:
        nodes_payload = _prepend_workflow_summary_note(nodes, clean_summary)
        if credentials:
            try:
                normalized_credentials = _normalize_credentials_specs(credentials)
            except ValueError as exc:
                return structured_result(
                    tool_error(
                        "validation_error",
                        details=str(exc),
                        cause="validation",
                        retryable=False,
                    )
                )
            _, name_to_id = await _create_credentials_from_specs(normalized_credentials)
            nodes_payload = _apply_credentials_to_nodes(nodes_payload, name_to_id)
        payload: Dict[str, Any] = {
            "name": name,
            "nodes": nodes_payload,
            "connections": connections,
            "settings": settings or {"executionOrder": "v1"},
        }
        async with get_client() as client:
            response = await client.post("/workflows", json=payload)
            if response.status_code >= 400:
                err_body = response.text
                logger.error("n8n create_workflow failed: %s %s", response.status_code, err_body)
                response.raise_for_status()
            workflow = response.json()
            workflow = await tag_workflow(
                client,
                str(workflow["id"]),
                user_name=user_name,
                is_update=False,
                workflow=workflow if isinstance(workflow, dict) else None,
            )
            if active:
                await client.post(f"/workflows/{workflow['id']}/activate")
                workflow = (await client.get(f"/workflows/{workflow['id']}")).json()
            wf_out = workflow if isinstance(workflow, dict) else {"workflow": workflow}
            if isinstance(wf_out, dict):
                wf_out["summary"] = clean_summary
                wf_out = _attach_editor_url(wf_out)
                wf_out = _attach_mcp_next_steps(wf_out, is_create=True)
            return structured_result(wf_out)
    except Exception as exc:
        return structured_result(_http_error("n8n_create_workflow", exc))


@mcp.tool(structured_output=False)
async def n8n_delete_workflow(workflow_id: Optional[str] = None) -> CallToolResult:
    """Delete an n8n workflow (write).

    **Use when:**
        Removing a workflow that should no longer exist in n8n.

    **Args:**
        workflow_id: Target workflow ID (required).

    **Returns:**
        ``{"workflow_id", "deleted"}`` on success.

    **Errors:**
        ``{"error", "details"}`` when ``workflow_id`` is missing or the upstream API rejects the delete.
    """
    if not workflow_id or not str(workflow_id).strip():
        return structured_result(
            tool_error("validation_error", details="workflow_id is required", cause="validation", retryable=False)
        )
    try:
        async with get_client() as client:
            response = await client.delete(f"/workflows/{workflow_id}")
            response.raise_for_status()
        return structured_result({"workflow_id": str(workflow_id).strip(), "deleted": True})
    except Exception as exc:
        return structured_result(_http_error("n8n_delete_workflow", exc))


# Fields the n8n API accepts in PUT /workflows/{id}. Sending staticData/pinData/meta can cause 400.
_WORKFLOW_PUT_FIELDS = (
    "name",
    "nodes",
    "connections",
    "settings",
)

# Settings keys the n8n PUT /workflows/{id} API accepts. GET returns extra keys
# (e.g. callerPolicy, timeSavedPerExecution) that cause 400 "additional properties not allowed".
# See: https://docs.n8n.io/api/api-reference/#tag/workflow/PUT/workflows/%7Bid%7D
_WORKFLOW_SETTINGS_ALLOWED_KEYS = frozenset({"executionOrder", "saveExecutionProgress", "saveManualExecutions"})


def _filter_workflow_settings(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return only settings keys that n8n PUT /workflows/{id} accepts to avoid 400."""
    if not settings:
        return {"executionOrder": "v1"}
    return {k: v for k, v in settings.items() if k in _WORKFLOW_SETTINGS_ALLOWED_KEYS} or {"executionOrder": "v1"}


# Node keys that n8n PUT /workflows/{id} accepts. webhookId and similar are read-only and cause 400.
_NODE_PUT_FIELDS = frozenset({"id", "name", "type", "typeVersion", "position", "parameters", "credentials"})


def _filter_workflow_nodes(nodes: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Return nodes with only fields that n8n PUT accepts (strip webhookId etc.)."""
    if not nodes:
        return []
    return [{k: v for k, v in n.items() if k in _NODE_PUT_FIELDS} for n in nodes]


def _node_label(node: Dict[str, Any], index: int) -> str:
    """Return a human-friendly node label for validation messages."""
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return f"node {name!r}"
    return f"nodes[{index}]"


def _coerce_positive_int(value: Any) -> Optional[int]:
    """Return a positive integer from a number/string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if value.is_integer() and value > 0:
            return int(value)
        return None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _expand_simple_cron_field(expr: str, minimum: int, maximum: int) -> Optional[Set[int]]:
    """Expand a simple cron field into a bounded integer set."""
    if not isinstance(expr, str) or not expr.strip():
        return None
    values: Set[int] = set()
    for raw_part in expr.split(","):
        part = raw_part.strip()
        if not part:
            return None
        step = 1
        base = part
        if "/" in part:
            base, step_raw = part.split("/", 1)
            step = _coerce_positive_int(step_raw)
            if step is None:
                return None
        if base == "*":
            start = minimum
            end = maximum
        elif "-" in base:
            start_raw, end_raw = base.split("-", 1)
            if not start_raw.strip().isdigit() or not end_raw.strip().isdigit():
                return None
            start = int(start_raw)
            end = int(end_raw)
        elif base.isdigit():
            start = end = int(base)
        else:
            return None
        if start < minimum or end > maximum or start > end:
            return None
        values.update(range(start, end + 1, step))
    return values or None


def _minimum_minutes_from_cron_expression(expr: str) -> Optional[int]:
    """Return the shortest gap in minutes for a simple cron expression, or None if unsupported."""
    parts = [part for part in str(expr).split() if part]
    if len(parts) == 6:
        second_values = _expand_simple_cron_field(parts[0], 0, 59)
        if second_values is None:
            return None
        if second_values != {0}:
            return 0
        minute_expr, hour_expr = parts[1], parts[2]
    elif len(parts) == 5:
        minute_expr, hour_expr = parts[0], parts[1]
    else:
        return None

    minute_values = _expand_simple_cron_field(minute_expr, 0, 59)
    hour_values = _expand_simple_cron_field(hour_expr, 0, 23)
    if minute_values is None or hour_values is None:
        return None

    scheduled_minutes = sorted((hour * 60) + minute for hour in hour_values for minute in minute_values)
    if not scheduled_minutes:
        return None
    if len(scheduled_minutes) == 1:
        return 24 * 60

    gaps = [b - a for a, b in zip(scheduled_minutes, scheduled_minutes[1:])]
    gaps.append((scheduled_minutes[0] + 24 * 60) - scheduled_minutes[-1])
    return min(gaps)


def _minimum_minutes_from_schedule_trigger_rule(rule: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    """Infer the shortest recurrence from a schedule trigger rule."""
    interval_entries = rule.get("interval")
    if isinstance(interval_entries, list) and interval_entries:
        min_minutes: Optional[int] = None
        for idx, entry in enumerate(interval_entries):
            if not isinstance(entry, dict):
                return None, f"rule.interval[{idx}] must be an object"
            field = str(entry.get("field", "")).strip().lower()
            if field in {"second", "seconds"}:
                return 0, None
            if field in {"minute", "minutes"}:
                minutes = _coerce_positive_int(entry.get("minutesInterval"))
                if minutes is None:
                    return None, f"rule.interval[{idx}] with field=minutes requires positive minutesInterval"
                current_minutes = minutes
            elif field in {"hour", "hours"}:
                hours = _coerce_positive_int(entry.get("hoursInterval"))
                if hours is None:
                    return None, f"rule.interval[{idx}] with field=hours requires positive hoursInterval"
                current_minutes = hours * 60
            elif field in {"day", "days"}:
                current_minutes = 24 * 60
            elif field in {"week", "weeks"}:
                current_minutes = 7 * 24 * 60
            elif field in {"month", "months"}:
                current_minutes = 28 * 24 * 60
            elif field in {"year", "years"}:
                current_minutes = 365 * 24 * 60
            elif field in {"cron", "cronexpression"}:
                cron_expr = entry.get("expression") or entry.get("cronExpression")
                if not isinstance(cron_expr, str) or not cron_expr.strip():
                    return None, f"rule.interval[{idx}] with field=cron requires a cron expression"
                current_minutes = _minimum_minutes_from_cron_expression(cron_expr)
                if current_minutes is None:
                    return None, (
                        f"rule.interval[{idx}] cron expression could not be verified; use a simple "
                        f"schedule of at least {_MINIMUM_SCHEDULE_TRIGGER_INTERVAL_HOURS} hours"
                    )
            else:
                return None, f"rule.interval[{idx}] uses unsupported field {field!r}"
            min_minutes = current_minutes if min_minutes is None else min(min_minutes, current_minutes)
        return min_minutes, None

    cron_expr = rule.get("cronExpression") or rule.get("expression")
    if isinstance(cron_expr, str) and cron_expr.strip():
        min_minutes = _minimum_minutes_from_cron_expression(cron_expr)
        if min_minutes is None:
            return None, (
                f"cron expression could not be verified; use a simple schedule of at least "
                f"{_MINIMUM_SCHEDULE_TRIGGER_INTERVAL_HOURS} hours"
            )
        return min_minutes, None

    return None, None


def _schedule_policy_fingerprint(node: Dict[str, Any]) -> Optional[str]:
    """Return a stable fingerprint for schedule policy comparison on update."""
    if not isinstance(node, dict):
        return None
    return json.dumps(
        {"type": node.get("type"), "parameters": node.get("parameters")},
        sort_keys=True,
        default=str,
    )


def _schedule_trigger_policy_violations(
    nodes: List[Dict[str, Any]],
    *,
    previous_nodes: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Return schedule-trigger policy violations for new or modified nodes."""
    previous_by_id: Dict[str, Dict[str, Any]] = {}
    previous_by_name: Dict[str, Dict[str, Any]] = {}
    if previous_nodes:
        for previous in previous_nodes:
            if not isinstance(previous, dict):
                continue
            prev_id = previous.get("id")
            prev_name = previous.get("name")
            if prev_id is not None:
                previous_by_id[str(prev_id)] = previous
            if isinstance(prev_name, str) and prev_name.strip():
                previous_by_name[prev_name] = previous

    violations: List[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or node.get("type") != "n8n-nodes-base.scheduleTrigger":
            continue
        previous = None
        node_id = node.get("id")
        node_name = node.get("name")
        if node_id is not None:
            previous = previous_by_id.get(str(node_id))
        if previous is None and isinstance(node_name, str) and node_name.strip():
            previous = previous_by_name.get(node_name)
        if previous is not None and _schedule_policy_fingerprint(previous) == _schedule_policy_fingerprint(node):
            continue

        parameters = node.get("parameters")
        if not isinstance(parameters, dict):
            continue
        rule = parameters.get("rule")
        if not isinstance(rule, dict):
            continue

        min_minutes, parse_error = _minimum_minutes_from_schedule_trigger_rule(rule)
        label = _node_label(node, index)
        if parse_error:
            violations.append(
                f"{label} uses a Schedule Trigger configuration that could not be verified against the "
                f"{_MINIMUM_SCHEDULE_TRIGGER_INTERVAL_HOURS}-hour minimum: {parse_error}."
            )
            continue
        if min_minutes is None:
            continue
        if min_minutes < _MINIMUM_SCHEDULE_TRIGGER_INTERVAL_MINUTES:
            violations.append(
                f"{label} uses a Schedule Trigger that runs more often than every "
                f"{_MINIMUM_SCHEDULE_TRIGGER_INTERVAL_HOURS} hours. Minute-based schedules and intervals under "
                f"{_MINIMUM_SCHEDULE_TRIGGER_INTERVAL_HOURS} hours are not allowed."
            )
    return violations


def _merge_node_dict(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merge patch into base; shallow-merge parameters dicts when both are objects."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if k == "parameters" and isinstance(v, dict) and isinstance(out.get("parameters"), dict):
            out["parameters"] = {**out["parameters"], **v}
        else:
            out[k] = copy.deepcopy(v) if isinstance(v, (dict, list)) else v
    return out


def _merge_workflow_nodes_by_id(
    existing: List[Dict[str, Any]],
    patch: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge patch nodes into existing by node id, preserve order, append nodes whose ids are new."""
    patch_by_id: Dict[str, Dict[str, Any]] = {}
    for n in patch:
        if isinstance(n, dict) and n.get("id") is not None:
            patch_by_id[str(n["id"])] = n

    existing_ids: set[str] = set()
    for n in existing:
        if isinstance(n, dict) and n.get("id") is not None:
            existing_ids.add(str(n["id"]))

    out: List[Dict[str, Any]] = []
    for n in existing:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        if nid is None:
            out.append(copy.deepcopy(n))
            continue
        sid = str(nid)
        if sid in patch_by_id:
            out.append(_merge_node_dict(n, patch_by_id[sid]))
        else:
            out.append(copy.deepcopy(n))

    for n in patch:
        if not isinstance(n, dict):
            continue
        if n.get("id") is None:
            out.append(copy.deepcopy(n))
            continue
        sid = str(n["id"])
        if sid not in existing_ids:
            out.append(copy.deepcopy(n))
    return out


@mcp.tool(structured_output=False)
async def n8n_update_workflow(
    workflow_id: Optional[str] = None,
    name: Optional[str] = None,
    nodes: Optional[List[Dict[str, Any]]] = None,
    connections: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
    credentials: Optional[List[Dict[str, Any]]] = None,
    active: Optional[bool] = None,
    nodes_replace: bool = False,
    user_name: Optional[str] = None,
) -> CallToolResult:
    """Update an existing n8n workflow (write).

    **Use when:**
        You need to modify workflow metadata, nodes, connections, settings, or activation state.

    **Args:**
        workflow_id: Target workflow ID (required).
        name: Optional new name; omit to keep the current name.
        nodes: Optional node list updates. By default merged **by node ``id``** into the existing nodes (same id is
            deep-merged; ``parameters`` dicts shallow-merge). When ``nodes_replace`` is true, replaces the **entire**
            ``nodes`` array with this list.
        nodes_replace: When false (default), merge ``nodes`` by id; when true, replace the full nodes list.
        connections: When provided, **replaces** the workflow ``connections`` object entirely; omit to preserve.
        settings: When provided, **replaces** ``settings`` with your object (allowed keys are filtered for the API);
            omit to preserve.
        active: Optional activation flag; omit to leave activation unchanged (after PUT, a separate activate /
            deactivate call may run).
        credentials: Optional list of ``{name, type, data}`` specs — creates or reuses credentials **by name+type**
            and injects credential ids into matching nodes.
        user_name: Optional caller identity. When provided and the caller differs from the workflow's existing
            ``created_by:*`` owner, a ``last_update:<user_name>`` tag replaces any prior ``last_update:*`` tag so
            that the last editor is always visible in the n8n UI.

    **Returns:**
        Updated workflow object (JSON) on success.

    **Notes:**
        The server GETs the workflow, merges arguments, strips read-only PUT fields, then PUTs. **Omitted arguments
        stay unchanged.** Call ``n8n_get_workflow`` first for a safe merge. Use ``n8n_get_credentials`` before
        creating credentials to avoid duplicates.

    **Errors:**
        ``{"error", "details"}`` when ``workflow_id`` is missing, credentials are invalid, or HTTP fails.

    **Example:**
        ``n8n_update_workflow(workflow_id=\"42\", nodes=[{\"id\": \"abc\", \"parameters\": {\"url\": \"https://example.com\"}}], user_name=\"bob\")``
    """
    if not workflow_id or not str(workflow_id).strip():
        return structured_result(
            tool_error("validation_error", details="workflow_id is required", cause="validation", retryable=False)
        )
    try:
        async with get_client() as client:
            get_resp = await client.get(f"/workflows/{workflow_id}")
            get_resp.raise_for_status()
            existing = get_resp.json()
            if isinstance(existing, dict):
                existing = await ensure_workflow_has_mcp_tag(client, str(workflow_id), workflow=existing)

            merged: Dict[str, Any] = dict(existing)
            if name is not None:
                merged["name"] = name
            if nodes is not None:
                existing_nodes = merged.get("nodes") or []
                if not isinstance(existing_nodes, list):
                    existing_nodes = []
                merged["nodes"] = (
                    nodes
                    if nodes_replace
                    else _merge_workflow_nodes_by_id(
                        [n for n in existing_nodes if isinstance(n, dict)],
                        nodes,
                    )
                )
            if connections is not None:
                merged["connections"] = connections
            if settings is not None:
                merged["settings"] = settings
            if credentials:
                try:
                    normalized_credentials = _normalize_credentials_specs(credentials)
                except ValueError as exc:
                    return structured_result(
                        tool_error(
                            "validation_error",
                            details=str(exc),
                            cause="validation",
                            retryable=False,
                        )
                    )
                _, name_to_id = await _create_credentials_from_specs(normalized_credentials)
                merged_nodes = merged.get("nodes", [])
                merged["nodes"] = _apply_credentials_to_nodes(merged_nodes, name_to_id)
            previous_active = bool(existing.get("active"))
            requested_active = active

            payload = {k: merged[k] for k in _WORKFLOW_PUT_FIELDS if k in merged and merged[k] is not None}
            # Only send settings keys that n8n PUT accepts; GET returns extra keys that cause 400
            payload["settings"] = _filter_workflow_settings(payload.get("settings") or merged.get("settings"))
            # Strip node keys that PUT rejects (e.g. webhookId)
            payload["nodes"] = _filter_workflow_nodes(payload.get("nodes") or merged.get("nodes"))
            schedule_violations = _schedule_trigger_policy_violations(
                payload["nodes"],
                previous_nodes=[n for n in existing.get("nodes", []) if isinstance(n, dict)],
            )
            if schedule_violations:
                return structured_result(
                    tool_error(
                        "validation_error",
                        details=" ".join(schedule_violations),
                        cause="validation",
                        retryable=False,
                        suggested_fix=(
                            f"Use Schedule Trigger intervals of at least "
                            f"{_MINIMUM_SCHEDULE_TRIGGER_INTERVAL_HOURS} hours."
                        ),
                    )
                )

            response = await client.put(f"/workflows/{workflow_id}", json=payload)
            if response.status_code == 400:
                err_body = response.text
                logger.error(
                    "n8n PUT /workflows/%s returned 400: %s",
                    workflow_id,
                    err_body,
                )
                raise httpx.HTTPStatusError(
                    f"n8n workflow update failed (400): {err_body}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            workflow = response.json()
            workflow = await tag_workflow(
                client,
                workflow_id,
                user_name=user_name,
                is_update=True,
                workflow=workflow if isinstance(workflow, dict) else None,
            )
            if requested_active is not None and requested_active != previous_active:
                endpoint = (
                    f"/workflows/{workflow_id}/deactivate"
                    if not requested_active
                    else f"/workflows/{workflow_id}/activate"
                )
                act_resp = await client.post(endpoint)
                # If regular API key lacks scope (e.g. not owner), retry with admin key when configured
                if act_resp.status_code in (400, 403) and "scope" in (act_resp.text or "").lower():
                    try:
                        async with get_admin_client() as admin_client:
                            act_resp = await admin_client.post(endpoint)
                            act_resp.raise_for_status()
                            refreshed = await admin_client.get(f"/workflows/{workflow_id}")
                            refreshed.raise_for_status()
                            workflow = refreshed.json()
                    except ValueError:
                        act_resp.raise_for_status()
                    except httpx.HTTPStatusError:
                        raise
                else:
                    act_resp.raise_for_status()
                    refreshed = await client.get(f"/workflows/{workflow_id}")
                    refreshed.raise_for_status()
                    workflow = refreshed.json()
            wf_out = workflow if isinstance(workflow, dict) else {"workflow": workflow}
            if isinstance(wf_out, dict):
                wf_out = _attach_editor_url(wf_out)
                wf_out = _attach_mcp_next_steps(wf_out, is_create=False)
            return structured_result(wf_out)
    except Exception as exc:
        return structured_result(_http_error("n8n_update_workflow", exc))


# -- Credential helpers ------------------------------------------------------


def _normalize_credentials_specs(credentials: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Clean credential specs: requires name, type, and optional data object."""
    normalized: List[Dict[str, Any]] = []
    single = len(credentials) == 1
    for idx, spec in enumerate(credentials):
        if not isinstance(spec, dict):
            raise ValueError(
                "Each credential must be an object with name, type, and optional data"
                if single
                else f"credentials[{idx}] must be an object with name/type/data"
            )
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                "name is required and must be a non-empty string"
                if single
                else f"credentials[{idx}] requires a non-empty name"
            )
        credential_type = spec.get("type")
        if not isinstance(credential_type, str) or not credential_type.strip():
            raise ValueError(
                "credential_type is required and must be a non-empty string"
                if single
                else f"credentials[{idx}] requires a non-empty type"
            )
        data = spec.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("data must be an object" if single else f"credentials[{idx}].data must be an object")
        normalized.append({"name": name.strip(), "type": credential_type.strip(), "data": data})
    return normalized


async def _create_credentials_from_specs(
    credentials: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Create missing credentials and reuse existing ones by (name, type)."""
    responses: List[Dict[str, Any]] = []
    name_to_id: Dict[str, str] = {}
    async with get_admin_client() as admin_client:
        existing_credentials = await _list_all_credentials(admin_client)
        existing_by_name_type: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for existing in existing_credentials:
            existing_name = existing.get("name")
            existing_type = existing.get("type")
            if isinstance(existing_name, str) and isinstance(existing_type, str):
                existing_by_name_type[(existing_name, existing_type)] = existing
        for spec in credentials:
            key = (spec["name"], spec["type"])
            existing = existing_by_name_type.get(key)
            if existing:
                existing_id = str(existing.get("id", ""))
                if existing_id:
                    name_to_id[spec["name"]] = existing_id
                responses.append(
                    {
                        "id": existing_id,
                        "name": spec["name"],
                        "type": spec["type"],
                        "reused": True,
                    }
                )
                continue
            response = await admin_client.post("/credentials", json=spec)
            response.raise_for_status()
            credential = response.json()
            responses.append(credential)
            created_id = str(credential.get("id", ""))
            name_to_id[spec["name"]] = created_id
            existing_by_name_type[key] = {"id": created_id, "name": spec["name"], "type": spec["type"]}
    return responses, name_to_id


def _patch_credentials_dict(credentials: Dict[str, Any], name_to_id: Dict[str, str]) -> Dict[str, Any]:
    """Return a credentials dict with new ids injected for matching names."""
    patched: Dict[str, Any] = {}
    for cred_type, cred_config in credentials.items():
        if isinstance(cred_config, dict):
            cred_name = cred_config.get("name")
            if isinstance(cred_name, str) and cred_name in name_to_id:
                config_copy = dict(cred_config)
                config_copy["id"] = name_to_id[cred_name]
                patched[cred_type] = config_copy
                continue
        patched[cred_type] = cred_config
    return patched


def _apply_credentials_to_nodes(nodes: List[Dict[str, Any]], name_to_id: Dict[str, str]) -> List[Dict[str, Any]]:
    """Return a deep-copied node list where credentials matching names get ids."""
    if not name_to_id:
        return nodes
    patched_nodes: List[Dict[str, Any]] = []
    for node in nodes:
        node_copy = copy.deepcopy(node)
        credentials = node_copy.get("credentials")
        if isinstance(credentials, dict):
            node_copy["credentials"] = _patch_credentials_dict(credentials, name_to_id)
        patched_nodes.append(node_copy)
    return patched_nodes


# -- Credential tools -------------------------------------------------------


@mcp.tool(structured_output=False)
async def n8n_get_credentials(
    limit: int = 50,
    cursor: Optional[str] = None,
    fetch_all: bool = False,
    summary_only: bool = True,
    detail_level: str = "compact",
) -> CallToolResult:
    """List n8n credentials without exposing secret values (read/list).

    **Use when:**
        You need to discover existing credentials before creating or updating workflows.

    **Args:**
        cursor: Optional pagination cursor from a previous response's ``nextCursor``.
        fetch_all: When true, fetches every page automatically. **Warning:** higher latency, more load, and larger
            payloads — use only when you need the full set (e.g. deduplication before bulk edits).
        limit: Maximum credentials per page (capped by the implementation when using ``fetch_all``).
        summary_only: When true (default), each item is a compact record
            (``id``, ``name``, ``type``, ``updatedAt``). When false, returns richer credential metadata from the API
            **still without secret values**.

    **Returns:**
        ``{\"data\": [{\"id\", \"name\", \"type\", \"updatedAt\"}, ...], \"nextCursor\": str | null}`` in the typical
        summary case; shape matches the n8n API when ``summary_only`` is false. Empty registry → ``data: []``.

    **Notes:**
        Pagination: pass ``cursor`` from ``nextCursor``; with ``fetch_all``, ``nextCursor`` is null after aggregation.
        **Secret values are never returned.** Call this before ``n8n_create_credential`` or passing ``credentials``
        to workflow tools to avoid duplicates.

    **Errors:**
        ``{"error", "details"}`` on HTTP/API failure; empty ``data`` is not an error.

    **Example:**
        ``n8n_get_credentials(limit=50, summary_only=True)``
    """
    dl = parse_detail_level(detail_level, default="compact")
    try:
        async with get_admin_client() as client:
            pagination = {"limit": limit, "cursor": cursor}
            if fetch_all:
                data = await _list_all_credentials(client, page_limit=max(1, min(limit, 250)))
                if summary_only:
                    data = [_credential_summary(item) for item in data if isinstance(item, dict)]
                return structured_result(
                    with_response_meta(
                        {"data": data, "nextCursor": None, "detail_level": dl},
                        tool="n8n_get_credentials",
                        pagination=pagination,
                    )
                )
            params: Dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            response = await client.get("/credentials", params=params)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                data = (
                    [_credential_summary(item) for item in payload if isinstance(item, dict)]
                    if summary_only
                    else payload
                )
                return structured_result(
                    with_response_meta(
                        {"data": data, "nextCursor": None, "detail_level": dl},
                        tool="n8n_get_credentials",
                        pagination=pagination,
                    )
                )
            if isinstance(payload, dict) and summary_only:
                data = payload.get("data", [])
                return structured_result(
                    with_response_meta(
                        {
                            "data": [_credential_summary(item) for item in data if isinstance(item, dict)],
                            "nextCursor": payload.get("nextCursor"),
                            "detail_level": dl,
                        },
                        tool="n8n_get_credentials",
                        pagination=pagination,
                    )
                )
            return structured_result(
                with_response_meta(
                    (
                        {**payload, "detail_level": dl}
                        if isinstance(payload, dict)
                        else {"data": payload, "detail_level": dl}
                    ),
                    tool="n8n_get_credentials",
                    pagination=pagination,
                )
            )
    except Exception as exc:
        return structured_result(_http_error("n8n_get_credentials", exc))


@mcp.tool(structured_output=False)
async def n8n_get_credential_schema(credential_type: Optional[str] = None) -> CallToolResult:
    """Get credential type schema (read/detail).

    **Use when:**
        Building the ``data`` object for ``n8n_create_credential`` / workflow credential specs.

    **Args:**
        credential_type: n8n credential type key (required).

    **Returns:**
        Passthrough schema JSON.

    **Notes:**
        Run before ``n8n_create_credential`` to avoid trial-and-error payloads.

    **Errors:**
        ``{"error", "details"}`` when ``credential_type`` is missing or HTTP fails.

    **Example:**
        ``n8n_get_credential_schema(credential_type=\"slackOAuth2Api\")``
    """
    if not credential_type or not str(credential_type).strip():
        return structured_result(
            tool_error(
                "validation_error",
                details="credential_type is required",
                cause="validation",
                retryable=False,
            )
        )
    try:
        async with get_admin_client() as client:
            response = await client.get(f"/credentials/schema/{credential_type}")
            response.raise_for_status()
            body = response.json()
            return structured_result(body if isinstance(body, dict) else {"schema": body, "detail_level": "full"})
    except Exception as exc:
        return structured_result(_http_error("n8n_get_credential_schema", exc))


@mcp.tool(structured_output=False)
async def n8n_create_credential(
    name: Optional[str] = None,
    credential_type: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> CallToolResult:
    """Create a credential (write).

    **Use when:**
        A workflow needs a new stored secret reference.

    **Args:**
        name: Credential display name. credential_type: Type key. data: Secret fields per ``n8n_get_credential_schema``.

    **Returns:**
        Created credential dict (includes id).

    **Notes:**
        Prefer ``n8n_get_credentials`` + schema first. Treat ``data`` as secret-bearing.

    **Errors:**
        ``{"error", "details"}`` on validation or HTTP failure.

    **Example:**
        ``n8n_create_credential(name=\"slack_prod\", credential_type=\"slackApi\", data={...})``
    """
    specs = [{"name": name, "type": credential_type, "data": data}]
    try:
        normalized = _normalize_credentials_specs(specs)
    except ValueError as exc:
        return structured_result(
            tool_error(
                "validation_error",
                details=str(exc),
                cause="validation",
                retryable=False,
            )
        )
    try:
        created, _ = await _create_credentials_from_specs(normalized)
        return structured_result(created[0] if isinstance(created[0], dict) else {"credential": created[0]})
    except Exception as exc:
        return structured_result(_http_error("n8n_create_credential", exc))


# -- Execution tools --------------------------------------------------------


@mcp.tool(structured_output=False)
async def n8n_get_executions(
    limit: int = 50,
    workflow_id: Optional[str] = None,
    status: Optional[str] = None,
    cursor: Optional[str] = None,
    summary_only: bool = False,
    detail_level: str = "compact",
) -> CallToolResult:
    """List past workflow executions with filters and cursor pagination (list / read).

    Prefer this after ``n8n_find_workflow_by_name`` + ``n8n_get_workflow`` when debugging runs; it does not start executions.

    Typical input: optional ``workflow_id``, ``status``, ``limit``, ``cursor``.

    **Use when:**
        Debugging failures or reviewing run history (does **not** start runs).

    **Args:**
        limit, cursor: Pagination.
        workflow_id, status: Filters (e.g. success, failed, waiting, running).
        summary_only: Compact rows when true.

    **Returns:**
        ``{\"data\": [...], \"nextCursor\": ...}`` style payload.

    **Notes:**
        Pagination uses ``cursor`` / ``nextCursor``. This tool does not start runs — use webhooks for triggers.

    **Errors:**
        ``{"error", "details"}`` on HTTP failure.

    **Example:**
        ``n8n_get_executions(limit=10, workflow_id=\"42\", summary_only=True)``
    """
    limit = coerce_int(limit, default=50, minimum=1, maximum=250)
    params: Dict[str, Any] = {"limit": limit}
    if workflow_id:
        params["workflowId"] = workflow_id
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor
    dl = parse_detail_level(detail_level, default="compact")
    try:
        async with get_client() as client:
            response = await client.get("/executions", params=params)
            response.raise_for_status()
            payload = response.json()
            pagination = {"limit": limit, "cursor": cursor}
            if not summary_only:
                return structured_result(
                    with_response_meta(
                        (
                            {**payload, "detail_level": "full"}
                            if isinstance(payload, dict)
                            else {"data": payload, "detail_level": "full"}
                        ),
                        tool="n8n_get_executions",
                        pagination=pagination,
                    )
                )
            if isinstance(payload, dict):
                data = payload.get("data", [])
                summarized = [_execution_summary(item) for item in data if isinstance(item, dict)]
                return structured_result(
                    with_response_meta(
                        {"data": summarized, "nextCursor": payload.get("nextCursor"), "detail_level": dl},
                        tool="n8n_get_executions",
                        pagination=pagination,
                    )
                )
            if isinstance(payload, list):
                return structured_result(
                    with_response_meta(
                        {
                            "data": [_execution_summary(item) for item in payload if isinstance(item, dict)],
                            "nextCursor": None,
                            "detail_level": dl,
                        },
                        tool="n8n_get_executions",
                        pagination=pagination,
                    )
                )
            return structured_result(
                with_response_meta(
                    {"data": [], "nextCursor": None, "detail_level": dl},
                    tool="n8n_get_executions",
                    pagination=pagination,
                )
            )
    except Exception as exc:
        return structured_result(_http_error("n8n_get_executions", exc))


@mcp.tool(structured_output=False)
async def n8n_stop_execution(execution_id: Optional[str] = None) -> CallToolResult:
    """Stop a running/waiting execution (write).

    **Use when:**
        You need to cancel a stuck or long-running execution.

    **Args:**
        execution_id: n8n execution id (required).

    **Returns:**
        API JSON response body.

    **Notes:**
        Only works for executions n8n allows stopping.

    **Errors:**
        ``{"error": "validation_error", ...}`` when ``execution_id`` is missing or non-numeric — never forwarded upstream.
        ``{"error", "details"}`` on HTTP failure.

    **Example:**
        ``n8n_stop_execution(execution_id=\"12345\")``
    """
    raw_id = str(execution_id).strip() if execution_id is not None else ""
    if not raw_id:
        return structured_result(
            tool_error("validation_error", details="execution_id is required", cause="validation", retryable=False)
        )
    if not raw_id.lstrip("-").isdigit() or int(raw_id) <= 0:
        return structured_result(
            tool_error(
                "validation_error",
                details=f"execution_id must be a positive integer (got {execution_id!r}). "
                "n8n execution IDs are numeric.",
                cause="validation",
                retryable=False,
                suggested_fix="Pass a valid numeric n8n execution id (visible in the executions list).",
            )
        )
    try:
        async with get_client() as client:
            response = await client.post(f"/executions/{raw_id}/stop")
            response.raise_for_status()
            body = response.json()
            return structured_result(body if isinstance(body, dict) else {"data": body})
    except Exception as exc:
        return structured_result(_http_error("n8n_stop_execution", exc))


@mcp.tool(structured_output=False)
async def n8n_retry_execution(execution_id: Optional[str] = None) -> CallToolResult:
    """Retry a failed execution (write).

    **Use when:**
        You want n8n to re-run a failed execution record.

    **Args:**
        execution_id: Execution id to retry (required).

    **Returns:**
        API JSON response body.

    **Errors:**
        ``{"error": "validation_error", ...}`` when ``execution_id`` is missing or non-numeric — never forwarded upstream.
        ``{"error", "details"}`` on HTTP failure.

    **Example:**
        ``n8n_retry_execution(execution_id=\"12345\")``
    """
    raw_id = str(execution_id).strip() if execution_id is not None else ""
    if not raw_id:
        return structured_result(
            tool_error("validation_error", details="execution_id is required", cause="validation", retryable=False)
        )
    if not raw_id.lstrip("-").isdigit() or int(raw_id) <= 0:
        return structured_result(
            tool_error(
                "validation_error",
                details=f"execution_id must be a positive integer (got {execution_id!r}). "
                "n8n execution IDs are numeric.",
                cause="validation",
                retryable=False,
                suggested_fix="Pass a valid numeric n8n execution id (visible in the executions list).",
            )
        )
    try:
        async with get_client() as client:
            response = await client.post(f"/executions/{raw_id}/retry")
            response.raise_for_status()
            body = response.json()
            return structured_result(body if isinstance(body, dict) else {"data": body})
    except Exception as exc:
        return structured_result(_http_error("n8n_retry_execution", exc))


# -- n8n Nodes source browser (GitHub) --------------------------------------


async def _github_list_node_folder_names() -> List[str]:
    """Return sorted node package directory names from the n8n GitHub nodes tree."""
    url = f"{GITHUB_API}/repos/{N8N_NODES_REPO}/contents/{N8N_NODES_PATH}?ref={N8N_NODES_BRANCH}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=github_headers())
        response.raise_for_status()
        items = response.json()
        return sorted([item["name"] for item in items if item.get("type") == "dir"])


def _score_node_folder_matches(query: str, folders: List[str]) -> List[Tuple[str, int, str]]:
    """Return (folder, score, reason) sorted by score descending."""
    q = (query or "").strip().lower()
    if not q:
        return []
    tokens = [t for t in re.split(r"[\s_\-/]+", q) if t]
    ranked: List[Tuple[str, int, str]] = []
    for folder in folders:
        fl = folder.lower()
        score = 0
        reasons: List[str] = []
        if q == fl:
            score += 200
            reasons.append("exact_name")
        elif q in fl:
            score += 80
            reasons.append("substring_query_in_folder")
        elif fl in q:
            score += 40
            reasons.append("substring_folder_in_query")
        for t in tokens:
            if t and t in fl:
                score += 15
                reasons.append(f"token:{t}")
        if score > 0:
            ranked.append((folder, score, ",".join(sorted(set(reasons)))))
    ranked.sort(key=lambda x: -x[1])
    return ranked


def _folder_from_nodes_path(path: str) -> Optional[str]:
    """Extract package folder from a repo path like nodes/Slack/Slack.node.ts."""
    if not path or not isinstance(path, str):
        return None
    prefix = f"{N8N_NODES_PATH}/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix) :].strip("/")
    if not rest:
        return None
    return rest.split("/")[0]


def _validate_workflow_definition_body(
    *,
    name: Optional[str],
    nodes: Optional[List[Any]],
    connections: Optional[Dict[str, Any]],
    settings: Optional[Dict[str, Any]],
) -> Tuple[bool, List[str], List[str]]:
    """Structural validation only — no n8n API calls. Returns (valid, errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            errors.append("name must be a non-empty string when provided")
    if nodes is None and connections is None and settings is None and name is None:
        errors.append("Provide at least one of: name, nodes, connections, settings")
        return False, errors, warnings

    if connections is not None and nodes is None:
        errors.append("Provide `nodes` when validating `connections` (links reference node.name values).")

    if nodes is not None or connections is not None:
        warnings.append("Node parameter requirements are not validated here — use n8n_get_node_source when in doubt.")

    node_names: Set[str] = set()
    node_ids: Set[str] = set()
    if nodes is not None:
        if not isinstance(nodes, list):
            errors.append("nodes must be a list when provided")
        else:
            for i, raw in enumerate(nodes):
                if not isinstance(raw, dict):
                    errors.append(f"nodes[{i}] must be an object")
                    continue
                nid = raw.get("id")
                nname = raw.get("name")
                if nid is None or (isinstance(nid, str) and not str(nid).strip()):
                    errors.append(f"nodes[{i}].id is required")
                else:
                    sid = str(nid)
                    if sid in node_ids:
                        errors.append(f"duplicate node id: {sid!r}")
                    node_ids.add(sid)
                if not isinstance(nname, str) or not nname.strip():
                    errors.append(f"nodes[{i}].name is required (non-empty string)")
                else:
                    if nname in node_names:
                        errors.append(f"duplicate node name: {nname!r} (connections use names)")
                    node_names.add(nname)
                if not isinstance(raw.get("type"), str) or not str(raw.get("type")).strip():
                    errors.append(f"nodes[{i}].type is required")
                tv = raw.get("typeVersion")
                if tv is None:
                    errors.append(f"nodes[{i}].typeVersion is required")
                elif not isinstance(tv, (int, float)):
                    errors.append(f"nodes[{i}].typeVersion must be a number")
                pos = raw.get("position")
                if not isinstance(pos, list) or len(pos) < 2:
                    errors.append(f"nodes[{i}].position must be a list of at least two numbers")
                else:
                    if not all(isinstance(x, (int, float)) for x in pos[:2]):
                        errors.append(f"nodes[{i}].position entries must be numbers")
                params = raw.get("parameters")
                if params is None:
                    errors.append(f"nodes[{i}].parameters is required (use {{}} if empty)")
                elif not isinstance(params, dict):
                    errors.append(f"nodes[{i}].parameters must be an object")
                extra_keys = set(raw.keys()) - _NODE_PUT_FIELDS
                if extra_keys:
                    warnings.append(
                        f"nodes[{i}] has keys not accepted on n8n PUT (may be stripped or rejected): "
                        f"{sorted(extra_keys)}"
                    )
            errors.extend(_schedule_trigger_policy_violations([n for n in nodes if isinstance(n, dict)]))

    if settings is not None:
        if not isinstance(settings, dict):
            errors.append("settings must be an object when provided")
        else:
            bad = [k for k in settings.keys() if k not in _WORKFLOW_SETTINGS_ALLOWED_KEYS]
            if bad:
                errors.append(
                    "settings contains keys not allowed on n8n PUT: "
                    f"{bad} — allowed: {sorted(_WORKFLOW_SETTINGS_ALLOWED_KEYS)}"
                )

    if connections is not None:
        if not isinstance(connections, dict):
            errors.append("connections must be an object when provided")
        elif node_names:
            for src_name, outs in connections.items():
                if not isinstance(src_name, str):
                    errors.append(f"connections key must be string (source node name), got {type(src_name)}")
                    continue
                if src_name not in node_names:
                    errors.append(
                        f"connections source {src_name!r} has no node with that name "
                        f"(connection keys must match node.name)"
                    )
                if not isinstance(outs, dict):
                    errors.append(f"connections[{src_name!r}] must be an object with e.g. main: [...]")
                    continue
                main_block = outs.get("main")
                if main_block is not None:
                    if not isinstance(main_block, list):
                        errors.append(f"connections[{src_name!r}].main must be a list")
                    else:
                        for bi, branch in enumerate(main_block):
                            if not isinstance(branch, list):
                                errors.append(f"connections[{src_name!r}].main[{bi}] must be a list of link objects")
                                continue
                            for li, link in enumerate(branch):
                                if not isinstance(link, dict):
                                    errors.append(f"connections[{src_name!r}].main[{bi}][{li}] must be a link object")
                                    continue
                                tgt = link.get("node")
                                if not isinstance(tgt, str) or not tgt.strip():
                                    errors.append(
                                        f'connections[{src_name!r}].main[{bi}][{li}] needs string "node" (target name)'
                                    )
                                elif tgt not in node_names:
                                    errors.append(
                                        f"link target {tgt!r} is not a node.name "
                                        f"(from connections[{src_name!r}].main[{bi}][{li}])"
                                    )

    valid = not errors
    return valid, errors, warnings


WORKFLOW_STARTER_MINIMAL_JSON = (
    json.dumps(
        {
            "name": "Starter (replace name)",
            "nodes": [
                {
                    "id": "1",
                    "name": "Manual Trigger",
                    "type": "n8n-nodes-base.manualTrigger",
                    "typeVersion": 1,
                    "position": [0, 0],
                    "parameters": {},
                },
                {
                    "id": "2",
                    "name": "Set",
                    "type": "n8n-nodes-base.set",
                    "typeVersion": 3,
                    "position": [220, 0],
                    "parameters": {"assignments": {"assignments": []}},
                },
            ],
            "connections": {
                "Manual Trigger": {"main": [[{"node": "Set", "type": "main", "index": 0}]]},
            },
            "settings": {"executionOrder": "v1"},
        },
        indent=2,
    )
    + "\n"
)

# Prompt `n8n_minimal_workflow_editor` — static rules (shared with MCP resources for inspection).
N8N_MINIMAL_WORKFLOW_EDITOR_STATIC = """You are working on n8n workflows. Your goal is to produce the smallest correct workflow with the least noise.

Behavior rules:
- Ask at most 2 clarifying questions total.
- If minor details are missing, make the safest reasonable assumption and state it briefly.
- Do not brainstorm broadly unless I ask for options.
- Do not explain n8n basics unless I ask.

Workflow rules:
- Prefer updating an existing workflow over creating a new one.
- Reuse existing credentials before creating new credentials.
- For Slack nodes, default to the credential named `Slack n8n Bot` unless the user explicitly asks for another Slack credential or the existing workflow already uses a different approved Slack credential.
- Reuse existing nodes and structure when possible.
- Keep the workflow minimal, clean, and easy to read.
- Prefer native n8n nodes and expressions over Code nodes.
- Use a Code node only when the same result cannot be done cleanly with standard nodes.
- Do not add placeholder nodes, Note nodes, disabled nodes, test nodes, or temporary nodes in the final result.
- Do not add extra Set/Edit Fields nodes unless they clearly reduce complexity.
- Do not create duplicate branches, duplicate logic, or duplicate credentials.
- Do not add retries, error branches, fallback logic, logging, or "future-proofing" unless I explicitly ask.
- Do not rename, move, or refactor unrelated parts of the workflow.
- Do not activate the workflow unless I explicitly ask.
- Do not create or suggest Schedule Trigger recurrences under 2 hours.
- Do not use minute-based schedules; the minimum allowed recurring schedule is every 2 hours.
- Do not create or update workflows without the required `mcp` tag.

Decision rules:
- Before making changes, inspect the current workflow if one already exists.
- If there is a simple solution and a complex solution, choose the simple solution.
- If a tradeoff exists, tell me in 1-2 short sentences and then proceed with the simpler option by default.
- If a requested design is noisy, brittle, or unnecessarily complex, say so clearly and propose the cleaner version.

Output rules:
- First, give a 1-2 sentence plan.
- Then make the change.
- At the end, report only:
  1. What changed
  2. Any assumptions made
  3. Any risks or follow-up items
- Keep the final explanation short and concrete.

Use the n8n MCP tools as needed (e.g. n8n_get_workflow before updates, n8n_get_credentials before creating credentials). Help: mcp_proxy://help/by-task/n8n-create-workflow"""


def n8n_minimal_workflow_editor_full_text(task: str) -> str:
    """Full prompt body for `n8n_minimal_workflow_editor` (rules + Current task section)."""
    return f"{N8N_MINIMAL_WORKFLOW_EDITOR_STATIC.rstrip()}\n\n---\nCurrent task:\n{task}\n"


@mcp.resource("n8n://prompts/n8n-minimal-workflow-editor")
def n8n_resource_minimal_workflow_editor_full() -> str:
    """Full template matching prompt `n8n_minimal_workflow_editor`; task line shows where `task` is injected.

    Read via ``resources/read`` when the client lists prompts but does not expose prompt message bodies.
    The MCP prompt appends your ``task`` argument under **Current task** (same structure as below).
    """
    return n8n_minimal_workflow_editor_full_text(
        "<supply via MCP prompts/get on prompt `n8n_minimal_workflow_editor`, argument `task`>"
    )


@mcp.resource("n8n://prompts/n8n-minimal-workflow-editor/static")
def n8n_resource_minimal_workflow_editor_static() -> str:
    """Rules-only body of `n8n_minimal_workflow_editor` (everything before the ``---`` / Current task block)."""
    return N8N_MINIMAL_WORKFLOW_EDITOR_STATIC.strip() + "\n"


@mcp.resource("n8n://templates/workflow-starter-minimal")
def n8n_workflow_starter_minimal_resource() -> str:
    """Minimal valid-style workflow JSON for n8n_create_workflow (manual trigger → Set)."""
    return WORKFLOW_STARTER_MINIMAL_JSON


@mcp.tool(structured_output=False)
async def n8n_list_nodes(structured: bool = True) -> CallToolResult:
    """List n8n node package folders from GitHub (read/list).

    **Use when:**
        Exploring which node implementations exist before opening source files.

    **Returns:**
        ``{\"items\"|\"names\", \"count\", \"detail_level\"}`` depending on ``structured``.

    **Notes:**
        Follow with ``n8n_list_node_files`` / ``n8n_get_node_source`` for parameter detail.

    **Errors:**
        ``{"error", "details"}`` on GitHub HTTP failures.

    **Example:**
        ``n8n_list_nodes(structured=True)``
    """
    try:
        names = await _github_list_node_folder_names()
        if not structured:
            return structured_result(
                with_response_meta(
                    {"names": names, "count": len(names), "detail_level": "compact"},
                    tool="n8n_list_nodes",
                    data_from="names",
                )
            )
        return structured_result(
            with_response_meta(
                {"items": [{"name": name} for name in names], "count": len(names), "detail_level": "compact"},
                tool="n8n_list_nodes",
                data_from="items",
            )
        )
    except Exception as exc:
        return structured_result(_http_error("n8n_list_nodes", exc))


@mcp.tool(structured_output=False)
async def n8n_list_node_files(node_name: Optional[str] = None) -> CallToolResult:
    """List files in a node folder on GitHub (read/list).

    **Use when:**
        Finding ``.ts`` sources before reading them.

    **Args:**
        node_name: Package folder name under n8n's nodes tree.

    **Returns:**
        ``{\"items\": [{name, path, type, download_url}, ...], \"detail_level\"}``; empty ``items`` if ``node_name`` missing.

    **Notes:**
        Pair returned paths with ``n8n_get_node_source``.

    **Errors:**
        ``{"error", "details"}`` on GitHub failure.

    **Example:**
        ``n8n_list_node_files(node_name=\"Slack\")``
    """
    if not node_name or not str(node_name).strip():
        return structured_result(
            with_response_meta({"items": [], "detail_level": "compact"}, tool="n8n_list_node_files", data_from="items")
        )
    url = f"{GITHUB_API}/repos/{N8N_NODES_REPO}/contents/{N8N_NODES_PATH}/{node_name}?ref={N8N_NODES_BRANCH}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=github_headers())
        response.raise_for_status()
        items = response.json()
        rows = [
            {
                "name": item["name"],
                "path": item["path"],
                "type": item["type"],
                "download_url": str(item.get("download_url") or ""),
            }
            for item in items
        ]
        return structured_result(
            with_response_meta(
                {"items": rows, "detail_level": "compact"}, tool="n8n_list_node_files", data_from="items"
            )
        )


@mcp.tool(structured_output=False)
async def n8n_get_node_source(file_path: Optional[str] = None) -> CallToolResult:
    """Fetch raw node TypeScript source from GitHub (read/detail).

    **Use when:**
        Inspecting ``properties``/defaults for workflow authoring.

    **Args:**
        file_path: Repo-relative path to a node source file (e.g. ``nodes/Slack/Slack.node.ts``).

    **Returns:**
        ``{\"text\", \"path\", \"encoding\", \"detail_level\"}``; empty text when ``file_path`` missing.

    **Notes:**
        Uses GitHub raw URLs; large files can be slow.

    **Errors:**
        ``{"error", "details"}`` on download failure.

    **Example:**
        ``n8n_get_node_source(file_path=\"nodes/Slack/Slack.node.ts\")``
    """
    fp = (file_path or "").strip()
    if not fp:
        return structured_result({"text": "", "path": "", "encoding": "utf-8", "detail_level": "compact"})
    url = f"{GITHUB_RAW}/{N8N_NODES_REPO}/{N8N_NODES_BRANCH}/{fp}"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=github_headers())
        response.raise_for_status()
        return structured_result({"text": response.text, "path": fp, "encoding": "utf-8", "detail_level": "compact"})


@mcp.tool(structured_output=False)
async def n8n_search_nodes(query: Optional[str] = None) -> CallToolResult:
    """Search n8n node sources via GitHub code search (read/search).

    **Use when:**
        You know a keyword (``Slack channel``, ``notion``, …) but not which file defines it.

    **Args:**
        query: GitHub code-search query fragment (combined with fixed repo/path filters).

    **Returns:**
        ``{\"items\": [{name, path, url}, ...], \"detail_level\"}`` or structured error when GitHub denies search.

    **Notes:**
        Set ``GITHUB_TOKEN`` if public code search is rate-limited.

    **Errors:**
        ``{"error", "details", "status_code"?}`` when GitHub returns 401/403 or on HTTP errors.

    **Example:**
        ``n8n_search_nodes(query=\"slack\")``
    """
    if not query or not str(query).strip():
        return structured_result(
            with_response_meta({"items": [], "detail_level": "compact"}, tool="n8n_search_nodes", data_from="items")
        )
    search_url = f"{GITHUB_API}/search/code?q={query}+repo:{N8N_NODES_REPO}+path:{N8N_NODES_PATH}"
    async with httpx.AsyncClient() as client:
        response = await client.get(search_url, headers=github_headers())
        if response.status_code in (401, 403):
            message = (
                "GitHub code search is unauthorized or rate-limited. "
                "Set GITHUB_TOKEN with a token that can access public repos "
                "and retry."
            )
            logger.warning("n8n_search_nodes: %s (status %s)", message, response.status_code)
            return structured_result(
                tool_error(
                    "github_rate_limited",
                    details=message,
                    status_code=response.status_code,
                    cause="permission",
                    retryable=True,
                    suggested_fix="Set GITHUB_TOKEN for higher rate limits and retry.",
                )
            )
        response.raise_for_status()
        data = response.json()
        items = [
            {"name": item["name"], "path": item["path"], "url": item["html_url"]} for item in data.get("items", [])[:20]
        ]
        return structured_result(
            with_response_meta({"items": items, "detail_level": "compact"}, tool="n8n_search_nodes", data_from="items")
        )


@mcp.tool(structured_output=False)
async def n8n_find_node_by_name_or_capability(
    query: Optional[str] = None,
    limit: int = 15,
    use_search_fallback: bool = True,
) -> CallToolResult:
    """Rank n8n node package folders by human query (read/search).

    **Use when:**
        You need likely node folders (e.g. \"slack\", \"http request\") without scanning the full GitHub tree.

    **Args:**
        query: Free-text capability or node name fragment.
        limit: Max rows returned (default 15).
        use_search_fallback: When folder-name scores are weak, run GitHub code search (needs ``GITHUB_TOKEN`` if rate-limited).

    **Returns:**
        ``items`` with ``node_folder``, ``score``, ``match_reason``; ``search_fallback_used`` when code search contributed.

    **Notes:**
        Next step is usually ``n8n_list_node_files`` then ``n8n_get_node_source``.

    **Errors:**
        ``validation_error`` when ``query`` is empty; GitHub errors otherwise.
    """
    raw_q = (query or "").strip()
    if not raw_q:
        return structured_result(
            tool_error(
                "validation_error",
                details="query is required",
                cause="validation",
                retryable=False,
            )
        )
    cap = max(1, min(int(limit) if limit is not None else 15, 50))
    search_fallback_used = False
    try:
        folders = await _github_list_node_folder_names()
    except Exception as exc:
        return structured_result(_http_error("n8n_find_node_by_name_or_capability", exc))

    ranked = _score_node_folder_matches(raw_q, folders)
    items: List[Dict[str, Any]] = [
        {"node_folder": folder, "score": score, "match_reason": reason} for folder, score, reason in ranked[:cap]
    ]
    top_score = items[0]["score"] if items else 0

    if use_search_fallback and top_score < 25:
        search_url = f"{GITHUB_API}/search/code?q={raw_q}+repo:{N8N_NODES_REPO}+path:{N8N_NODES_PATH}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(search_url, headers=github_headers())
                if response.status_code not in (401, 403):
                    response.raise_for_status()
                    data = response.json()
                    seen_folder: Set[str] = {str(x.get("node_folder")) for x in items if x.get("node_folder")}
                    for hit in data.get("items", [])[:20]:
                        path = hit.get("path") if isinstance(hit, dict) else None
                        if not isinstance(path, str):
                            continue
                        folder = _folder_from_nodes_path(path)
                        if not folder or folder in seen_folder:
                            continue
                        seen_folder.add(folder)
                        items.append(
                            {
                                "node_folder": folder,
                                "score": 12,
                                "match_reason": "github_code_search",
                                "path": path,
                            }
                        )
                        search_fallback_used = True
                        if len(items) >= cap:
                            break
        except Exception as exc:
            logger.warning("n8n_find_node_by_name_or_capability search fallback: %s", exc)

    items = items[:cap]
    payload: Dict[str, Any] = {
        "items": items,
        "count": len(items),
        "search_fallback_used": search_fallback_used,
        "detail_level": "compact",
    }
    return structured_result(with_response_meta(payload, tool="n8n_find_node_by_name_or_capability", data_from="items"))


@mcp.tool(structured_output=False)
async def n8n_validate_workflow_definition(
    name: Optional[str] = None,
    nodes: Optional[List[Dict[str, Any]]] = None,
    connections: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> CallToolResult:
    """Dry-run structural validation for workflow JSON before create/update (read-only).

    **Use when:**
        You want to catch missing node fields, duplicate ids/names, bad settings keys, or broken connection wiring locally.

    **Args:**
        name, nodes, connections, settings — same shapes as ``n8n_create_workflow`` / ``n8n_update_workflow`` payloads.

    **Returns:**
        ``valid`` (bool), ``errors`` (blocking), ``warnings`` (non-blocking). No n8n API calls.

    **Notes:**
        Does not validate credential existence or per-node parameter schemas. See resource ``n8n://templates/workflow-starter-minimal``.

    **Errors:**
        Returns ``valid: false`` with ``errors`` instead of throwing for structural problems.
    """
    valid, errors, warnings = _validate_workflow_definition_body(
        name=name, nodes=nodes, connections=connections, settings=settings
    )
    return structured_result(
        with_response_meta(
            {
                "valid": valid,
                "errors": errors,
                "warnings": warnings,
                "detail_level": "compact",
            },
            tool="n8n_validate_workflow_definition",
        )
    )


@mcp.prompt()
def n8n_workflow_assistant(question: str) -> str:
    """Generate a prompt to help with n8n workflow tasks.

    **Args:**
        question: The workflow-related question or task (e.g. 'update Slack channel',
            'create a workflow that sends to Slack', 'change the message text').
    """
    return (
        "You are an n8n workflow expert. Follow these capabilities and limitations.\n\n"
        "CAPABILITIES — Read-only: n8n_get_workflows, n8n_find_workflow_by_name, n8n_get_workflow (full details), "
        "n8n_get_executions (history and details), n8n_get_credentials (names and types only; "
        "secrets never exposed), n8n_list_nodes, n8n_list_node_files, n8n_get_node_source, n8n_search_nodes, "
        "n8n_find_node_by_name_or_capability, n8n_validate_workflow_definition. Resource: n8n://templates/workflow-starter-minimal.\n"
        "Write (use with care): n8n_create_workflow, n8n_update_workflow (GET + merge + PUT full workflow), "
        "n8n_create_credential, n8n_stop_execution, n8n_retry_execution.\n\n"
        "LIMITATIONS — This server cannot: execute or trigger a workflow (no tool to start a run; "
        "use Webhook trigger + POST to https://<n8n-instance>/webhook/<path>), delete workflows or "
        "credentials, or access credential secrets.\n\n"
        "API BEHAVIOR — n8n PUT expects a complete workflow; n8n_update_workflow supplies it from GET plus your "
        "fields. Nodes: merged by id by default (single-node edits OK); nodes_replace=true replaces the whole "
        "nodes list. Omit connections/settings to keep them; passing them replaces those objects entirely. "
        "Schedule Trigger nodes must run no more often than every 2 hours; minute-based schedules are rejected. "
        "Every workflow created or updated through this server must retain the `mcp` tag.\n\n"
        "CHECKLIST: 0) Help: mcp_proxy://help/by-task/n8n-create-workflow. 1) n8n_get_credentials(fetch_all=true) "
        "before creating credentials; reuse by name+type. 2) n8n_get_credential_schema before creating. "
        "3) n8n_validate_workflow_definition before writes when unsure. 4) For updates, GET first; merge nodes by id "
        "unless nodes_replace=true; omit connections/settings to preserve. 5) n8n_get_node_source for parameters. "
        "6) After create/update, follow mcp_next_steps in the tool result.\n\n"
        f"User task: {question}\n\n"
        "Start by fetching the workflow if updating, or listing nodes if creating. "
        "When updating nodes without nodes_replace, include each node's id; other nodes stay unchanged."
    )


@mcp.prompt()
def n8n_minimal_workflow_editor(task: str) -> str:
    """Prompt template for lean n8n authoring: minimal graphs, few questions, prefer update over create.

    **Args:**
        task: What to build or change (workflow name, behavior, constraints).

    **Inspect text:** ``resources/read`` on ``n8n://prompts/n8n-minimal-workflow-editor`` (full template) or
    ``n8n://prompts/n8n-minimal-workflow-editor/static`` (rules only).
    """
    return n8n_minimal_workflow_editor_full_text(task)


# ---------------------------------------------------------------------------
# Machine-readable tool metadata
# ---------------------------------------------------------------------------

TOOL_METADATA: dict = {
    "n8n_get_workflows": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": False,
    },
    "n8n_get_workflow": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "workflow_id",
    },
    "n8n_find_workflow_by_name": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "resolver": True,
        "primary_param": "query",
    },
    "n8n_create_workflow": {
        "read_only": False,
        "mutation": True,
        "idempotent": False,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "n8n_delete_workflow": {
        "read_only": False,
        "mutation": True,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "workflow_id",
    },
    "n8n_update_workflow": {
        "read_only": False,
        "mutation": True,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "workflow_id",
    },
    "n8n_get_credentials": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": False,
    },
    "n8n_get_credential_schema": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "n8n_create_credential": {
        "read_only": False,
        "mutation": True,
        "idempotent": False,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "n8n_get_executions": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": False,
    },
    "n8n_stop_execution": {
        "read_only": False,
        "mutation": True,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "execution_id",
    },
    "n8n_retry_execution": {
        "read_only": False,
        "mutation": True,
        "idempotent": False,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "execution_id",
    },
    "n8n_list_nodes": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "n8n_list_node_files": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "n8n_get_node_source": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "n8n_search_nodes": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "primary_param": "query",
    },
    "n8n_find_node_by_name_or_capability": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "resolver": True,
        "primary_param": "query",
    },
    "n8n_validate_workflow_definition": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
}
