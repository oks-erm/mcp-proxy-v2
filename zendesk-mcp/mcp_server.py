"""MCP tools for Zendesk (read-only): custom object list/get/search, views, tickets, audits, users, reservations."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.coerce_numeric import coerce_int, coerce_optional_int
from mcp_platform.detail_level import parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result
from zendesk_client import (
    MAX_VIEW_IDS_FOR_COUNT_MANY,
    ZENDESK_NOT_FOUND,
    ZendeskService,
)

logger = logging.getLogger(__name__)

_service: Optional[ZendeskService] = None

_TICKET_COMPACT_KEYS = (
    "id",
    "subject",
    "raw_subject",
    "status",
    "priority",
    "type",
    "requester_id",
    "assignee_id",
    "organization_id",
    "group_id",
    "brand_id",
    "created_at",
    "updated_at",
    "tags",
    "custom_status_id",
)


def _get_service() -> ZendeskService:
    global _service
    if _service is None:
        _service = ZendeskService()
    return _service


def _zendesk_result_or_error(result: Any, *, not_found_detail: str, upstream_detail: str) -> Optional[Dict[str, Any]]:
    """Return a tool_error dict for HTTP 404 / transport failure; None if ``result`` is a normal payload."""
    if result is ZENDESK_NOT_FOUND:
        return tool_error(
            "not_found",
            details=not_found_detail,
            cause="not_found",
            retryable=False,
            suggested_fix="Verify the resource id, custom object key, or that the record exists in Zendesk.",
        )
    if result is None:
        return tool_error(
            "upstream_error",
            details=upstream_detail,
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check credentials, network, and Zendesk API status.",
        )
    return None


def _mask_ticket(t: Any, dl: str) -> Any:
    if not isinstance(t, dict):
        return t
    if dl == "full":
        return dict(t)
    if dl == "compact":
        return {k: t[k] for k in _TICKET_COMPACT_KEYS if k in t}
    out = {k: t[k] for k in _TICKET_COMPACT_KEYS if k in t}
    desc = t.get("description")
    if isinstance(desc, str) and desc:
        out["description"] = desc[:2000] + ("…" if len(desc) > 2000 else "")
    return out


def _mask_custom_object_record(rec: Any, dl: str) -> Any:
    if not isinstance(rec, dict):
        return rec
    if dl == "full":
        return dict(rec)
    out: Dict[str, Any] = {k: rec[k] for k in ("id", "name", "external_id", "created_at", "updated_at") if k in rec}
    fields = rec.get("custom_object_fields")
    if not isinstance(fields, dict):
        return {**out, **{k: rec[k] for k in rec if k not in out and k != "custom_object_fields"}}
    limit = 200 if dl == "compact" else 2000

    def _shorten(v: Any) -> Any:
        if isinstance(v, str) and len(v) > limit:
            return v[:limit] + "…"
        return v

    out["custom_object_fields"] = {kk: _shorten(vv) for kk, vv in fields.items()}
    return out


def _mask_custom_records_in_response(result: Dict[str, Any], dl: str) -> Dict[str, Any]:
    out = deepcopy(result) if dl != "full" else result
    if dl == "full":
        out = dict(result)
    key = "custom_object_records"
    recs = out.get(key)
    if isinstance(recs, list):
        out[key] = [_mask_custom_object_record(r, dl) for r in recs]
    rec = out.get("custom_object_record")
    if isinstance(rec, dict):
        out["custom_object_record"] = _mask_custom_object_record(rec, dl)
    out["detail_level"] = dl
    return out


def _mask_ticket_document(data: Dict[str, Any], dl: str) -> Dict[str, Any]:
    out = dict(data)
    t = out.get("ticket")
    if isinstance(t, dict):
        out["ticket"] = _mask_ticket(t, dl)
    out["detail_level"] = dl
    return out


def _zendesk_ticket_summary_top(t: Dict[str, Any]) -> Dict[str, Any]:
    """Flat summary shape for resolver tools (matches contract; avoids nested ticket wrapper)."""
    tags = t.get("tags")
    tag_list = tags if isinstance(tags, list) else []
    return {
        "id": int(t["id"]) if t.get("id") is not None else 0,
        "subject": t.get("subject") if t.get("subject") is not None else None,
        "status": t.get("status") if t.get("status") is not None else None,
        "priority": t.get("priority") if t.get("priority") is not None else None,
        "requester_id": int(t["requester_id"]) if t.get("requester_id") is not None else None,
        "assignee_id": int(t["assignee_id"]) if t.get("assignee_id") is not None else None,
        "organization_id": int(t["organization_id"]) if t.get("organization_id") is not None else None,
        "created_at": str(t["created_at"]) if t.get("created_at") is not None else None,
        "updated_at": str(t["updated_at"]) if t.get("updated_at") is not None else None,
        "tags": tag_list,
        "detail_level": "summary",
    }


def _ticket_compact_resolver(t: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(t["id"]) if t.get("id") is not None else 0,
        "subject": t.get("subject") if t.get("subject") is not None else None,
        "status": t.get("status") if t.get("status") is not None else None,
        "created_at": str(t["created_at"]) if t.get("created_at") is not None else None,
        "updated_at": str(t["updated_at"]) if t.get("updated_at") is not None else None,
    }


def _extract_ticket_ids_from_reservation_custom_object(data: Any) -> List[int]:
    """Pull probable Zendesk ticket ids linked on reservations_data custom object rows."""
    if not isinstance(data, dict):
        return []
    recs = data.get("custom_object_records")
    if not isinstance(recs, list):
        return []
    ids: List[int] = []
    key_candidates = (
        "zendesk_ticket_id",
        "ticket_id",
        "ticketId",
        "support_ticket_id",
        "zendesk_ticket",
    )
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        fields = rec.get("custom_object_fields")
        if not isinstance(fields, dict):
            fields = {}
        for key in key_candidates:
            raw = fields.get(key)
            if raw is None:
                raw = rec.get(key)
            if raw is None:
                continue
            try:
                tid = int(str(raw).strip())
                if tid not in ids:
                    ids.append(tid)
            except (TypeError, ValueError):
                continue
    return ids


def _mask_audit(a: Any, dl: str) -> Any:
    if not isinstance(a, dict):
        return a
    if dl == "full":
        return dict(a)
    base = {k: a[k] for k in ("id", "ticket_id", "created_at", "author_id") if k in a}
    if dl == "summary":
        ev = a.get("events")
        if isinstance(ev, list):
            base["events"] = ev[:30]
        else:
            base["events"] = ev
    return base


def _mask_user(u: Any, dl: str) -> Any:
    if not isinstance(u, dict):
        return u
    if dl == "full":
        return dict(u)
    if dl == "compact":
        return {k: u[k] for k in ("id", "name", "email") if k in u}
    return {k: u[k] for k in ("id", "name", "email", "role", "active", "created_at", "updated_at") if k in u}


mcp = FastMCP(
    "zendesk",
    instructions=(
        "Zendesk read-only MCP. Tools return structured JSON in structuredContent: success objects echo "
        'detail_level where masking applies; errors use {"error": code, "details": message}. '
        "detail_level: compact (default for list/search), summary, or full (heavy fields, long text). "
        "Tools: custom objects (list/get/search), tickets (get/search/find by reservation), views (list/count/tickets), "
        "audits, users, reservations_data fetch. Behind mcp-proxy id 'zendesk' names may be prefixed zendesk_*."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


@mcp.tool(structured_output=False)
async def zendesk_list_custom_object_records(
    custom_object_key: str,
    page_size: Optional[int] = None,
    page_after: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """List custom object records from Zendesk (read/list).

    Use when:
        You need paginated browsing of records for a known custom object type.

    Args:
        custom_object_key: Custom object key (e.g. reservations_data, listing_data).
        page_size: Optional page[size], capped at 100.
        page_after: Cursor from meta.after_cursor for the next page.
        detail_level: compact (default) | summary | full — controls custom_object_fields string truncation.

    Returns:
        Zendesk list JSON (custom_object_records, meta, links) plus detail_level. Empty list is normal.

    Notes:
        Prefer zendesk_search_custom_object_records when you can express a filter. Cursor pagination via meta.has_more.

    Errors:
        ``not_found`` when Zendesk returns HTTP 404; ``upstream_error`` on transport/other HTTP failures.

    Example:
        zendesk_list_custom_object_records("reservations_data", page_size=50)
    """
    dl = parse_detail_level(detail_level)
    service = _get_service()
    page_size_coerced = coerce_optional_int(page_size, minimum=1, maximum=100)
    params: Dict[str, Any] = {}
    if page_size_coerced is not None:
        params["page[size]"] = page_size_coerced
    if page_after:
        params["page[after]"] = page_after
    result = await asyncio.to_thread(lambda: service.get_custom_object_records(custom_object_key, **params))
    err = _zendesk_result_or_error(
        result,
        not_found_detail="Custom object collection or path returned 404.",
        upstream_detail="Zendesk request failed or returned no data when listing custom object records.",
    )
    if err:
        return structured_result(err)
    return structured_result(
        with_response_meta(
            _mask_custom_records_in_response(result, dl),
            tool="zendesk_list_custom_object_records",
            pagination={"page_size": page_size, "page_after": page_after},
            data_from="custom_object_records",
        )
    )


@mcp.tool(structured_output=False)
async def zendesk_get_custom_object_record(
    custom_object_key: str,
    record_id: str,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Fetch one Zendesk custom object record (read/detail).

    Use when:
        You know custom_object_key and record id.

    Args:
        custom_object_key: Zendesk custom object key.
        record_id: Record id from list/search.
        detail_level: compact | summary | full (default compact).

    Returns:
        Passthrough record JSON; custom_object_fields may be truncated unless full.

    Notes:
        Prefer list/search for discovery.

    Errors:
        ``not_found`` when Zendesk returns HTTP 404; ``upstream_error`` on transport/other failures.

    Example:
        zendesk_get_custom_object_record("listing_data", "123")
    """
    dl = parse_detail_level(detail_level, default="summary")
    service = _get_service()
    result = await asyncio.to_thread(service.get_custom_object_record, custom_object_key, record_id)
    err = _zendesk_result_or_error(
        result,
        not_found_detail="Custom object record not found (HTTP 404).",
        upstream_detail="Zendesk request failed or returned no data when fetching the custom object record.",
    )
    if err:
        return structured_result(err)
    if isinstance(result, dict) and "custom_object_record" in result:
        return structured_result(_mask_custom_records_in_response(result, dl))
    if isinstance(result, dict):
        return structured_result({"custom_object_record": _mask_custom_object_record(result, dl), "detail_level": dl})
    return structured_result({"record": result, "detail_level": dl})


