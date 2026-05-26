"""MCP tools for Breezeway (read-focused with guarded task writes)."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from breezeway_client import BreezewayMcpClient
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.coerce_numeric import coerce_int
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result
from pydantic import BaseModel, ConfigDict, Field

_client: Optional[BreezewayMcpClient] = None


def _get_client() -> BreezewayMcpClient:
    global _client
    if _client is None:
        _client = BreezewayMcpClient()
    return _client


_PROPERTY_SUMMARY_BLOCK_SUBSTR: tuple[str, ...] = (
    "lockbox",
    "wifi",
    "wi-fi",
    "password",
    "keycode",
    "garage",
    "instruction",
    "access_code",
    "alarm",
    "media",
    "photo",
    "picture",
    "credential",
    "combo",
    "smart_lock",
    "pms_",
    "lock_code",
    "door_code",
    "safe_code",
    "wifi_",
    "wpa",
    "ssid",
    "checkin_instruction",
    "checkout_instruction",
)


def _property_key_sensitive(key: str) -> bool:
    kl = key.lower()
    return any(fragment in kl for fragment in _PROPERTY_SUMMARY_BLOCK_SUBSTR)


def _compact_property_row(prop: Dict[str, Any]) -> Dict[str, Any]:
    allow = (
        "id",
        "name",
        "display",
        "address1",
        "address2",
        "city",
        "state",
        "zipcode",
        "country",
        "reference_property_id",
        "reference_external_property_id",
        "status",
    )
    return {k: prop[k] for k in allow if k in prop}


def _summarize_property_row(prop: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in prop.items():
        if _property_key_sensitive(k):
            continue
        if isinstance(v, dict):
            if k.lower() in ("address", "location"):
                inner = {
                    sk: sv
                    for sk, sv in v.items()
                    if not _property_key_sensitive(sk) and isinstance(sv, (str, int, float, bool))
                }
                if inner:
                    out[k] = inner
            continue
        if isinstance(v, list):
            continue
        out[k] = v
    return out


def _summarize_properties_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    results = data.get("results")
    if not isinstance(results, list):
        return {"data": [], "detail_level": "summary"}
    summarized = [_summarize_property_row(r) for r in results if isinstance(r, dict)]
    return {"data": summarized, "detail_level": "summary"}


def _compact_properties_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    results = data.get("results")
    if not isinstance(results, list):
        return {"data": [], "detail_level": "compact"}
    rows = [_compact_property_row(r) for r in results if isinstance(r, dict)]
    return {"data": rows, "detail_level": "compact"}


_FIND_PROPERTY_MAX_PAGES = int(os.getenv("BREEZEWAY_FIND_PROPERTY_MAX_PAGES", "25"))
_BREEZEWAY_TRIAGE_DEFAULT_LIMIT = 25
_BREEZEWAY_TRIAGE_MAX_LIMIT = 100
_BREEZEWAY_TRIAGE_DEFAULT_MAX_PROPERTIES = 250
_BREEZEWAY_TRIAGE_MAX_PROPERTIES = 1000
_BREEZEWAY_TRIAGE_DEFAULT_MAX_TASKS_PER_PROPERTY = 100
_BREEZEWAY_TRIAGE_MAX_TASKS_PER_PROPERTY = 500
_BREEZEWAY_TRIAGE_FETCH_CONCURRENCY = int(os.getenv("BREEZEWAY_TRIAGE_FETCH_CONCURRENCY", "8"))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _property_row_text(row: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("name", "display", "address1", "address2", "city", "state", "zipcode", "country"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    for key in ("reference_property_id", "reference_external_property_id"):
        ref = row.get(key)
        if ref is not None:
            parts.append(str(ref))
    return _norm(" ".join(parts))


def _row_matches_hint(row: Dict[str, Any], hint: str) -> bool:
    hn = _norm(hint)
    if not hn:
        return False
    hay = _property_row_text(row)
    if hn in hay:
        return True
    rid = row.get("id")
    if rid is not None and hn == str(rid).strip().lower():
        return True
    for key in ("reference_property_id", "reference_external_property_id"):
        ref = row.get(key)
        if ref is not None and hn == _norm(str(ref)):
            return True
    return False


def _breezeway_resolver_property_row(p: Dict[str, Any]) -> Dict[str, Any]:
    ref_ext = p.get("reference_external_property_id")
    ref_p = p.get("reference_property_id")
    st = p.get("status")
    return {
        "id": int(p["id"]) if p.get("id") is not None else 0,
        "name": str(p.get("name") or ""),
        "display": p.get("display") if p.get("display") is not None else None,
        "city": p.get("city") if p.get("city") is not None else None,
        "state": p.get("state") if p.get("state") is not None else None,
        "country": p.get("country") if p.get("country") is not None else None,
        "reference_external_property_id": str(ref_ext) if ref_ext is not None else None,
        "reference_property_id": str(ref_p) if ref_p is not None else None,
        "status": str(st) if st is not None else None,
    }


def _property_match_rank(row: Dict[str, Any], query_norm: str) -> Optional[Tuple[int, int]]:
    """Lower tier wins; None = no match. query_norm = _norm(query)."""
    if not query_norm:
        return None
    name_n = _norm(str(row.get("name") or ""))
    disp_n = _norm(str(row.get("display") or ""))
    if name_n and name_n == query_norm:
        return (0, len(name_n))
    if disp_n and disp_n == query_norm:
        return (1, len(disp_n))
    for key in ("reference_property_id", "reference_external_property_id"):
        ref = row.get(key)
        if ref is not None and query_norm == _norm(str(ref)):
            return (2, 0)
    if name_n and query_norm in name_n:
        return (3, len(name_n))
    if disp_n and query_norm in disp_n:
        return (4, len(disp_n))
    hay = _property_row_text(row)
    if query_norm in hay:
        return (5, 0)
    return None


def _today() -> date:
    return date.today()


def _normalize_text(value: Any) -> str:
    return _norm(str(value or ""))


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_iso_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = _parse_iso_datetime(value)
    if parsed is not None:
        return parsed.date()
    return None


def _priority_rank(value: Any) -> Tuple[str, int]:
    text = _normalize_text(value)
    if not text:
        return ("unknown", 20)
    if any(token in text for token in ("emergency", "urgent", "critical", "highest", "asap")):
        return (str(value), 90)
    if "high" in text:
        return (str(value), 70)
    if any(token in text for token in ("medium", "normal", "standard")):
        return (str(value), 40)
    if any(token in text for token in ("low", "minor", "lowest")):
        return (str(value), 15)
    return (str(value), 30)


def _task_is_closed(task: Dict[str, Any]) -> bool:
    if task.get("finished_at"):
        return True
    closed_tokens = ("close", "closed", "complete", "completed", "done", "finish", "finished", "cancel", "approved")
    for key in ("status", "stage", "state", "workflow_state"):
        text = _normalize_text(task.get(key))
        if text and any(token in text for token in closed_tokens):
            return True
    return False


def _assignee_display_name(user: Dict[str, Any]) -> Optional[str]:
    for key in ("name", "full_name", "display_name"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    first = str(user.get("firstName") or user.get("first_name") or "").strip()
    last = str(user.get("lastName") or user.get("last_name") or "").strip()
    full = " ".join(part for part in (first, last) if part)
    if full:
        return full
    email = user.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def _extract_assignments(task: Dict[str, Any], users_by_id: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw_assignments = task.get("assignments")
    if not isinstance(raw_assignments, list):
        return []
    out: List[Dict[str, Any]] = []
    for assignment in raw_assignments:
        if not isinstance(assignment, dict):
            continue
        assignee_raw = assignment.get("assignee_id") or assignment.get("id") or assignment.get("user_id")
        try:
            assignee_id = int(assignee_raw) if assignee_raw is not None else None
        except (TypeError, ValueError):
            assignee_id = None
        user = users_by_id.get(assignee_id) if assignee_id is not None else None
        name = _assignee_display_name(assignment) or (_assignee_display_name(user) if isinstance(user, dict) else None)
        row: Dict[str, Any] = {}
        if assignee_id is not None:
            row["id"] = assignee_id
        if name:
            row["name"] = name
        if row:
            out.append(row)
    return out


def _comment_text(comment: Dict[str, Any]) -> Optional[str]:
    for key in ("comment", "message", "body", "text", "content"):
        value = comment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _comment_sort_key(comment: Dict[str, Any]) -> Tuple[int, str]:
    for key in ("created_at", "updated_at", "timestamp"):
        value = comment.get(key)
        parsed = _parse_iso_datetime(value)
        if parsed is not None:
            return (1, parsed.isoformat())
    return (0, "")


def _property_context(property_row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "property_id": int(property_row["id"]) if property_row.get("id") is not None else None,
        "property_name": str(property_row.get("name") or property_row.get("display") or ""),
        "reference_property_id": str(property_row.get("reference_property_id") or "") or None,
        "city": property_row.get("city"),
        "status": property_row.get("status"),
    }


def _triage_bucket(
    *,
    priority_score: int,
    scheduled_for: Optional[date],
    today: date,
    is_assigned: bool,
    department: Optional[str],
) -> Tuple[str, str, List[str], int]:
    reasons: List[str] = []
    score = priority_score

    if priority_score >= 70:
        reasons.append(f"priority={priority_score}")

    if scheduled_for is not None:
        days_until = (scheduled_for - today).days
        if days_until < 0:
            reasons.append(f"overdue_by={abs(days_until)}d")
            score += 40 + min(abs(days_until), 7) * 3
            if priority_score >= 70:
                return ("act_now", "Act now", reasons, score)
        elif days_until == 0:
            reasons.append("due=today")
            score += 25
            if priority_score >= 70:
                return ("act_now", "Act now", reasons, score)
        elif days_until <= 2:
            reasons.append(f"due_in={days_until}d")
            score += 10

    dep_text = _normalize_text(department)
    if dep_text and any(token in dep_text for token in ("safety", "maintenance")):
        reasons.append(f"department={dep_text}")
        score += 8

    if not is_assigned:
        reasons.append("unassigned")
        score += 8
        if priority_score >= 70:
            return ("act_now", "Act now", reasons, score)

    if scheduled_for is not None and scheduled_for <= today:
        return ("act_now", "Act now", reasons, score)
    if priority_score >= 70:
        return ("next_up", "Next up", reasons, score)
    return ("watchlist", "Watchlist", reasons, score)


def _triage_task_row(
    task: Dict[str, Any],
    *,
    property_info: Dict[str, Any],
    users_by_id: Dict[int, Dict[str, Any]],
    today: date,
) -> Optional[Dict[str, Any]]:
    task_id_raw = task.get("id")
    try:
        task_id = int(task_id_raw) if task_id_raw is not None else None
    except (TypeError, ValueError):
        task_id = None
    if task_id is None:
        return None
    if _task_is_closed(task):
        return None

    priority_text, priority_score = _priority_rank(task.get("type_priority"))
    assignments = _extract_assignments(task, users_by_id)
    scheduled_for = _parse_iso_date(task.get("scheduled_date"))
    department = str(task.get("type_department") or "") or None
    bucket, bucket_label, reasons, urgency_score = _triage_bucket(
        priority_score=priority_score,
        scheduled_for=scheduled_for,
        today=today,
        is_assigned=bool(assignments),
        department=department,
    )

    return {
        "id": task_id,
        "name": str(task.get("name") or ""),
        "property_id": property_info.get("property_id"),
        "property_name": property_info.get("property_name"),
        "reference_property_id": property_info.get("reference_property_id"),
        "city": property_info.get("city"),
        "department": department,
        "priority": priority_text,
        "scheduled_date": task.get("scheduled_date"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "assignments": assignments,
        "assignment_count": len(assignments),
        "urgency_bucket": bucket,
        "urgency_bucket_label": bucket_label,
        "urgency_score": urgency_score,
        "needs_attention_now": bucket == "act_now",
        "reasons": reasons,
        "raw": task,
    }


async def _list_all_properties(
    client: BreezewayMcpClient,
    *,
    max_properties: int,
) -> Tuple[List[Dict[str, Any]], int]:
    properties: List[Dict[str, Any]] = []
    page = 1
    pages_scanned = 0
    while len(properties) < max_properties:
        raw = await asyncio.to_thread(client.list_properties_page, page, 100)
        pages_scanned += 1
        rows = raw.get("results", []) if isinstance(raw, dict) else []
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if isinstance(row, dict):
                properties.append(row)
                if len(properties) >= max_properties:
                    break
        total_pages = raw.get("total_pages") if isinstance(raw, dict) else None
        if isinstance(total_pages, int) and page >= total_pages:
            break
        page += 1
    return properties, pages_scanned


async def _list_property_tasks_for_triage(
    client: BreezewayMcpClient,
    *,
    property_row: Dict[str, Any],
    max_tasks_per_property: int,
) -> Tuple[List[Dict[str, Any]], int]:
    property_id = property_row.get("id")
    try:
        home_id = int(property_id) if property_id is not None else None
    except (TypeError, ValueError):
        home_id = None
    if home_id is None:
        return ([], 0)

    tasks: List[Dict[str, Any]] = []
    page = 1
    pages_scanned = 0
    while len(tasks) < max_tasks_per_property:
        remaining = max_tasks_per_property - len(tasks)
        raw = await asyncio.to_thread(
            client.list_tasks_page,
            page=page,
            limit=min(100, remaining),
            home_id=home_id,
        )
        pages_scanned += 1
        rows = raw.get("results", []) if isinstance(raw, dict) else []
        if not isinstance(rows, list) or not rows:
            break
        tasks.extend(dict(row) for row in rows if isinstance(row, dict))
        total_pages = raw.get("total_pages") if isinstance(raw, dict) else None
        if isinstance(total_pages, int) and page >= total_pages:
            break
        page += 1
    return tasks[:max_tasks_per_property], pages_scanned


mcp = FastMCP(
    "breezeway",
    instructions=(
        "Breezeway inventory API: list properties (paginated), breezeway_find_property_by_name_or_external_id, "
        "breezeway_get_property_summary (GET /property/{id}), list people, list tasks, portfolio task triage, "
        "get task comments, move/update tasks, get reservation by external ID. "
        "detail_level: compact (minimal identity/location), summary (default for lists — redacts sensitive-shaped keys), "
        "full (opt-in raw API). Task move/update tools mutate Breezeway state and require write permission behind mcp-proxy. "
        "Behind mcp-proxy id 'breezeway' tools are breezeway_*."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


@mcp.tool(structured_output=False)
async def breezeway_list_properties_page(
    page: int = 1,
    limit: int = 100,
    detail_level: Literal["compact", "summary", "full"] = "summary",
) -> CallToolResult:
    """List Breezeway properties in paginated form (read/list).

    **Use when:**
        You need property discovery without operational secrets by default.

    **Args:**
        page: 1-based page index.
        limit: Page size.
        detail_level: ``summary`` (default) redacts sensitive-shaped keys; ``compact`` keeps only core identity/location columns; ``full`` is raw API.

    **Returns:**
        ``{"data": [...], "detail_level",
        "meta": {"tool", "schema_version", "pagination": {limit, offset, has_more, total_count}}}``.
        ``data`` is the authoritative list field.

    **Notes:**
        Use ``detail_level='full'`` only when operational metadata is required.

    **Errors:**
        ``{"error", "details"}`` on failure.

    **Example:**
        ``breezeway_list_properties_page(page=1, limit=50)``
    """
    page = coerce_int(page, default=1, minimum=1)
    limit = coerce_int(limit, default=100, minimum=1, maximum=500)
    client = _get_client()
    raw: Dict[str, Any] = await asyncio.to_thread(client.list_properties_page, page, limit)

    def _build_pagination(payload: Dict[str, Any]) -> Dict[str, Any]:
        total_pages = payload.get("total_pages")
        total_results = payload.get("total_results")
        offset = (page - 1) * limit
        has_more = (page < total_pages) if isinstance(total_pages, int) else None
        return build_pagination_meta(
            limit=limit,
            offset=offset,
            has_more=has_more,
            next_offset=offset + limit if has_more else None,
            total_count=total_results if isinstance(total_results, int) else None,
        )

    if not isinstance(raw, dict):
        return structured_result({"data": raw, "detail_level": detail_level})

    if detail_level == "full":
        body = {**raw, "detail_level": "full"}
        if "data" not in body and isinstance(body.get("results"), list):
            body["data"] = body["results"]
        return structured_result(
            with_response_meta(body, tool="breezeway_list_properties_page", pagination=_build_pagination(raw))
        )
    if detail_level == "summary":
        body = _summarize_properties_payload(raw)
        return structured_result(
            with_response_meta(body, tool="breezeway_list_properties_page", pagination=_build_pagination(raw))
        )
    # compact (default)
    body = _compact_properties_payload(raw)
    return structured_result(
        with_response_meta(body, tool="breezeway_list_properties_page", pagination=_build_pagination(raw))
    )


@mcp.tool(structured_output=False)
async def breezeway_find_property_by_name_or_external_id(
    query: str,
    limit: int = 10,
    max_pages: Optional[int] = None,
    page_size: int = 100,
) -> CallToolResult:
    """Resolve Breezeway properties from business identifiers agents know (read/search).

    IMPORTANT: The search parameter is named ``query`` — do not pass ``name``, ``external_id``, or ``id``.

    Use when:
        You have a property name, display name, or external/PMS id string — without manually scanning list pages.

    Args:
        query: Required text matched against ``name``, ``display``, ``reference_external_property_id``,
            ``reference_property_id``, and address fields (case-insensitive; exact matches sort before partial).
        limit: Max properties to return after ranking (default 10).
        max_pages: Optional cap on list scans (default env BREEZEWAY_FIND_PROPERTY_MAX_PAGES).
        page_size: Page size for inventory list API.

    Returns:
        ``{"count", "data", "detail_level": "compact"}`` with stable identity/location fields only (no access secrets).

    Notes:
        Numeric ``query`` attempts ``GET /property/{id}`` first. Several rows mean ambiguous matches — do not assume
        the first is correct without confirmation.

    Errors:
        ``{"error": "validation_error", "details": ...}`` when ``query`` is empty.
        ``{"error": "upstream_failed", "details": ...}`` (as ``request_failed``) when the list API fails mid-scan.

    Example:
        ``breezeway_find_property_by_name_or_external_id(query="Beach House", limit=5)``
    """
    h = (query or "").strip()
    if not h:
        return structured_result(
            tool_error("validation_error", details="query is required", cause="validation", retryable=False)
        )

    client = _get_client()

    if h.isdigit():
        try:
            raw = await asyncio.to_thread(client.get_property, int(h))
            if isinstance(raw, dict):
                return structured_result(
                    with_response_meta(
                        {
                            "count": 1,
                            "data": [_breezeway_resolver_property_row(raw)],
                            "detail_level": "compact",
                        },
                        tool="breezeway_find_property_by_name_or_external_id",
                    )
                )
        except Exception:
            pass

    cap = max_pages if max_pages is not None else _FIND_PROPERTY_MAX_PAGES
    cap = max(1, min(cap, 500))
    qn = _norm(h)
    ranked: List[Tuple[Tuple[int, int], Dict[str, Any]]] = []
    page = 1
    while page <= cap and len(ranked) < max(50, limit * 5):
        try:
            raw_page = await asyncio.to_thread(client.list_properties_page, page, min(max(page_size, 1), 100))
        except Exception as e:
            if not ranked:
                return structured_result(
                    tool_error(
                        "upstream_failed",
                        details=str(e),
                        cause="upstream_error",
                        retryable=True,
                        suggested_fix="Retry later or check Breezeway API status.",
                    )
                )
            break
        rows = raw_page.get("results", []) if isinstance(raw_page, dict) else []
        if not isinstance(rows, list):
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not _row_matches_hint(row, h):
                continue
            rk = _property_match_rank(row, qn)
            if rk is not None:
                ranked.append((rk, row))
        total_pages = raw_page.get("total_pages") if isinstance(raw_page, dict) else None
        if isinstance(total_pages, int) and page >= total_pages:
            break
        if not rows:
            break
        page += 1

    ranked.sort(key=lambda x: (x[0][0], x[0][1]))
    data = [_breezeway_resolver_property_row(dict(r)) for _, r in ranked[: max(1, min(limit, 50))]]
    return structured_result(
        with_response_meta(
            {"count": len(data), "data": data, "detail_level": "compact"},
            tool="breezeway_find_property_by_name_or_external_id",
        )
    )


@mcp.tool(structured_output=False)
async def breezeway_get_property_summary(
    property_id: int,
) -> CallToolResult:
    """Fetch one safe, compact property summary by exact Breezeway property id (read/detail).

    IMPORTANT: The parameter is named ``property_id`` (integer) — do not pass ``id``, ``name``, or ``query``.

    Use when:
        You already have the numeric Breezeway ``property_id`` (from list tools or ``breezeway_find_property_by_name_or_external_id``).

    Args:
        property_id: Breezeway inventory property id (integer).

    Returns:
        Summary fields with ``detail_level: "summary"`` — sensitive access-shaped keys stripped (same rules as list summary mode).

    Notes:
        Does not return lockbox/Wi-Fi style operational secrets by default.

    Errors:
        ``{"error": "upstream_failed", "details": ...}`` on HTTP failure.
        ``{"error": "unexpected_shape", "details": ...}`` when the payload is not a property object.

    Example:
        ``breezeway_get_property_summary(property_id=12345)``
    """
    client = _get_client()
    try:
        raw = await asyncio.to_thread(client.get_property, int(property_id))
    except Exception as e:
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify property_id and Breezeway API status.",
            )
        )
    if not isinstance(raw, dict):
        return structured_result(
            tool_error(
                "unexpected_shape",
                details="unexpected response shape",
                cause="upstream_error",
                retryable=False,
            )
        )
    s = _summarize_property_row(raw)
    ref_ext = raw.get("reference_external_property_id")
    ref_p = raw.get("reference_property_id")
    st = raw.get("status")
    return structured_result(
        {
            "id": int(raw.get("id") or property_id),
            "name": str(s.get("name") or ""),
            "display": s.get("display") if s.get("display") is not None else None,
            "city": s.get("city") if s.get("city") is not None else None,
            "state": s.get("state") if s.get("state") is not None else None,
            "country": s.get("country") if s.get("country") is not None else None,
            "zipcode": s.get("zipcode") if s.get("zipcode") is not None else raw.get("zipcode"),
            "status": str(st) if st is not None else None,
            "reference_external_property_id": str(ref_ext) if ref_ext is not None else None,
            "reference_property_id": str(ref_p) if ref_p is not None else None,
            "detail_level": "summary",
        }
    )


_BREEZEWAY_LIST_USERS_MAX_LIMIT = 500
_BREEZEWAY_LIST_USERS_DEFAULT_LIMIT = 50
_BREEZEWAY_LIST_TASKS_DEFAULT_LIMIT = 50
_BREEZEWAY_LIST_TASKS_MAX_LIMIT = 100
_BREEZEWAY_TASK_MOVE_ACTIONS = ("close", "approve", "reopen")


class BreezewayTaskUpdatePayload(BaseModel):
    """Common Breezeway PATCH /task/{id} fields with extra-key passthrough."""

    model_config = ConfigDict(extra="allow")

    name: Optional[str] = Field(
        default=None,
        description="Task name. Breezeway's update-task docs mark this field as required on the PATCH endpoint.",
    )
    type_department: Optional[Literal["housekeeping", "inspection", "maintenance", "safety"]] = Field(
        default=None,
        description="Task department enum from Breezeway.",
    )
    type_priority: Optional[str] = Field(
        default=None,
        description="Task priority enum from Breezeway. Keep the exact upstream value used by your Breezeway account.",
    )
    description: Optional[str] = Field(default=None, description="Task description.")
    template_id: Optional[int] = Field(default=None, description="Breezeway task template id.")
    scheduled_date: Optional[str] = Field(
        default=None,
        description="Scheduled date in ISO 8601 date format YYYY-MM-DD.",
    )
    scheduled_time: Optional[str] = Field(
        default=None,
        description="Scheduled time in HH:MM:SS format.",
    )
    assignments: Optional[List[int]] = Field(
        default=None,
        description="Array of Breezeway person ids assigned to the task.",
    )
    tags: Optional[List[int]] = Field(
        default=None,
        description="Array of Breezeway task tag ids.",
    )
    subdepartment_id: Optional[int] = Field(default=None, description="Breezeway subdepartment id.")
    rate_paid: Optional[float] = Field(default=None, description="Estimated rate paid.")
    rate_type: Optional[Literal["piece", "hourly"]] = Field(
        default=None,
        description="Estimated rate type.",
    )
    requested_by: Optional[str] = Field(
        default=None,
        description="Requester enum from Breezeway. Use the exact upstream enum value.",
    )


@mcp.tool(structured_output=False)
async def breezeway_list_users(
    detail_level: Literal["compact", "full"] = "compact",
    limit: int = _BREEZEWAY_LIST_USERS_DEFAULT_LIMIT,
    offset: int = 0,
) -> CallToolResult:
    """List Breezeway people/users (read/list) with offset/limit over the full /people response.

    **Use when:**
        You need user directory / ids.

    **Args:**
        detail_level: ``compact`` returns id+name-like scalars per user when possible; ``full`` returns raw rows.
        limit: Max users to return this page (1–500, default 50). The upstream API returns all people in one call;
            pagination is applied in-process to cap payload size.
        offset: Number of users to skip from the start of the full list (default 0).

    **Returns:**
        ``{"data": [...], "count", "detail_level", "meta": {"tool", "schema_version", "pagination": {limit, offset, has_more, next_offset, total_count}}}``.
        ``data`` is the authoritative list field; pagination state is exclusively in ``meta.pagination``.

    **Notes:**
        ``full`` may include operational fields; default ``compact`` is agent-safe.

    **Errors:**
        ``{"error", "details"}`` on transport failure.

    **Example:**
        ``breezeway_list_users(limit=50, offset=0)``
    """
    if limit < 1 or limit > _BREEZEWAY_LIST_USERS_MAX_LIMIT:
        return structured_result(
            tool_error(
                "validation_error",
                details=f"limit must be between 1 and {_BREEZEWAY_LIST_USERS_MAX_LIMIT}",
                cause="validation",
                retryable=False,
            )
        )
    if offset < 0:
        return structured_result(
            tool_error("validation_error", details="offset must be >= 0", cause="validation", retryable=False)
        )

    client = _get_client()
    users: List[Dict[str, Any]] = await asyncio.to_thread(client.list_all_users)
    total = len(users)
    page = users[offset : offset + limit]
    has_more = offset + limit < total
    next_offset = offset + limit if has_more else None

    pagination = build_pagination_meta(
        limit=limit,
        offset=offset,
        has_more=has_more,
        next_offset=next_offset,
        total_count=total,
    )
    if detail_level == "full":
        return structured_result(
            with_response_meta(
                {"data": page, "count": len(page), "detail_level": "full"},
                tool="breezeway_list_users",
                pagination=pagination,
            )
        )
    compact_users = []
    for u in page:
        if isinstance(u, dict):
            compact_users.append(
                {k: u[k] for k in ("id", "first_name", "last_name", "email", "role", "name") if k in u}
                or {"id": u.get("id")}
            )
        else:
            compact_users.append(u)
    return structured_result(
        with_response_meta(
            {"data": compact_users, "count": len(compact_users), "detail_level": "compact"},
            tool="breezeway_list_users",
            pagination=pagination,
        )
    )


@mcp.tool(structured_output=False)
async def breezeway_list_tasks(
    page: int = 1,
    limit: int = _BREEZEWAY_LIST_TASKS_DEFAULT_LIMIT,
    home_id: Optional[int] = None,
    reference_property_id: Optional[str] = None,
    scheduled_date: Optional[str] = None,
    created_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    updated_at: Optional[str] = None,
    assignee_ids: Optional[List[int]] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[Literal["asc", "desc"]] = None,
    include_comments: bool = False,
) -> CallToolResult:
    """List Breezeway tasks (read/list).

    Use when:
        You need operational tasks for one property/home, optionally with per-task comments hydrated inline.

    Args:
        page, limit: Pagination over Breezeway's native task list endpoint.
        home_id: Breezeway home/property id.
        reference_property_id: External/property reference id.
        scheduled_date, created_at, finished_at, updated_at: Optional API date filters.
        assignee_ids: Optional assignee id list forwarded as a comma-delimited filter.
        sort_by, sort_order: Optional Breezeway sort parameters.
        include_comments: When true, fetch ``/task/{id}/comments`` for each returned task and attach them under ``comments``.

    Returns:
        ``{"data": [...], "count", "detail_level": "full", "meta": {"tool", "schema_version", "pagination": {...}}}``.

    Notes:
        Breezeway documents either ``home_id`` or ``reference_property_id`` for task retrieval. Comment hydration is best-effort
        per task: failures are attached as ``comments_error`` on the individual task instead of failing the whole page.

    Errors:
        ``{"error", "details"}`` on validation or upstream failure.
    """
    page = coerce_int(page, default=1, minimum=1)
    limit = coerce_int(
        limit, default=_BREEZEWAY_LIST_TASKS_DEFAULT_LIMIT, minimum=1, maximum=_BREEZEWAY_LIST_TASKS_MAX_LIMIT
    )
    if home_id is None and not (reference_property_id or "").strip():
        return structured_result(
            tool_error(
                "validation_error",
                details="either home_id or reference_property_id is required",
                cause="validation",
                retryable=False,
            )
        )
    if sort_order is not None and sort_order not in {"asc", "desc"}:
        return structured_result(
            tool_error(
                "validation_error",
                details="sort_order must be 'asc' or 'desc'",
                cause="validation",
                retryable=False,
            )
        )

    client = _get_client()
    try:
        raw: Dict[str, Any] = await asyncio.to_thread(
            client.list_tasks_page,
            page=page,
            limit=limit,
            home_id=home_id,
            reference_property_id=(reference_property_id or None),
            scheduled_date=scheduled_date,
            created_at=created_at,
            finished_at=finished_at,
            updated_at=updated_at,
            assignee_ids=assignee_ids,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    except Exception as e:
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify the task filters and Breezeway API status.",
            )
        )

    rows = raw.get("results", []) if isinstance(raw, dict) else []
    if not isinstance(rows, list):
        return structured_result(
            tool_error(
                "unexpected_shape",
                details="unexpected response shape",
                cause="upstream_error",
                retryable=False,
            )
        )

    data: List[Dict[str, Any]] = [dict(row) for row in rows if isinstance(row, dict)]
    if include_comments and data:
        comment_tasks = []
        task_ids: List[Optional[int]] = []
        for row in data:
            task_id_raw = row.get("id")
            try:
                task_id = int(task_id_raw) if task_id_raw is not None else None
            except (TypeError, ValueError):
                task_id = None
            task_ids.append(task_id)
            comment_tasks.append(
                asyncio.to_thread(client.get_task_comments, task_id)
                if task_id is not None
                else asyncio.sleep(0, result=[])
            )
        comments_results = await asyncio.gather(*comment_tasks, return_exceptions=True)
        for row, task_id, comments_result in zip(data, task_ids, comments_results):
            if task_id is None:
                row["comments_error"] = "task id missing from task payload"
                continue
            if isinstance(comments_result, Exception):
                row["comments_error"] = str(comments_result)
                continue
            row["comments"] = comments_result
            if isinstance(comments_result, list):
                row["comments_count"] = len(comments_result)

    total_pages = raw.get("total_pages") if isinstance(raw, dict) else None
    total_results = raw.get("total_results") if isinstance(raw, dict) else None
    offset = (page - 1) * limit
    has_more = (page < total_pages) if isinstance(total_pages, int) else None
    pagination = build_pagination_meta(
        limit=limit,
        offset=offset,
        has_more=has_more,
        next_offset=offset + limit if has_more else None,
        total_count=total_results if isinstance(total_results, int) else None,
    )
    body = {"data": data, "count": len(data), "detail_level": "full", "include_comments": include_comments}
    return structured_result(with_response_meta(body, tool="breezeway_list_tasks", pagination=pagination))


def _build_triage_groups(tasks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    order = ("act_now", "next_up", "watchlist")
    labels = {
        "act_now": "Act now",
        "next_up": "Next up",
        "watchlist": "Watchlist",
    }
    groups: List[Dict[str, Any]] = []
    for key in order:
        rows = [task for task in tasks if task.get("urgency_bucket") == key]
        if not rows:
            continue
        groups.append(
            {
                "key": key,
                "label": labels[key],
                "count": len(rows),
                "task_ids": [task["id"] for task in rows if task.get("id") is not None],
            }
        )
    return groups


@mcp.tool(structured_output=False)
async def breezeway_triage_tasks(
    limit: int = _BREEZEWAY_TRIAGE_DEFAULT_LIMIT,
    include_comments: bool = False,
    max_properties: int = _BREEZEWAY_TRIAGE_DEFAULT_MAX_PROPERTIES,
    max_tasks_per_property: int = _BREEZEWAY_TRIAGE_DEFAULT_MAX_TASKS_PER_PROPERTY,
) -> CallToolResult:
    """Scan Breezeway properties, rank open tasks across the portfolio, and group the queue by urgency (read/list).

    Use when:
        You need a portfolio-wide view of the operational queue instead of querying one home/property at a time.

    Args:
        limit: Maximum number of ranked tasks to return after scoring/grouping.
        include_comments: When true, fetch comments for the returned tasks only (not the full scanned queue).
        max_properties: Safety cap on how many properties to scan before ranking.
        max_tasks_per_property: Safety cap on how many tasks to scan per property.

    Returns:
        ``{"data": [...], "recommended_now": [...], "groups": [...], "scan_summary": {...}, "detail_level": "summary"}``.
        ``data`` is the ranked shortlist. ``recommended_now`` is a compact subset of tasks that should be acted on first.

    Notes:
        This tool is the portfolio-level exception to the normal Breezeway task-scope rule. It still uses scoped
        per-property task calls under the hood so the output remains grounded in the same upstream task payloads.
        Comments, when requested, are fetched only for the returned shortlist to keep scans bounded.

    Errors:
        ``{"error", "details"}`` on validation or upstream failure.
    """
    limit = coerce_int(limit, default=_BREEZEWAY_TRIAGE_DEFAULT_LIMIT, minimum=1, maximum=_BREEZEWAY_TRIAGE_MAX_LIMIT)
    max_properties = coerce_int(
        max_properties,
        default=_BREEZEWAY_TRIAGE_DEFAULT_MAX_PROPERTIES,
        minimum=1,
        maximum=_BREEZEWAY_TRIAGE_MAX_PROPERTIES,
    )
    max_tasks_per_property = coerce_int(
        max_tasks_per_property,
        default=_BREEZEWAY_TRIAGE_DEFAULT_MAX_TASKS_PER_PROPERTY,
        minimum=1,
        maximum=_BREEZEWAY_TRIAGE_MAX_TASKS_PER_PROPERTY,
    )

    client = _get_client()
    try:
        properties, property_pages_scanned = await _list_all_properties(client, max_properties=max_properties)
    except Exception as e:
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify Breezeway property access before scanning the portfolio.",
            )
        )

    if not properties:
        return structured_result(
            with_response_meta(
                {
                    "data": [],
                    "count": 0,
                    "recommended_now": [],
                    "groups": [],
                    "scan_summary": {
                        "properties_scanned": 0,
                        "property_pages_scanned": property_pages_scanned,
                        "task_pages_scanned": 0,
                        "tasks_scanned": 0,
                        "open_tasks_considered": 0,
                        "properties_with_errors": [],
                    },
                    "detail_level": "summary",
                },
                tool="breezeway_triage_tasks",
            )
        )

    users_by_id: Dict[int, Dict[str, Any]] = {}
    try:
        users = await asyncio.to_thread(client.list_all_users)
        if isinstance(users, list):
            for user in users:
                if not isinstance(user, dict):
                    continue
                raw_id = user.get("id")
                try:
                    user_id = int(raw_id) if raw_id is not None else None
                except (TypeError, ValueError):
                    user_id = None
                if user_id is not None:
                    users_by_id[user_id] = user
    except Exception:
        users_by_id = {}

    semaphore = asyncio.Semaphore(max(1, _BREEZEWAY_TRIAGE_FETCH_CONCURRENCY))
    property_errors: List[Dict[str, Any]] = []

    async def _scan_property(property_row: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
        async with semaphore:
            info = _property_context(property_row)
            try:
                rows, pages_scanned = await _list_property_tasks_for_triage(
                    client,
                    property_row=property_row,
                    max_tasks_per_property=max_tasks_per_property,
                )
                return (info, rows, pages_scanned)
            except Exception as exc:
                property_errors.append(
                    {
                        "property_id": info.get("property_id"),
                        "property_name": info.get("property_name"),
                        "details": str(exc),
                    }
                )
                return (info, [], 0)

    scans = await asyncio.gather(*(_scan_property(property_row) for property_row in properties))
    today = _today()
    triaged: List[Dict[str, Any]] = []
    tasks_scanned = 0
    task_pages_scanned = 0
    for info, rows, pages_scanned in scans:
        task_pages_scanned += pages_scanned
        tasks_scanned += len(rows)
        for row in rows:
            triaged_row = _triage_task_row(row, property_info=info, users_by_id=users_by_id, today=today)
            if triaged_row is not None:
                triaged.append(triaged_row)

    triaged.sort(
        key=lambda task: (
            0 if task.get("urgency_bucket") == "act_now" else 1 if task.get("urgency_bucket") == "next_up" else 2,
            -int(task.get("urgency_score") or 0),
            str(task.get("scheduled_date") or ""),
            str(task.get("property_name") or ""),
            int(task.get("id") or 0),
        )
    )
    shortlisted = triaged[:limit]

    if include_comments and shortlisted:
        comment_results = await asyncio.gather(
            *(asyncio.to_thread(client.get_task_comments, int(task["id"])) for task in shortlisted),
            return_exceptions=True,
        )
        for task, comments_result in zip(shortlisted, comment_results):
            if isinstance(comments_result, Exception):
                task["comments_error"] = str(comments_result)
                continue
            if not isinstance(comments_result, list):
                task["comments_error"] = "unexpected comment response shape"
                continue
            task["comments_count"] = len(comments_result)
            if comments_result:
                latest_comment = max(
                    (comment for comment in comments_result if isinstance(comment, dict)),
                    key=_comment_sort_key,
                    default=None,
                )
                if isinstance(latest_comment, dict):
                    preview = _comment_text(latest_comment)
                    if preview:
                        task["latest_comment"] = preview

    groups = _build_triage_groups(shortlisted)
    recommended_now = [task for task in shortlisted if task.get("needs_attention_now")][: min(10, len(shortlisted))]
    for task in shortlisted:
        task.pop("raw", None)
    for task in recommended_now:
        task.pop("raw", None)

    payload = {
        "data": shortlisted,
        "count": len(shortlisted),
        "recommended_now": recommended_now,
        "groups": groups,
        "scan_summary": {
            "properties_scanned": len(properties),
            "property_pages_scanned": property_pages_scanned,
            "task_pages_scanned": task_pages_scanned,
            "tasks_scanned": tasks_scanned,
            "open_tasks_considered": len(triaged),
            "properties_with_errors": property_errors,
        },
        "detail_level": "summary",
        "include_comments": include_comments,
    }
    return structured_result(with_response_meta(payload, tool="breezeway_triage_tasks"))


@mcp.tool(structured_output=False)
async def breezeway_get_task_comments(task_id: int) -> CallToolResult:
    """Fetch the comment thread for a single Breezeway task (read/detail).

    Use when:
        You already know the exact task id and need the discussion/history for that one task.

    Args:
        task_id: Breezeway task id.

    Returns:
        ``{"task_id", "data", "count", "detail_level": "full"}`` where ``data`` is the comment list.

    Notes:
        Prefer ``breezeway_list_tasks(include_comments=True)`` when you already need the task rows and only want
        to enrich the returned page with comments. Prefer this tool when you are drilling into one task thread.

    Errors:
        ``{"error": "validation_error", ...}`` when ``task_id`` is invalid.
        ``{"error": "upstream_failed", ...}`` when Breezeway rejects the id or the API is unavailable.

    Example:
        ``breezeway_get_task_comments(task_id=12345)``
    """
    task_id = coerce_int(task_id, default=0, minimum=1)
    if task_id < 1:
        return structured_result(
            tool_error("validation_error", details="task_id must be >= 1", cause="validation", retryable=False)
        )

    client = _get_client()
    try:
        comments = await asyncio.to_thread(client.get_task_comments, task_id)
    except Exception as e:
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify the task_id and Breezeway API status.",
            )
        )
    if not isinstance(comments, list):
        return structured_result(
            tool_error(
                "unexpected_shape",
                details="unexpected response shape",
                cause="upstream_error",
                retryable=False,
            )
        )
    return structured_result(
        with_response_meta(
            {"task_id": task_id, "data": comments, "count": len(comments), "detail_level": "full"},
            tool="breezeway_get_task_comments",
        )
    )


@mcp.tool(structured_output=False)
async def breezeway_move_task(
    task_id: int,
    action: Literal["close", "approve", "reopen"],
) -> CallToolResult:
    """Move a Breezeway task through a supported workflow transition (write).

    Use when:
        The user explicitly wants to change task state without patching other task fields.

    Args:
        task_id: Breezeway task id.
        action: One of ``close``, ``approve``, or ``reopen``.

    Returns:
        ``{"task_id", "action", "result"?}`` where ``result`` is the upstream response body when Breezeway returns one.

    Notes:
        This is a state-changing write tool and should require ``permissions.write`` for server id ``breezeway`` in mcp-proxy.
        Use this instead of ``breezeway_update_task`` when the change is specifically a workflow/status transition.

    Errors:
        ``{"error": "validation_error", ...}`` when ``task_id`` or ``action`` is invalid.
        ``{"error": "upstream_failed", ...}`` when the transition is not allowed or Breezeway rejects the request.

    Example:
        ``breezeway_move_task(task_id=12345, action="close")``
    """
    task_id = coerce_int(task_id, default=0, minimum=1)
    action = (action or "").strip().lower()
    if task_id < 1:
        return structured_result(
            tool_error("validation_error", details="task_id must be >= 1", cause="validation", retryable=False)
        )
    if action not in _BREEZEWAY_TASK_MOVE_ACTIONS:
        return structured_result(
            tool_error(
                "validation_error",
                details=f"action must be one of {', '.join(_BREEZEWAY_TASK_MOVE_ACTIONS)}",
                cause="validation",
                retryable=False,
            )
        )

    client = _get_client()
    try:
        body = await asyncio.to_thread(client.move_task, task_id, action)
    except Exception as e:
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify the task state transition is allowed in Breezeway.",
            )
        )

    payload: Dict[str, Any] = {"task_id": task_id, "action": action}
    if body is not None:
        payload["result"] = body
    return structured_result(with_response_meta(payload, tool="breezeway_move_task"))


@mcp.tool(structured_output=False)
async def breezeway_update_task(
    task_id: int,
    updates: BreezewayTaskUpdatePayload,
) -> CallToolResult:
    """Patch a Breezeway task with the raw update payload expected by the upstream API (write).

    Use when:
        The user explicitly wants to edit task fields such as assignee, dates, instructions, priority, or other
        task attributes supported by Breezeway's ``PATCH /task/{id}`` endpoint.

    Args:
        task_id: Breezeway task id.
        updates: Non-empty PATCH object. Common documented fields are exposed in the tool schema and extra
            upstream keys are still allowed for forward compatibility.

    Returns:
        ``{"task_id", "result"?}`` where ``result`` is the upstream response body when Breezeway returns one.

    Notes:
        This is a write tool and should require ``permissions.write`` for server id ``breezeway`` in mcp-proxy.
        Pass only fields that Breezeway documents for the task update endpoint. Common fields exposed here are
        ``name``, ``type_department``, ``type_priority``, ``description``, ``template_id``, ``scheduled_date``,
        ``scheduled_time``, ``assignments``, ``tags``, ``subdepartment_id``, ``rate_paid``, ``rate_type``,
        and ``requested_by``. When the desired change is purely workflow state, prefer ``breezeway_move_task``
        over sending a broad patch.

    Errors:
        ``{"error": "validation_error", ...}`` when ``task_id`` is invalid or ``updates`` is empty/non-object.
        ``{"error": "upstream_failed", ...}`` when Breezeway rejects the PATCH body.

    Example:
        ``breezeway_update_task(task_id=12345, updates={"assignee_id": 77})``
    """
    task_id = coerce_int(task_id, default=0, minimum=1)
    update_dict = updates.model_dump(exclude_none=True)
    if task_id < 1:
        return structured_result(
            tool_error("validation_error", details="task_id must be >= 1", cause="validation", retryable=False)
        )
    if not update_dict:
        return structured_result(
            tool_error(
                "validation_error",
                details="updates must be a non-empty object",
                cause="validation",
                retryable=False,
            )
        )

    client = _get_client()
    try:
        body = await asyncio.to_thread(client.update_task, task_id, update_dict)
    except Exception as e:
        return structured_result(
            tool_error(
                "upstream_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify the PATCH payload matches Breezeway's update-task schema.",
            )
        )

    payload: Dict[str, Any] = {"task_id": task_id}
    if body is not None:
        payload["result"] = body
    return structured_result(with_response_meta(payload, tool="breezeway_update_task"))


@mcp.tool(structured_output=False)
async def breezeway_get_reservation_by_external_id(
    external_reservation_id: str,
    allow_multiple: bool = False,
) -> CallToolResult:
    """Fetch a Breezeway reservation by external reservation id (read/detail).

    **Use when:**
        You have an external PMS/booking id.

    **Args:**
        external_reservation_id: External id string.
        allow_multiple: Forwarded to client.

    **Returns:**
        Passthrough reservation JSON object.

    **Notes:**
        ``allow_multiple`` is passed through to the API client when the backend supports it.

    **Errors:**
        ``{"error", "details"}`` on failure.

    **Example:**
        ``breezeway_get_reservation_by_external_id(external_reservation_id="abc")``
    """
    client = _get_client()
    body = await asyncio.to_thread(client.get_reservation_by_external_id, external_reservation_id, allow_multiple)
    return structured_result(body if isinstance(body, dict) else {"reservation": body, "detail_level": "full"})


# ---------------------------------------------------------------------------
# Machine-readable tool metadata (for conformance checks and agent introspection)
# ---------------------------------------------------------------------------

TOOL_METADATA: dict = {
    "breezeway_list_properties_page": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "breezeway_find_property_by_name_or_external_id": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "resolver": True,
        "primary_param": "query",
    },
    "breezeway_get_property_summary": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "property_id",
    },
    "breezeway_list_users": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "breezeway_list_tasks": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "breezeway_triage_tasks": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "breezeway_get_task_comments": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "primary_param": "task_id",
    },
    "breezeway_move_task": {
        "read_only": False,
        "mutation": True,
        "idempotent": False,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "primary_param": "task_id",
    },
    "breezeway_update_task": {
        "read_only": False,
        "mutation": True,
        "idempotent": False,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "primary_param": "task_id",
    },
    "breezeway_get_reservation_by_external_id": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "external_reservation_id",
    },
}
