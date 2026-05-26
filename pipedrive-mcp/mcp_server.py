"""MCP tools for Pipedrive (read-only): list, get, search — activities, deals, leads, persons, pipelines, stages."""

from __future__ import annotations

import asyncio
import copy
from typing import Any, Callable, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.detail_level import parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import with_response_meta
from mcp_platform.transport import structured_result
from pipedrive_client import get_json, get_v2_json

_MAX_LIMIT = 500
_MAX_SCAN_PAGES = 20
_SCAN_PAGE_SIZE = 500

_DROP_KEYS = frozenset(
    {
        "note",
        "notes",
        "public_description",
        "formatted_note",
        "description",
        "formatted_description",
    }
)

mcp = FastMCP(
    "pipedrive",
    instructions=(
        "Pipedrive CRM read-only MCP. Success payloads include meta.schema_version, meta.tool, and when applicable "
        "meta.pagination (request page params). Upstream pagination is not duplicated under additional_data when "
        "meta.pagination is present. detail_level applies to list/search rows. Errors use {error, details, cause?, ...}. "
        "Resolvers: pipedrive_find_deal_by_title_or_exact_name (count/data), pipedrive_get_deal_summary. "
        "Behind mcp-proxy id 'pipedrive' tools stay pipedrive_*."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        return 1
    return min(limit, _MAX_LIMIT)


def _pipedrive_upstream_error(body: Any) -> Optional[dict[str, Any]]:
    if not isinstance(body, dict):
        return None
    if body.get("success") is False:
        msg: Any = body.get("error")
        if msg is None and isinstance(body.get("error_info"), dict):
            msg = body["error_info"].get("message")
        details = str(msg or "Pipedrive request failed")
        extra: dict[str, Any] = {}
        if body.get("status_code") is not None:
            extra["status_code"] = body["status_code"]
        return tool_error(
            "upstream_failed",
            details=details,
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check Pipedrive API status.",
            **extra,
        )
    return None


def _lead_id_path_error(lead_id: str) -> Optional[dict[str, Any]]:
    lid = (lead_id or "").strip()
    if not lid:
        return tool_error("validation_error", details="lead_id is required", cause="validation", retryable=False)
    if any(x in lid for x in ("/", "\\", "..")):
        return tool_error("validation_error", details="invalid lead_id", cause="validation", retryable=False)
    return None


def _search_term_error(term: str, exact_match: Optional[bool]) -> Optional[dict[str, Any]]:
    t = (term or "").strip()
    if not t:
        return tool_error("validation_error", details="term is required", cause="validation", retryable=False)
    min_len = 1 if exact_match else 2
    if len(t) < min_len:
        return tool_error(
            "validation_error",
            details=f"term must be at least {min_len} character(s) (API minimum)",
            cause="validation",
            retryable=False,
        )
    return None


def _next_cursor(body: Dict[str, Any]) -> Optional[str]:
    ad = body.get("additional_data")
    if isinstance(ad, dict):
        for key in ("next_cursor", "cursor"):
            v = ad.get(key)
            if isinstance(v, str) and v:
                return v
        pag = ad.get("pagination")
        if isinstance(pag, dict):
            for key in ("next_cursor", "cursor"):
                v = pag.get(key)
                if isinstance(v, str) and v:
                    return v
    return None


def _v2_body_error(body: Any) -> Optional[Dict[str, Any]]:
    if isinstance(body, dict) and body.get("success") is False:
        return body
    return None


def _mask_item(item: Dict[str, Any], dl: str) -> Dict[str, Any]:
    if dl == "full":
        return dict(item)
    out: Dict[str, Any] = {}
    for k, v in item.items():
        if dl == "compact" and k in _DROP_KEYS:
            continue
        if dl == "summary" and k in _DROP_KEYS and isinstance(v, str):
            out[k] = v[:800] + ("…" if len(v) > 800 else "")
        elif dl == "compact" and isinstance(v, str) and len(v) > 700:
            out[k] = v[:300] + "…"
        else:
            out[k] = v
    return out


def _with_detail_level(body: dict[str, Any], dl: str) -> dict[str, Any]:
    b = copy.deepcopy(body) if dl != "full" else dict(body)
    b["detail_level"] = dl
    if dl == "full":
        return b
    data = b.get("data")
    if isinstance(data, list):
        b["data"] = [_mask_item(x, dl) if isinstance(x, dict) else x for x in data]
    elif isinstance(data, dict):
        b["data"] = _mask_item(data, dl)
    return b


def _finish(
    body: Any, dl: str, tool: Optional[str] = None, pagination: Optional[Dict[str, Any]] = None
) -> CallToolResult:
    if err := _pipedrive_upstream_error(body):
        return structured_result(err)
    if not isinstance(body, dict):
        payload: Any = {"value": body, "detail_level": dl}
        if tool:
            payload = with_response_meta(payload, tool=tool, pagination=pagination)
        return structured_result(payload)
    if "data" in body or body.get("success") is True or "additional_data" in body:
        payload = _with_detail_level(body, dl)
        if pagination is not None:
            ad = payload.get("additional_data")
            if isinstance(ad, dict) and "pagination" in ad:
                ad = {k: v for k, v in ad.items() if k != "pagination"}
                if ad:
                    payload["additional_data"] = ad
                else:
                    payload.pop("additional_data", None)
        if tool:
            payload = with_response_meta(payload, tool=tool, pagination=pagination)
        return structured_result(payload)
    payload = {**body, "detail_level": dl}
    if tool:
        payload = with_response_meta(payload, tool=tool, pagination=pagination)
    return structured_result(payload)


def _scan_v2_list(
    path: str,
    list_params: Dict[str, Any],
    max_matches: int,
    predicate: Callable[[Dict[str, Any]], bool],
) -> Dict[str, Any]:
    matches: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    scanned = 0
    pages_fetched = 0
    while len(matches) < max_matches and pages_fetched < _MAX_SCAN_PAGES:
        pages_fetched += 1
        params_t = {**list_params, "limit": min(_SCAN_PAGE_SIZE, _MAX_LIMIT)}
        if cursor:
            params_t["cursor"] = cursor
        body = get_v2_json(path, params_t)
        if err := _v2_body_error(body):
            return err
        if not isinstance(body, dict):
            return {"success": False, "error": "unexpected Pipedrive response shape"}
        data = body.get("data")
        if not isinstance(data, list):
            break
        for item in data:
            scanned += 1
            if isinstance(item, dict) and predicate(item):
                matches.append(item)
                if len(matches) >= max_matches:
                    break
        if len(matches) >= max_matches:
            break
        cursor = _next_cursor(body)
        if not cursor:
            break
    return {
        "data": matches,
        "additional_data": {
            "client_side_filter": True,
            "resource": path,
            "items_scanned": scanned,
            "pages_fetched": pages_fetched,
        },
        "success": True,
    }


@mcp.tool(structured_output=False)
async def pipedrive_list_activities(
    start: int = 0,
    limit: int = 50,
    user_id: Optional[int] = None,
    deal_id: Optional[int] = None,
    done: Optional[int] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """List activities (read/list).

    Use when:
        You need paginated activities with filters.

    Args:
        start: Offset.
        limit: Page size (capped).
        user_id, deal_id, done: Optional filters (done: 0=open, 1=done).
        detail_level: compact (default) | summary | full for each activity in data.

    Returns:
        v1 activities JSON with data[] plus detail_level.

    Notes:
        Prefer pipedrive_search_activities for substring match in subject/note.

    Errors:
        upstream_failed when the API reports failure.

    Example:
        pipedrive_list_activities(start=0, limit=25, detail_level="compact")
    """
    dl = parse_detail_level(detail_level)
    params: Dict[str, Any] = {"start": start, "limit": _clamp_limit(limit)}
    if user_id is not None:
        params["user_id"] = user_id
    if deal_id is not None:
        params["deal_id"] = deal_id
    if done is not None:
        params["done"] = done
    body = await asyncio.to_thread(get_json, "activities", params)
    return _finish(body, dl, tool="pipedrive_list_activities", pagination={"start": start, "limit": limit})


@mcp.tool(structured_output=False)
async def pipedrive_get_activity(
    activity_id: int,
    include_fields: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Get one activity (read/detail).

    Use when:
        You have a numeric activity id.

    Args:
        activity_id: Activity id.
        include_fields: Optional v2 includes.
        detail_level: compact | summary | full (default summary).

    Returns:
        GET /activities/{id} JSON; data masked unless full.

    Notes:
        Uses API v2 single-resource shape.

    Errors:
        upstream_failed on API/token errors.

    Example:
        pipedrive_get_activity(1, detail_level="compact")
    """
    dl = parse_detail_level(detail_level, default="summary")
    params: Dict[str, Any] = {}
    if include_fields:
        params["include_fields"] = include_fields
    body = await asyncio.to_thread(get_v2_json, f"activities/{activity_id}", params)
    return _finish(body, dl)


@mcp.tool(structured_output=False)
async def pipedrive_search_activities(
    term: str,
    limit: int = 100,
    user_id: Optional[int] = None,
    deal_id: Optional[int] = None,
    done: Optional[int] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Search activities by text (read/search).

    Use when:
        You need case-insensitive substring match in subject, note, or public_description.

    Args:
        term: Needle (min 2 chars).
        limit: Max matches.
        user_id, deal_id, done: Optional filters during scan.
        detail_level: compact (default) | summary | full.

    Returns:
        Client-side scan result: data[], additional_data with scan stats, detail_level.

    Notes:
        No native search endpoint; may scan several list pages.

    Errors:
        validation for short term; upstream_failed if list pages error.

    Example:
        pipedrive_search_activities("call back", limit=20)
    """
    dl = parse_detail_level(detail_level)
    if err := _search_term_error(term, exact_match=False):
        return structured_result(err)
    needle = term.strip().casefold()

    def pred(item: Dict[str, Any]) -> bool:
        parts: List[str] = []
        for key in ("subject", "public_description", "note"):
            v = item.get(key)
            if v is not None and v != "":
                parts.append(str(v))
        return needle in " ".join(parts).casefold()

    list_params: Dict[str, Any] = {}
    if user_id is not None:
        list_params["owner_id"] = user_id
    if deal_id is not None:
        list_params["deal_id"] = deal_id
    if done is not None:
        list_params["done"] = done == 1 if isinstance(done, int) else bool(done)

    body = await asyncio.to_thread(_scan_v2_list, "activities", list_params, _clamp_limit(limit), pred)
    return _finish(body, dl, tool="pipedrive_search_activities")


@mcp.tool(structured_output=False)
async def pipedrive_list_deals(
    start: int = 0,
    limit: int = 50,
    status: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """List deals (read/list).

    Use when:
        Browsing deals with optional status filter.

    Args:
        start, limit: Offset pagination.
        status: e.g. open, won, lost.
        detail_level: compact (default) | summary | full for data items.

    Returns:
        v1 deals list JSON plus detail_level.

    Notes:
        Prefer pipedrive_search_deals for indexed search.

    Errors:
        upstream_failed on API failure.

    Example:
        pipedrive_list_deals(limit=30, status="open")
    """
    dl = parse_detail_level(detail_level)
    params: Dict[str, Any] = {"start": start, "limit": _clamp_limit(limit)}
    if status:
        params["status"] = status
    body = await asyncio.to_thread(get_json, "deals", params)
    return _finish(body, dl, tool="pipedrive_list_deals", pagination={"start": start, "limit": limit})


@mcp.tool(structured_output=False)
async def pipedrive_get_deal(
    deal_id: int,
    include_fields: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Get one deal (read/detail).

    Use when:
        You have deal_id and need the v2 document.

    Args:
        deal_id: Numeric id.
        include_fields: Optional includes.
        detail_level: compact | summary | full (default summary).

    Returns:
        GET /deals/{id} JSON with masked data unless full.

    Notes:
        Passthrough fields depend on API version.

    Errors:
        upstream_failed on errors.

    Example:
        pipedrive_get_deal(10, detail_level="full")
    """
    dl = parse_detail_level(detail_level, default="summary")
    params: Dict[str, Any] = {}
    if include_fields:
        params["include_fields"] = include_fields
    body = await asyncio.to_thread(get_v2_json, f"deals/{deal_id}", params)
    return _finish(body, dl)


@mcp.tool(structured_output=False)
async def pipedrive_search_deals(
    term: str,
    fields: Optional[str] = None,
    exact_match: Optional[bool] = None,
    person_id: Optional[int] = None,
    organization_id: Optional[int] = None,
    status: Optional[str] = None,
    include_fields: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Search deals by text (read/search).

    Use when:
        You need v2 deals/search instead of paging all deals.

    Args:
        term: Search term (required).
        fields, exact_match, person_id, organization_id, status, include_fields: API filters.
        limit: Max hits.
        cursor: Pagination cursor.
        detail_level: compact (default) | summary | full — masks nested hit payloads when possible.

    Returns:
        Passthrough v2 search JSON plus detail_level on the top-level dict.

    Notes:
        Empty hits are normal.

    Errors:
        validation or upstream_failed.

    Example:
        pipedrive_search_deals("Acme", status="open", limit=50)
    """
    dl = parse_detail_level(detail_level)
    if err := _search_term_error(term, exact_match):
        return structured_result(err)
    params: Dict[str, Any] = {
        "term": term.strip(),
        "limit": _clamp_limit(limit),
    }
    if fields:
        params["fields"] = fields
    if exact_match is not None:
        params["exact_match"] = str(exact_match).lower()
    if person_id is not None:
        params["person_id"] = person_id
    if organization_id is not None:
        params["organization_id"] = organization_id
    if status:
        params["status"] = status
    if include_fields:
        params["include_fields"] = include_fields
    if cursor:
        params["cursor"] = cursor
    body = await asyncio.to_thread(get_v2_json, "deals/search", params)
    return _finish(body, dl, tool="pipedrive_search_deals", pagination={"limit": limit, "cursor": cursor})


def _deals_search_hits(body: Any) -> List[Dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item = it.get("item")
        if isinstance(item, dict):
            out.append(item)
        else:
            out.append(it)
    return out


def _deal_resolver_row(hit: Dict[str, Any]) -> Dict[str, Any]:
    person_id: Optional[int] = None
    person_name: Optional[str] = None
    pr = hit.get("person_id")
    if isinstance(pr, dict):
        v = pr.get("id") if pr.get("id") is not None else pr.get("value")
        if v is not None:
            try:
                person_id = int(v)
            except (TypeError, ValueError):
                person_id = None
        person_name = pr.get("name") if isinstance(pr.get("name"), str) else None
    elif isinstance(pr, int):
        person_id = pr

    organization_id: Optional[int] = None
    organization_name: Optional[str] = None
    for key in ("org_id", "organization_id"):
        o = hit.get(key)
        if isinstance(o, dict):
            v = o.get("id") or o.get("value")
            if v is not None:
                try:
                    organization_id = int(v)
                except (TypeError, ValueError):
                    organization_id = None
            on = o.get("name")
            organization_name = str(on) if on is not None else None
            break
        if isinstance(o, int):
            organization_id = o
            break

    st = hit.get("status")
    pipeline_id = hit.get("pipeline_id")
    stage_id = hit.get("stage_id")
    pid: Optional[int] = None
    sid: Optional[int] = None
    try:
        pid = int(pipeline_id) if pipeline_id is not None else None
    except (TypeError, ValueError):
        pid = None
    try:
        sid = int(stage_id) if stage_id is not None else None
    except (TypeError, ValueError):
        sid = None

    return {
        "id": int(hit["id"]) if hit.get("id") is not None else 0,
        "title": str(hit.get("title") or ""),
        "status": str(st) if st is not None else None,
        "pipeline_id": pid,
        "stage_id": sid,
        "person_id": person_id,
        "person_name": person_name,
        "organization_id": organization_id,
        "organization_name": organization_name,
    }


@mcp.tool(structured_output=False)
async def pipedrive_find_deal_by_title_or_exact_name(
    query: str,
    person_id: Optional[int] = None,
    organization_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 10,
    cursor: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Resolve deals from a human title/name without interpreting raw v2 search payloads (read/search).

    IMPORTANT: The search parameter is named ``query`` — do not pass ``title`` or ``name``.

    Use when:
        You have a natural-language deal title. Prefer this over ``pipedrive_search_deals`` when you need ranked
        exact-then-partial title matches only.

    Args:
        query: Deal title text (required).
        person_id, organization_id, status: Optional Pipedrive search scope.
        limit: Max deals after ranking (default 10).
        cursor: Optional pagination cursor for the underlying v2 search.
        detail_level: Echoed on the payload (default compact).

    Returns:
        ``{"count", "data", "detail_level": "compact"}``. Multiple rows mean ambiguity — never auto-select.

    Notes:
        Exact case-insensitive title matches are returned before substring matches. Empty ``data`` is normal.

    Errors:
        ``{"error": "validation_error", "details": ...}`` when ``query`` is too short for the API.
        ``{"error": "upstream_failed", "details": ...}`` on API errors.

    Example:
        pipedrive_find_deal_by_title_or_exact_name(query="Invoice #4", limit=5)
    """
    dl = parse_detail_level(detail_level, default="compact")
    if err := _search_term_error(query, False):
        return structured_result(err)
    qstrip = (query or "").strip()
    qn = qstrip.casefold()
    fetch_limit = min(_MAX_LIMIT, max(limit * 3, 25))
    params: Dict[str, Any] = {
        "term": qstrip,
        "fields": "title",
        "limit": _clamp_limit(fetch_limit),
    }
    if person_id is not None:
        params["person_id"] = person_id
    if organization_id is not None:
        params["organization_id"] = organization_id
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor
    body = await asyncio.to_thread(get_v2_json, "deals/search", params)
    if err := _pipedrive_upstream_error(body):
        return structured_result(err)
    hits = _deals_search_hits(body)
    if not hits:
        return structured_result(
            with_response_meta(
                {"count": 0, "data": [], "detail_level": dl}, tool="pipedrive_find_deal_by_title_or_exact_name"
            )
        )
    exact = [h for h in hits if isinstance(h, dict) and str(h.get("title") or "").casefold() == qn]
    if exact:
        ranked = exact
    else:
        ranked = [h for h in hits if isinstance(h, dict) and qn in str(h.get("title") or "").casefold()]
    rows = [_deal_resolver_row(dict(h)) for h in ranked[: max(1, min(limit, 50))]]
    return structured_result(
        with_response_meta(
            {"count": len(rows), "data": rows, "detail_level": dl},
            tool="pipedrive_find_deal_by_title_or_exact_name",
        )
    )


@mcp.tool(structured_output=False)
async def pipedrive_get_deal_summary(
    deal_id: int,
) -> CallToolResult:
    """Fetch one compact CRM deal summary by exact deal id (read/detail).

    Use when:
        You have ``deal_id`` from search/list/resolver tools and need stable identity fields only.

    Args:
        deal_id: Pipedrive deal id.

    Returns:
        Resolver-shaped row with ``detail_level: "summary"`` (no heavy note blobs).

    Notes:
        Uses GET v2 ``/deals/{id}``. Prefer after ``pipedrive_find_deal_by_title_or_exact_name``.

    Errors:
        ``{"error": "upstream_failed", "details": ...}`` on API failure or missing deal.

    Example:
        pipedrive_get_deal_summary(deal_id=123)
    """
    body = await asyncio.to_thread(get_v2_json, f"deals/{int(deal_id)}", {})
    if err := _pipedrive_upstream_error(body):
        return structured_result(err)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return structured_result(
            tool_error("not_found", details=f"No deal data for id={deal_id}", cause="not_found", retryable=False)
        )
    row = _deal_resolver_row(data)
    return structured_result({**row, "detail_level": "summary"})


@mcp.tool(structured_output=False)
async def pipedrive_list_leads(
    start: int = 0,
    limit: int = 50,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """List leads (read/list).

    Use when:
        Paginating the Leads Inbox.

    Args:
        start, limit: Offset pagination.
        detail_level: compact (default) | summary | full.

    Returns:
        v1 leads JSON plus detail_level.

    Notes:
        Prefer pipedrive_search_leads for search semantics.

    Errors:
        upstream_failed.

    Example:
        pipedrive_list_leads(limit=40)
    """
    dl = parse_detail_level(detail_level)
    params = {"start": start, "limit": _clamp_limit(limit)}
    body = await asyncio.to_thread(get_json, "leads", params)
    return _finish(body, dl, tool="pipedrive_list_leads", pagination={"start": start, "limit": limit})


@mcp.tool(structured_output=False)
async def pipedrive_get_lead(
    lead_id: str,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Get one lead (read/detail).

    Use when:
        You have the UUID lead id.

    Args:
        lead_id: UUID for v1 /leads/{id}.
        detail_level: compact | summary | full (default summary) for embedded payload.

    Returns:
        v1 lead JSON; data masked when single-object response.

    Notes:
        Validation uses path safety checks.

    Errors:
        validation for bad id; upstream_failed.

    Example:
        pipedrive_get_lead("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    """
    dl = parse_detail_level(detail_level, default="summary")
    if err := _lead_id_path_error(lead_id):
        return structured_result(err)
    body = await asyncio.to_thread(get_json, f"leads/{lead_id.strip()}", {})
    return _finish(body, dl)


@mcp.tool(structured_output=False)
async def pipedrive_search_leads(
    term: str,
    fields: Optional[str] = None,
    exact_match: Optional[bool] = None,
    person_id: Optional[int] = None,
    organization_id: Optional[int] = None,
    include_fields: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Search leads (read/search).

    Use when:
        Finding Leads Inbox rows by term.

    Args:
        term: Search string.
        fields, exact_match, filters, include_fields, limit, cursor: API parameters.
        detail_level: compact (default) | summary | full.

    Returns:
        v2 leads/search JSON plus detail_level.

    Notes:
        Empty data is normal.

    Errors:
        validation or upstream_failed.

    Example:
        pipedrive_search_leads("hotel", limit=20)
    """
    dl = parse_detail_level(detail_level)
    if err := _search_term_error(term, exact_match):
        return structured_result(err)
    params: Dict[str, Any] = {"term": term.strip(), "limit": _clamp_limit(limit)}
    if fields:
        params["fields"] = fields
    if exact_match is not None:
        params["exact_match"] = str(exact_match).lower()
    if person_id is not None:
        params["person_id"] = person_id
    if organization_id is not None:
        params["organization_id"] = organization_id
    if include_fields:
        params["include_fields"] = include_fields
    if cursor:
        params["cursor"] = cursor
    body = await asyncio.to_thread(get_v2_json, "leads/search", params)
    return _finish(body, dl, tool="pipedrive_search_leads", pagination={"limit": limit, "cursor": cursor})


@mcp.tool(structured_output=False)
async def pipedrive_list_persons(
    start: int = 0,
    limit: int = 50,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """List persons (read/list).

    Use when:
        Paginating contacts without a search term.

    Args:
        start, limit: Pagination.
        detail_level: compact (default) | summary | full.

    Returns:
        v1 persons JSON plus detail_level.

    Notes:
        Prefer pipedrive_search_persons when searching.

    Errors:
        upstream_failed.

    Example:
        pipedrive_list_persons(limit=100)
    """
    dl = parse_detail_level(detail_level)
    params = {"start": start, "limit": _clamp_limit(limit)}
    body = await asyncio.to_thread(get_json, "persons", params)
    return _finish(body, dl, tool="pipedrive_list_persons", pagination={"start": start, "limit": limit})


@mcp.tool(structured_output=False)
async def pipedrive_get_person(
    person_id: int,
    include_fields: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Get one person (read/detail).

    Use when:
        You have person_id.

    Args:
        person_id: Numeric id.
        include_fields: Optional includes.
        detail_level: compact | summary | full (default summary).

    Returns:
        v2 person JSON with masked data unless full.

    Errors:
        upstream_failed.

    Example:
        pipedrive_get_person(5, include_fields="picture", detail_level="compact")
    """
    dl = parse_detail_level(detail_level, default="summary")
    params: Dict[str, Any] = {}
    if include_fields:
        params["include_fields"] = include_fields
    body = await asyncio.to_thread(get_v2_json, f"persons/{person_id}", params)
    return _finish(body, dl)


@mcp.tool(structured_output=False)
async def pipedrive_search_persons(
    term: str,
    fields: Optional[str] = None,
    exact_match: Optional[bool] = None,
    organization_id: Optional[int] = None,
    include_fields: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Search persons (read/search).

    Use when:
        Finding contacts by name/email term.

    Args:
        term, fields, exact_match, organization_id, include_fields, limit, cursor: API args.
        detail_level: compact (default) | summary | full.

    Returns:
        v2 persons/search JSON plus detail_level.

    Errors:
        validation or upstream_failed.

    Example:
        pipedrive_search_persons("jane@example.com", exact_match=True)
    """
    dl = parse_detail_level(detail_level)
    if err := _search_term_error(term, exact_match):
        return structured_result(err)
    params: Dict[str, Any] = {"term": term.strip(), "limit": _clamp_limit(limit)}
    if fields:
        params["fields"] = fields
    if exact_match is not None:
        params["exact_match"] = str(exact_match).lower()
    if organization_id is not None:
        params["organization_id"] = organization_id
    if include_fields:
        params["include_fields"] = include_fields
    if cursor:
        params["cursor"] = cursor
    body = await asyncio.to_thread(get_v2_json, "persons/search", params)
    return _finish(body, dl, tool="pipedrive_search_persons", pagination={"limit": limit, "cursor": cursor})


@mcp.tool(structured_output=False)
async def pipedrive_list_pipelines(
    start: int = 0,
    limit: int = 50,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """List pipelines (read/list).

    Use when:
        Listing pipeline definitions.

    Args:
        start, limit: Pagination.
        detail_level: compact (default) | summary | full.

    Returns:
        v1 pipelines JSON plus detail_level.

    Notes:
        Prefer pipedrive_search_pipelines for name substring.

    Errors:
        upstream_failed.

    Example:
        pipedrive_list_pipelines(limit=20)
    """
    dl = parse_detail_level(detail_level)
    params = {"start": start, "limit": _clamp_limit(limit)}
    body = await asyncio.to_thread(get_json, "pipelines", params)
    return _finish(body, dl, tool="pipedrive_list_pipelines", pagination={"start": start, "limit": limit})


@mcp.tool(structured_output=False)
async def pipedrive_get_pipeline(
    pipeline_id: int,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Get one pipeline (read/detail).

    Use when:
        You have pipeline_id.

    Args:
        pipeline_id: Numeric id.
        detail_level: compact | summary | full (default summary).

    Returns:
        v2 pipeline JSON plus detail_level.

    Errors:
        upstream_failed.

    Example:
        pipedrive_get_pipeline(1, detail_level="full")
    """
    dl = parse_detail_level(detail_level, default="summary")
    body = await asyncio.to_thread(get_v2_json, f"pipelines/{pipeline_id}", {})
    return _finish(body, dl)


@mcp.tool(structured_output=False)
async def pipedrive_search_pipelines(
    term: str,
    limit: int = 100,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Search pipelines by name (read/search).

    Use when:
        Matching pipeline name substring.

    Args:
        term: Substring needle.
        limit: Max matches.
        detail_level: compact (default) | summary | full.

    Returns:
        Client-side scan: data[], additional_data, detail_level.

    Notes:
        Scans v2 list pages.

    Errors:
        validation or upstream_failed.

    Example:
        pipedrive_search_pipelines("Sales", limit=10)
    """
    dl = parse_detail_level(detail_level)
    if err := _search_term_error(term, exact_match=False):
        return structured_result(err)
    needle = term.strip().casefold()

    def pred(item: Dict[str, Any]) -> bool:
        name = item.get("name")
        return isinstance(name, str) and needle in name.casefold()

    body = await asyncio.to_thread(_scan_v2_list, "pipelines", {}, _clamp_limit(limit), pred)
    return _finish(body, dl, tool="pipedrive_search_pipelines")


@mcp.tool(structured_output=False)
async def pipedrive_list_stages(
    start: int = 0,
    limit: int = 50,
    pipeline_id: Optional[int] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """List stages (read/list).

    Use when:
        Listing deal stages.

    Args:
        start, limit: Pagination.
        pipeline_id: Optional filter.
        detail_level: compact (default) | summary | full.

    Returns:
        v1 stages JSON plus detail_level.

    Notes:
        Prefer pipedrive_search_stages for name match.

    Errors:
        upstream_failed.

    Example:
        pipedrive_list_stages(pipeline_id=3)
    """
    dl = parse_detail_level(detail_level)
    params: Dict[str, Any] = {"start": start, "limit": _clamp_limit(limit)}
    if pipeline_id is not None:
        params["pipeline_id"] = pipeline_id
    body = await asyncio.to_thread(get_json, "stages", params)
    return _finish(body, dl, tool="pipedrive_list_stages", pagination={"start": start, "limit": limit})


@mcp.tool(structured_output=False)
async def pipedrive_get_stage(
    stage_id: int,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Get one stage (read/detail).

    Use when:
        You have stage_id.

    Args:
        stage_id: Numeric id.
        detail_level: compact | summary | full (default summary).

    Returns:
        v2 stage JSON plus detail_level.

    Errors:
        upstream_failed.

    Example:
        pipedrive_get_stage(2)
    """
    dl = parse_detail_level(detail_level, default="summary")
    body = await asyncio.to_thread(get_v2_json, f"stages/{stage_id}", {})
    return _finish(body, dl)


@mcp.tool(structured_output=False)
async def pipedrive_search_stages(
    term: str,
    limit: int = 100,
    pipeline_id: Optional[int] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Search stages by name (read/search).

    Use when:
        Filtering stages by name fragment.

    Args:
        term: Substring.
        limit: Max matches.
        pipeline_id: Optional pipeline scope for scan.
        detail_level: compact (default) | summary | full.

    Returns:
        Client-side scan result plus detail_level.

    Errors:
        validation or upstream_failed.

    Example:
        pipedrive_search_stages("Qualified", pipeline_id=1)
    """
    dl = parse_detail_level(detail_level)
    if err := _search_term_error(term, exact_match=False):
        return structured_result(err)
    needle = term.strip().casefold()

    def pred(item: Dict[str, Any]) -> bool:
        name = item.get("name")
        return isinstance(name, str) and needle in name.casefold()

    list_params: Dict[str, Any] = {}
    if pipeline_id is not None:
        list_params["pipeline_id"] = pipeline_id
    body = await asyncio.to_thread(_scan_v2_list, "stages", list_params, _clamp_limit(limit), pred)
    return _finish(body, dl, tool="pipedrive_search_stages")


# ---------------------------------------------------------------------------
# Machine-readable tool metadata
# ---------------------------------------------------------------------------

TOOL_METADATA: dict = {
    "pipedrive_list_deals": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "pipedrive_get_deal": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "deal_id",
    },
    "pipedrive_search_deals": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": False,
    },
    "pipedrive_find_deal_by_title_or_exact_name": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "resolver": True,
        "primary_param": "query",
    },
    "pipedrive_list_activities": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "pipedrive_get_activity": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "activity_id",
    },
    "pipedrive_search_activities": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "primary_param": "term",
    },
    "pipedrive_list_leads": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "pipedrive_get_lead": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "lead_id",
    },
    "pipedrive_list_persons": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "pipedrive_get_person": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "person_id",
    },
    "pipedrive_search_persons": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": False,
    },
    "pipedrive_list_pipelines": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "pipedrive_get_pipeline": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "pipeline_id",
    },
    "pipedrive_search_pipelines": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "primary_param": "term",
    },
    "pipedrive_list_stages": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "pipedrive_get_stage": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "stage_id",
    },
    "pipedrive_search_stages": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "primary_param": "term",
    },
}