@mcp.tool(structured_output=False)
async def zendesk_search_custom_object_records(
    custom_object_key: str,
    query: str = "",
    page_size: Optional[int] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Search Zendesk custom object records (read/search).

    Use when:
        You need filtered lookup instead of scanning all records.

    Args:
        custom_object_key: e.g. reservations_data.
        query: Zendesk search query for that object type.
        page_size: Optional page[size] capped at 100.
        detail_level: compact (default) | summary | full.

    Returns:
        Search JSON with custom_object_records, meta, links; detail_level echoed.

    Notes:
        Empty custom_object_records is a normal outcome.

    Errors:
        ``not_found`` when Zendesk returns HTTP 404; ``upstream_error`` on transport/other failures.

    Example:
        zendesk_search_custom_object_records("reservations_data", "status:active")
    """
    dl = parse_detail_level(detail_level)
    service = _get_service()
    page_size_coerced = coerce_optional_int(page_size, minimum=1, maximum=100)
    params: Dict[str, Any] = {}
    if page_size_coerced is not None:
        params["page[size]"] = page_size_coerced
    result = await asyncio.to_thread(lambda: service.search_custom_object_records(custom_object_key, query, **params))
    err = _zendesk_result_or_error(
        result,
        not_found_detail="Custom object search path returned 404.",
        upstream_detail="Zendesk request failed or returned no data when searching custom object records.",
    )
    if err:
        return structured_result(err)
    return structured_result(
        with_response_meta(
            _mask_custom_records_in_response(result, dl),
            tool="zendesk_search_custom_object_records",
            pagination={"page_size": page_size},
            data_from="custom_object_records",
        )
    )


@mcp.tool(structured_output=False)
async def zendesk_get_ticket(
    ticket_id: int,
    include: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Load one Zendesk ticket by numeric id as a flattened summary (detail / read).

    Prefer this when you already have ``ticket_id``; use ``zendesk_search_tickets`` or ``zendesk_find_ticket_by_reservation_id`` to obtain ids first.

    Input: positive integer ``ticket_id``; optional ``include`` sideloads.

    Use when:
        You have the numeric Zendesk ticket id and need a compact summary without nested ``{"ticket": ...}`` wrappers.

    Args:
        ticket_id: Zendesk ticket id.
        include: Optional comma-separated sideloads (users, groups, etc.) — summary still flattens core ticket fields only.
        detail_level: Ignored for shape (always contract summary); kept for forward compatibility.

    Returns:
        ``{"data": {ticket fields}, "meta": {tool, schema_version}}``.
        ``data`` is the authoritative singleton.

    Notes:
        Prefer this over ``zendesk_search_tickets`` when the id is already known.

    Errors:
        ``{"error": "validation_error", ...}`` when ``ticket_id`` is zero, negative, or non-numeric — never sent upstream.
        ``{"error": "not_found", "details": ...}`` when the ticket cannot be retrieved.

    Example:
        zendesk_get_ticket(42)
    """
    _ = parse_detail_level(detail_level, default="summary")
    try:
        ticket_id_int = int(ticket_id)
    except (TypeError, ValueError):
        ticket_id_int = None
    if ticket_id_int is None or ticket_id_int <= 0:
        return structured_result(
            tool_error(
                "validation_error",
                details=f"ticket_id must be a positive integer (got {ticket_id!r}).",
                cause="validation",
                retryable=False,
                suggested_fix="Pass a valid positive Zendesk ticket id.",
            )
        )
    service = _get_service()
    data = await asyncio.to_thread(service.get_ticket, ticket_id_int, include)
    if data is ZENDESK_NOT_FOUND:
        return structured_result(
            tool_error(
                "not_found",
                details=f"Ticket {ticket_id_int} not found (HTTP 404).",
                cause="not_found",
                retryable=False,
                suggested_fix="Verify the numeric ticket id exists in Zendesk.",
            ),
        )
    if data is None:
        return structured_result(
            tool_error(
                "upstream_error",
                details="Zendesk request failed when fetching the ticket.",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check credentials and Zendesk API status.",
            ),
        )
    if not isinstance(data, dict):
        return structured_result(
            tool_error(
                "upstream_failed", details="Unexpected Zendesk response shape", cause="upstream_error", retryable=True
            )
        )
    t = data.get("ticket")
    if not isinstance(t, dict):
        return structured_result(
            tool_error("upstream_failed", details="Ticket payload missing", cause="upstream_error", retryable=True)
        )
    summary = _zendesk_ticket_summary_top(t)
    return structured_result(
        with_response_meta(
            {"data": summary},
            tool="zendesk_get_ticket",
        )
    )


@mcp.tool(structured_output=False)
async def zendesk_find_ticket_by_reservation_id(
    reservation_id: str,
    limit: int = 10,
    batch_size: int = 100,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Resolve Zendesk ticket candidates from a PMS / Guesty reservation id without writing search syntax (resolver).

    Prefer this over ad-hoc ``zendesk_search_tickets`` when the user gave a reservation identifier and you need linked tickets.

    Input: ``reservation_id`` string plus optional ``limit`` / ``batch_size``.

    Use when:
        Agents supply a PMS/reservation identifier and need likely Zendesk tickets.

    Args:
        reservation_id: Business reservation id / confirmation token.
        limit: Max tickets to return after deduping.
        batch_size: Page size when falling back to ticket search API.
        detail_level: Echoed as ``compact`` on success (resolver rows are always compact).

    Returns:
        ``{"count", "data", "detail_level": "compact"}`` — never guesses a single ticket; returns candidates.

    Notes:
        Tries ``reservations_data`` custom object search for linked ticket ids first; if none, falls back to
        ``type:ticket`` phrase search including the reservation id.

    Errors:
        ``{"error": "validation_error", "details": ...}`` when ``reservation_id`` is empty.
        ``{"error": "upstream_failed", "details": ...}`` on unexpected failures.

    Example:
        zendesk_find_ticket_by_reservation_id("ABC-123", limit=10)
    """
    dl = parse_detail_level(detail_level, default="compact")
    rid = (reservation_id or "").strip()
    if not rid:
        return structured_result(
            tool_error("validation_error", details="reservation_id is required", cause="validation", retryable=False)
        )
    service = _get_service()
    tickets_out: List[Dict[str, Any]] = []
    try:
        co = await asyncio.to_thread(lambda: service.search_custom_object_records("reservations_data", rid))
        ticket_ids = _extract_ticket_ids_from_reservation_custom_object(co)
        for tid in ticket_ids[:limit]:
            doc = await asyncio.to_thread(service.get_ticket, tid, None)
            if isinstance(doc, dict):
                t = doc.get("ticket")
                if isinstance(t, dict):
                    tickets_out.append(_ticket_compact_resolver(t))
        if not tickets_out:
            query_string = f'type:ticket "{rid}"'
            tickets = await asyncio.to_thread(
                service.search_tickets,
                query_string=query_string,
                limit=limit,
                batch_size=batch_size,
            )
            if isinstance(tickets, list):
                for t in tickets[:limit]:
                    if isinstance(t, dict):
                        tickets_out.append(_ticket_compact_resolver(t))
        eff = "compact" if dl == "compact" else dl
        return structured_result(
            with_response_meta(
                {"count": len(tickets_out), "data": tickets_out, "detail_level": eff},
                tool="zendesk_find_ticket_by_reservation_id",
            )
        )
    except Exception as e:
        logger.exception("zendesk_find_ticket_by_reservation_id failed")
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Zendesk API status.",
            )
        )


@mcp.tool(structured_output=False)
async def zendesk_search_tickets(
    query_string: str,
    limit: int = 1000,
    batch_size: int = 100,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Search tickets with raw Zendesk query syntax (search / read).

    Prefer ``zendesk_find_ticket_by_reservation_id`` when the user only has a reservation id; use this for status, free-text, or custom search strings.

    Input: ``query_string`` (e.g. ``type:ticket status:open``), ``limit``, ``batch_size``.

    Use when:
        You can express the filter in Zendesk search syntax.

    Args:
        query_string: Zendesk search query (e.g. type:ticket status:open).
        limit: Max tickets across internal pages.
        batch_size: Page size per request (max 100).
        detail_level: compact (default) | summary | full per ticket.

    Returns:
        ``{"data": [...], "count", "detail_level", "meta": {...}}``.
        ``data`` is the authoritative list field.

    Notes:
        Pagination is handled internally until limit or exhaustion.

    Errors:
        upstream_failed on exception.

    Example:
        zendesk_search_tickets("type:ticket status:new", limit=100)
    """
    dl = parse_detail_level(detail_level)
    limit = coerce_int(limit, default=1000, minimum=1, maximum=10000)
    batch_size = coerce_int(batch_size, default=100, minimum=1, maximum=100)
    try:
        service = _get_service()
        tickets = await asyncio.to_thread(
            service.search_tickets,
            query_string=query_string,
            limit=limit,
            batch_size=batch_size,
        )
        masked = [_mask_ticket(t, dl) for t in tickets] if isinstance(tickets, list) else tickets
        return structured_result(
            with_response_meta(
                {"data": masked, "count": len(tickets) if isinstance(tickets, list) else 0, "detail_level": dl},
                tool="zendesk_search_tickets",
                # This tool exhausts pages internally up to `limit`; no continuation cursor for the caller.
                pagination=build_pagination_meta(limit=limit),
            )
        )
    except Exception as e:
        logger.exception("zendesk_search_tickets failed")
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Zendesk API status.",
            )
        )


@mcp.tool(structured_output=False)
async def zendesk_get_ticket_audits(
    ticket_ids: Union[int, List[int]],
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Get ticket audit history (read/detail).

    Use when:
        You need audit events for one or more ticket ids.

    Args:
        ticket_ids: Single id or list of ids.
        detail_level: compact (default) | summary | full.

    Returns:
        { "audits": [...], "count", "detail_level" }.

    Notes:
        summary caps events per audit; full returns service payload unchanged.

    Errors:
        upstream_failed on exception.

    Example:
        zendesk_get_ticket_audits([1, 2], detail_level="summary")
    """
    dl = parse_detail_level(detail_level)
    try:
        service = _get_service()
        audits = await asyncio.to_thread(service.get_ticket_audits, ticket_ids)
        if dl == "full" or not isinstance(audits, list):
            return structured_result(
                with_response_meta(
                    {"audits": audits, "count": len(audits) if isinstance(audits, list) else 0, "detail_level": dl},
                    tool="zendesk_get_ticket_audits",
                    data_from="audits",
                )
            )
        return structured_result(
            with_response_meta(
                {
                    "audits": [_mask_audit(a, dl) for a in audits],
                    "count": len(audits),
                    "detail_level": dl,
                },
                tool="zendesk_get_ticket_audits",
                data_from="audits",
            ),
        )
    except Exception as e:
        logger.exception("zendesk_get_ticket_audits failed")
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Zendesk API status.",
            )
        )


@mcp.tool(structured_output=False)
async def zendesk_get_users(
    user_ids: List[int],
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Get Zendesk users by id (read/detail).

    Use when:
        You already have numeric user ids.

    Args:
        user_ids: Ids to fetch (may be chunked by the client).
        detail_level: compact (default) | summary | full.

    Returns:
        ``{"data": [...], "count", "detail_level", "meta": {...}}``.

    Notes:
        compact keeps id, name, email.

    Errors:
        upstream_failed on exception.

    Example:
        zendesk_get_users([101, 102], detail_level="compact")
    """
    dl = parse_detail_level(detail_level)
    try:
        service = _get_service()
        users = await asyncio.to_thread(service.get_users, user_ids)
        if dl == "full" or not isinstance(users, list):
            return structured_result(
                with_response_meta(
                    {"data": users, "count": len(users) if isinstance(users, list) else 0, "detail_level": dl},
                    tool="zendesk_get_users",
                )
            )
        return structured_result(
            with_response_meta(
                {"data": [_mask_user(u, dl) for u in users], "count": len(users), "detail_level": dl},
                tool="zendesk_get_users",
            ),
        )
    except Exception as e:
        logger.exception("zendesk_get_users failed")
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Zendesk API status.",
            )
        )


@mcp.tool(structured_output=False)
async def zendesk_fetch_reservation_record(
    reservation_id: str,
    detail_level: Optional[str] = None,
) -> CallToolResult:
    """Fetch reservations_data custom object row for a reservation id (read/detail).

    Use when:
        You need the Zendesk reservation custom object for one id.

    Args:
        reservation_id: Business reservation id understood by ZendeskService.fetch_reservation_record.
        detail_level: compact | summary | full (default summary).

    Returns:
        Service record payload with masking applied like other custom object records.

    Notes:
        Prefer zendesk_search_custom_object_records for ad-hoc filters.

    Errors:
        ``not_found`` when Zendesk returns HTTP 404; ``upstream_error`` on transport failure; ``upstream_failed`` on unexpected exceptions.

    Example:
        zendesk_fetch_reservation_record("RES-9", detail_level="compact")
    """
    dl = parse_detail_level(detail_level, default="summary")
    try:
        service = _get_service()
        data = await asyncio.to_thread(service.fetch_reservation_record, reservation_id)
        if data is ZENDESK_NOT_FOUND:
            return structured_result(
                tool_error(
                    "not_found",
                    details="Reservation custom object record not found (HTTP 404).",
                    cause="not_found",
                    retryable=False,
                    suggested_fix="Verify the reservation id or that a reservations_data row exists.",
                ),
            )
        if data is None:
            return structured_result(
                tool_error(
                    "upstream_error",
                    details="Zendesk request failed when fetching the reservation record.",
                    cause="upstream_error",
                    retryable=True,
                    suggested_fix="Retry later or check credentials and Zendesk API status.",
                ),
            )
        if isinstance(data, dict):
            masked = _mask_custom_object_record(data, dl)
            if isinstance(masked, dict):
                return structured_result({**masked, "detail_level": dl})
            return structured_result({"record": masked, "detail_level": dl})
        return structured_result({"record": data, "detail_level": dl})
    except Exception as e:
        logger.exception("zendesk_fetch_reservation_record failed")
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Zendesk API status.",
            )
        )


@mcp.tool(structured_output=False)
async def zendesk_list_views(
    active_only: bool = True,
    access: Optional[str] = None,
    per_page: Optional[int] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    page_after: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    next_url: Optional[str] = None,
) -> CallToolResult:
    """List Zendesk views visible to the API user (shared/personal per ``access``).

    Use ``active_only=True`` (default) for sidebar-style active views. Follow ``meta.pagination.next_cursor``
    (opaque Zendesk ``next_page`` URL) on a later call via ``next_url``.

    View counts in the UI are approximate; use ``zendesk_get_view_ticket_counts`` for API counts.

    Args:
        active_only: If True, call List Active Views; else List Views with pagination filters.
        access: Optional ``shared``, ``personal``, or ``account`` (non-active list).
        per_page: Page size (non-active list; max varies by Zendesk).
        page: Offset-style page number (mutually exclusive with cursor params per Zendesk).
        page_size: Cursor page size ``page[size]`` (non-active list).
        page_after: Cursor ``page[after]`` (non-active list).
        sort_by: Sort field (where supported).
        sort_order: ``asc`` or ``desc``.
        next_url: Full ``next_page`` URL from a previous response (must be Zendesk).

    Returns:
        Zendesk JSON with ``views`` plus ``data`` (alias), ``meta``, and pagination when applicable.

    Errors:
        ``not_found`` (HTTP 404); ``upstream_error`` on transport failures.
    """
    service = _get_service()
    per_page_c = coerce_optional_int(per_page, minimum=1, maximum=100)
    page_size_c = coerce_optional_int(page_size, minimum=1, maximum=100)
    page_c = coerce_optional_int(page, minimum=1, maximum=10_000)
    result = await asyncio.to_thread(
        lambda: service.list_views(
            next_url=next_url,
            active_only=active_only,
            access=(access or "").strip() or None,
            per_page=per_page_c,
            page=page_c,
            page_size=page_size_c,
            page_after=(page_after or "").strip() or None,
            sort_by=(sort_by or "").strip() or None,
            sort_order=(sort_order or "").strip() or None,
        )
    )
    err = _zendesk_result_or_error(
        result,
        not_found_detail="Views list returned 404.",
        upstream_detail="Zendesk request failed when listing views.",
    )
    if err:
        return structured_result(err)
    if not isinstance(result, dict):
        return structured_result(
            tool_error(
                "upstream_failed",
                details="Unexpected Zendesk response when listing views.",
                cause="upstream_error",
                retryable=True,
            )
        )
    np = result.get("next_page")
    has_more = bool(np) if np is not None else False
    return structured_result(
        with_response_meta(
            result,
            tool="zendesk_list_views",
            pagination=build_pagination_meta(has_more=has_more, next_cursor=np if isinstance(np, str) else None),
            data_from="views",
        )
    )


@mcp.tool(structured_output=False)
async def zendesk_list_view_tickets(
    view_id: int,
    detail_level: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    per_page: Optional[int] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    page_after: Optional[str] = None,
    next_url: Optional[str] = None,
) -> CallToolResult:
    """List tickets for one Zendesk view (paginated).

    This endpoint is rate-limited per view per agent in Zendesk; prefer small pages and avoid tight loops.

    Args:
        view_id: Numeric view id from ``zendesk_list_views``.
        detail_level: compact (default) | summary | full for each ticket.
        sort_by: Column id from the view (not ``subject`` / ``submitter`` per Zendesk).
        sort_order: ``asc`` or ``desc``.
        per_page: Tickets per page where supported.
        page: Offset page number (mutually exclusive with cursor params per Zendesk).
        page_size: ``page[size]`` for cursor pagination.
        page_after: ``page[after]`` cursor.
        next_url: Full ``next_page`` URL from a prior response.

    Returns:
        Tickets list in ``data`` (and ``tickets``), ``meta``, pagination with ``next_cursor`` when present.

    Errors:
        ``validation_error`` for invalid ``view_id``; ``not_found``; ``upstream_error``.
    """
    try:
        vid = int(view_id)
    except (TypeError, ValueError):
        vid = None
    if vid is None or vid <= 0:
        return structured_result(
            tool_error(
                "validation_error",
                details=f"view_id must be a positive integer (got {view_id!r}).",
                cause="validation",
                retryable=False,
            )
        )
    dl = parse_detail_level(detail_level)
    service = _get_service()
    per_page_c = coerce_optional_int(per_page, minimum=1, maximum=100)
    page_size_c = coerce_optional_int(page_size, minimum=1, maximum=100)
    page_c = coerce_optional_int(page, minimum=1, maximum=10_000)
    result = await asyncio.to_thread(
        lambda: service.list_view_tickets(
            vid,
            next_url=next_url,
            sort_by=(sort_by or "").strip() or None,
            sort_order=(sort_order or "").strip() or None,
            per_page=per_page_c,
            page=page_c,
            page_size=page_size_c,
            page_after=(page_after or "").strip() or None,
        )
    )
    err = _zendesk_result_or_error(
        result,
        not_found_detail=f"View {vid} not found or tickets path returned 404.",
        upstream_detail="Zendesk request failed when listing view tickets.",
    )
    if err:
        return structured_result(err)
    if not isinstance(result, dict):
        return structured_result(
            tool_error(
                "upstream_failed",
                details="Unexpected Zendesk response for view tickets.",
                cause="upstream_error",
                retryable=True,
            )
        )
    tickets = result.get("tickets")
    if not isinstance(tickets, list):
        return structured_result(
            tool_error(
                "upstream_failed",
                details="View tickets response missing tickets list.",
                cause="upstream_error",
                retryable=True,
            )
        )
    masked = [_mask_ticket(t, dl) for t in tickets]
    out = {**result, "tickets": masked, "detail_level": dl}
    np = result.get("next_page")
    has_more = bool(np) if np is not None else False
    return structured_result(
        with_response_meta(
            out,
            tool="zendesk_list_view_tickets",
            pagination=build_pagination_meta(has_more=has_more, next_cursor=np if isinstance(np, str) else None),
            data_from="tickets",
        )
    )


@mcp.tool(structured_output=False)
async def zendesk_get_view_ticket_counts(
    view_ids: List[int],
) -> CallToolResult:
    """Return cached ticket counts for up to 200 views (batched, max 20 ids per Zendesk request).

    Counts are estimates; ``value`` may be ``null`` while Zendesk refreshes cache. Large views can lag 60–90 minutes.
    Rate limits: bulk endpoint ~6 requests/minute — this tool sleeps between batches.

    Prefer this over repeatedly hitting single-view count endpoints.

    Args:
        view_ids: Distinct positive view ids (duplicates removed, order preserved).

    Returns:
        ``data`` / ``view_counts`` list of ``{view_id, value, pretty, fresh, ...}``, plus ``count`` and ``meta``.

    Errors:
        ``validation_error`` if more than 200 ids or empty list; ``upstream_error`` on failure.
    """
    parsed: List[int] = []
    seen: set[int] = set()
    for x in view_ids or []:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v <= 0 or v in seen:
            continue
        seen.add(v)
        parsed.append(v)
    if not parsed:
        return structured_result(
            tool_error(
                "validation_error",
                details="view_ids must contain at least one positive integer.",
                cause="validation",
                retryable=False,
            )
        )
    if len(parsed) > MAX_VIEW_IDS_FOR_COUNT_MANY:
        return structured_result(
            tool_error(
                "validation_error",
                details=f"At most {MAX_VIEW_IDS_FOR_COUNT_MANY} distinct view ids per request (got {len(parsed)}).",
                cause="validation",
                retryable=False,
                suggested_fix="Split into multiple calls with smaller id lists.",
            )
        )
    service = _get_service()
    result = await asyncio.to_thread(service.get_view_ticket_counts, parsed)
    err = _zendesk_result_or_error(
        result,
        not_found_detail="View counts returned 404.",
        upstream_detail="Zendesk request failed when fetching view ticket counts.",
    )
    if err:
        return structured_result(err)
    if not isinstance(result, dict):
        return structured_result(
            tool_error(
                "upstream_failed",
                details="Unexpected Zendesk response for view counts.",
                cause="upstream_error",
                retryable=True,
            )
        )
    vc = result.get("view_counts")
    rows = vc if isinstance(vc, list) else []
    return structured_result(
        with_response_meta(
            {"view_counts": rows, "count": len(rows)},
            tool="zendesk_get_view_ticket_counts",
            data_from="view_counts",
        )
    )


# ---------------------------------------------------------------------------
# Machine-readable tool metadata
# ---------------------------------------------------------------------------

TOOL_METADATA: dict = {
    "zendesk_list_views": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": False,
        "primary_param": "active_only",
    },
    "zendesk_list_view_tickets": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": False,
        "primary_param": "view_id",
    },
    "zendesk_get_view_ticket_counts": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "primary_param": "view_ids",
    },
    "zendesk_search_tickets": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
        "primary_param": "query_string",
    },
    "zendesk_get_ticket": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "ticket_id",
    },
    "zendesk_find_ticket_by_reservation_id": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "resolver": True,
        "primary_param": "reservation_id",
    },
    "zendesk_get_ticket_audits": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": True,
        "primary_param": "ticket_id",
    },
    "zendesk_get_ticket_requests": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": True,
        "primary_param": "ticket_id",
    },
    "zendesk_fetch_reservation_record": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "reservation_id",
    },
}
