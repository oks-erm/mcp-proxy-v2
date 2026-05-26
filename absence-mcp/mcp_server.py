"""MCP tools for absence.io (read-only): users and absences via API v2."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from absence_client import list_absences_payload, list_users_payload, post_json
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.coerce_numeric import coerce_int
from mcp_platform.detail_level import effective_detail_level, parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result

logger = logging.getLogger(__name__)


def _upstream_error_result(result: Dict[str, Any]) -> CallToolResult:
    msg = result.get("error")
    details = msg if isinstance(msg, str) else str(msg)
    extra = result.get("details")
    if extra is not None and extra != details:
        details = f"{details}; {extra}"
    code = "upstream_error"
    status_code = result.get("status_code")
    return structured_result(
        tool_error(
            code,
            details=details,
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check the absence.io API status.",
            **({"status_code": status_code} if status_code else {}),
        )
    )


def _compact_absence_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "start": str(row.get("start") or ""),
        "end": str(row.get("end") or ""),
        "assignedToId": str(row.get("assignedToId") or "") or None,
        "status": row.get("status"),
        "reasonId": str(row.get("reasonId") or "") if row.get("reasonId") is not None else None,
        "daysCount": row.get("daysCount"),
    }


# absence.io: status 3 = inactive (portal skips these when listing "active" users)
_INACTIVE_STATUS = 3
# User scan: absence.io max page size 100; cap pages to avoid unbounded work on huge orgs
_MAX_USER_PAGES = 50
# Absences per user/day: rarely >100; cap total rows returned compactly
_MAX_ABSENCE_ROWS = 500

mcp = FastMCP(
    "absence",
    instructions=(
        "absence.io read-only API v2 (Hawk). "
        "For “Is person X absent on date Y?” use absence_is_user_absent_on_date (one call with user_name or user_id). "
        "Use absence_find_user_by_name to resolve a human name to user ids without listing all users. "
        "Use absence_list_absences for cross-user or bulk absence queries; overlap on a calendar day: "
        "start <= YYYY-MM-DDT00:00:00.000Z and end >= that same instant (or pass overlap_date=YYYY-MM-DD). "
        "absence_list_absences relations are opt-in (omit or [] = no expanded assignedTo). "
        "Pagination: skip/limit; empty data[] is success. "
        'sort_by on absence_list_absences is Mongo-style {"field":1|-1} or string field (asc); or sort_field+sort_direction (1|-1). '
        "absence_get_user_absences sorts by start asc. omit_user_details strips assignedTo blobs when relations expand them. "
        "Tools stay absence_* (mcp-proxy id absence)."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


def _normalize_search_text(s: str) -> str:
    return " ".join(s.strip().split()).lower()


def _parse_yyyy_mm_dd(value: str) -> Optional[str]:
    raw = value.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _date_to_overlap_iso(date_yyyy_mm_dd: str) -> str:
    return f"{date_yyyy_mm_dd}T00:00:00.000Z"


def _user_full_display_name(u: Dict[str, Any]) -> str:
    first = str(u.get("firstName") or "").strip()
    last = str(u.get("lastName") or "").strip()
    name_field = str(u.get("name") or "").strip()
    if name_field:
        return name_field
    return f"{first} {last}".strip()


def _compact_user_public(u: Dict[str, Any]) -> Dict[str, Any]:
    email = u.get("email")
    return {
        "id": str(u.get("id") or ""),
        "firstName": str(u.get("firstName") or ""),
        "lastName": str(u.get("lastName") or ""),
        "name": str(u.get("name") or ""),
        "email": email if email is not None else None,
        "status": int(u["status"]) if u.get("status") is not None else 0,
    }


def _user_name_tier_match(u: Dict[str, Any], needle_norm: str) -> Optional[int]:
    """Return 0 = exact full-name match, 1 = partial match, None = no match."""
    first = str(u.get("firstName") or "").strip()
    last = str(u.get("lastName") or "").strip()
    name_field = str(u.get("name") or "").strip()
    full = _user_full_display_name(u)
    full_n = _normalize_search_text(full)
    fn = _normalize_search_text(first)
    ln = _normalize_search_text(last)
    nn = _normalize_search_text(name_field) if name_field else ""

    if full_n == needle_norm:
        return 0
    partial = (
        (needle_norm in full_n)
        or (bool(fn) and needle_norm in fn)
        or (bool(ln) and needle_norm in ln)
        or (bool(nn) and needle_norm in nn)
    )
    return 1 if partial else None


def _scan_users_collect_matches(
    needle_norm: str,
    exclude_inactive: bool,
    result_limit: int,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Paginate POST /users; return up to result_limit compact users matching needle, sorted exact-first.
    Second return is post_json error dict if a page failed.
    """
    matches: List[Tuple[int, str, Dict[str, Any]]] = []
    skip = 0
    page = 100
    for _ in range(_MAX_USER_PAGES):
        result = post_json("users", list_users_payload(skip, page))
        if "error" in result and "data" not in result:
            return [], result
        rows = result.get("data")
        if not isinstance(rows, list):
            break
        for u in rows:
            if not isinstance(u, dict):
                continue
            if exclude_inactive and u.get("status") == _INACTIVE_STATUS:
                continue
            tier = _user_name_tier_match(u, needle_norm)
            if tier is None:
                continue
            compact = _compact_user_public(u)
            uid = compact["id"]
            display = (
                _user_full_display_name(u) or compact["name"] or f'{compact["firstName"]} {compact["lastName"]}'.strip()
            )
            sort_name = display or uid
            matches.append((tier, sort_name, compact))
        if len(rows) < page:
            break
        skip += page
        if len(matches) >= result_limit:
            break

    matches.sort(key=lambda t: (t[0], t[1], t[2]["id"]))
    return [t[2] for t in matches[:result_limit]], None


def _scan_users_collect_all_matches(
    needle_norm: str,
    exclude_inactive: bool,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Paginate all users (up to cap); return every name match for disambiguation."""
    matches: List[Tuple[int, str, Dict[str, Any]]] = []
    skip = 0
    page = 100
    for _ in range(_MAX_USER_PAGES):
        result = post_json("users", list_users_payload(skip, page))
        if "error" in result and "data" not in result:
            return [], result
        rows = result.get("data")
        if not isinstance(rows, list):
            break
        for u in rows:
            if not isinstance(u, dict):
                continue
            if exclude_inactive and u.get("status") == _INACTIVE_STATUS:
                continue
            tier = _user_name_tier_match(u, needle_norm)
            if tier is None:
                continue
            compact = _compact_user_public(u)
            uid = compact["id"]
            display = (
                _user_full_display_name(u) or compact["name"] or f'{compact["firstName"]} {compact["lastName"]}'.strip()
            )
            sort_name = display or uid
            matches.append((tier, sort_name, compact))
        if len(rows) < page:
            break
        skip += page

    matches.sort(key=lambda t: (t[0], t[1], t[2]["id"]))
    return [t[2] for t in matches], None


def _scan_users_find_by_id(
    user_id: str,
    exclude_inactive: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
    """
    Returns (compact_user, note, error). note set when not found or inactive; error on transport failure.
    """
    uid = user_id.strip()
    skip = 0
    page = 100
    for _ in range(_MAX_USER_PAGES):
        result = post_json("users", list_users_payload(skip, page))
        if "error" in result and "data" not in result:
            return None, None, result
        rows = result.get("data")
        if not isinstance(rows, list):
            break
        for u in rows:
            if not isinstance(u, dict):
                continue
            if str(u.get("id") or "") != uid:
                continue
            if exclude_inactive and u.get("status") == _INACTIVE_STATUS:
                return None, "User is inactive (status 3) and exclude_inactive is true.", None
            return _compact_user_public(u), None, None
        if len(rows) < page:
            break
        skip += page
    return None, f'No user found for user_id "{uid}" (scanned up to {_MAX_USER_PAGES * page} users).', None


def _fetch_absences_overlap_user(
    user_id: str, overlap_iso: str
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    filt: Dict[str, Any] = {
        "assignedToId": user_id,
        "start": {"$lte": overlap_iso},
        "end": {"$gte": overlap_iso},
    }
    all_rows: List[Dict[str, Any]] = []
    skip = 0
    page = 100
    while len(all_rows) < _MAX_ABSENCE_ROWS:
        body = list_absences_payload(skip, page, filt, None, {"start": 1})
        result = post_json("absences", body)
        if "error" in result and "data" not in result:
            return [], result
        chunk = result.get("data")
        if not isinstance(chunk, list) or not chunk:
            break
        for row in chunk:
            if isinstance(row, dict):
                all_rows.append(row)
        if len(chunk) < page:
            break
        skip += page
    return all_rows[:_MAX_ABSENCE_ROWS], None


def _absence_status_compact(st: Any) -> Any:
    if st is None or isinstance(st, bool):
        return None
    if isinstance(st, int):
        return st
    if isinstance(st, float):
        return int(st)
    return None


def _compact_matching_absence(row: Dict[str, Any]) -> Dict[str, Any]:
    st = row.get("status")
    reason = row.get("reasonId")
    dc = row.get("daysCount")
    return {
        "id": str(row.get("id") or ""),
        "start": str(row.get("start") or ""),
        "end": str(row.get("end") or ""),
        "status": _absence_status_compact(st),
        "daysCount": dc,
        "reasonId": str(reason) if reason is not None else None,
    }


@mcp.tool(structured_output=False)
async def absence_list_users(
    skip: int = 0,
    limit: int = 100,
    exclude_inactive: bool = True,
    compact: Optional[bool] = None,
    detail_level: str = "compact",
) -> CallToolResult:
    """List Absence users (read/list).

    **Use when:**
        You need absence.io user records to obtain valid ``user_id`` values, or to browse users.
        For resolving a human name without paging manually, prefer ``absence_find_user_by_name``.

    **Args:**
        skip: Number of users to skip (offset pagination).
        limit: Maximum users to return (clamped 1–100 by payload builder).
        exclude_inactive: If true, drops users whose ``status`` is ``3`` (inactive) from this page after fetch.
            ``count`` reflects the filtered list length; ``totalCount`` from the API may still describe the
            unfiltered total.
        compact: Optional legacy flag; false forces ``detail_level=full``.
        detail_level: ``compact`` (default) or ``full`` for each user row shape.

    **Returns:**
        JSON from ``POST /users`` with ``skip``, ``limit``, ``data``, typically ``totalCount``, and ``count``
        set to ``len(data)`` when ``data`` is a list.

    **Notes:**
        Pagination: offset ``skip`` + ``limit``.

    **Errors:**
        ``{"error", "details"}`` (and optional ``status_code``) on upstream failure.

    **Example:**
        ``absence_list_users(skip=0, limit=50, detail_level=\"compact\")``
    """
    dl = effective_detail_level(detail_level, default="compact", compact=compact)
    skip = coerce_int(skip, default=0, minimum=0)
    limit = coerce_int(limit, default=100, minimum=1, maximum=100)
    body = list_users_payload(skip, limit)
    result = await asyncio.to_thread(post_json, "users", body)
    if "error" in result and "data" not in result:
        return _upstream_error_result(result)
    data = result.get("data")
    if not isinstance(data, list):
        return structured_result(
            {**result, "detail_level": dl} if isinstance(result, dict) else {"data": result, "detail_level": dl}
        )
    if exclude_inactive:
        data = [u for u in data if not isinstance(u, dict) or u.get("status") != _INACTIVE_STATUS]
    if dl != "full":
        data = [_compact_user_public(u) for u in data if isinstance(u, dict)]
    total = result.get("totalCount") if isinstance(result, dict) else None
    _has_more = bool(skip + limit < total) if isinstance(total, int) else None
    return structured_result(
        with_response_meta(
            {**result, "data": data, "count": len(data), "detail_level": dl},
            tool="absence_list_users",
            pagination=build_pagination_meta(
                limit=limit,
                offset=skip,
                has_more=_has_more,
                next_offset=skip + limit if _has_more else None,
                total_count=total if isinstance(total, int) else None,
            ),
        )
    )


def _normalize_sort_by(sort_by: Any) -> Optional[Dict[str, int]]:
    """
    absence.io POST /absences uses Mongo-style sortBy: { field: 1|-1 } (see portal absence_tasks sortBy).
    Accept a plain field name string as shorthand for { field: 1 }.
    """
    if sort_by is None:
        return None
    if isinstance(sort_by, str):
        field = sort_by.strip()
        return {field: 1} if field else None
    if isinstance(sort_by, dict):
        if not sort_by:
            return None
        return cast(Dict[str, int], {str(k): int(v) for k, v in sort_by.items()})
    return None


def _resolve_absence_sort(
    sort_by: Any,
    sort_field: Optional[str],
    sort_direction: int,
) -> tuple[Optional[Dict[str, int]], Optional[str]]:
    """Prefer sort_field + sort_direction for strict MCP clients; sort_by is advanced / legacy."""
    if sort_direction not in (1, -1):
        return None, "sort_direction must be 1 (ascending) or -1 (descending)."
    by_sort_by = _normalize_sort_by(sort_by)
    by_field: Optional[Dict[str, int]] = None
    if sort_field is not None and str(sort_field).strip():
        by_field = {str(sort_field).strip(): sort_direction}
    if by_sort_by and by_field and by_sort_by != by_field:
        return None, "sort_field/sort_direction conflicts with sort_by; use only one style."
    return by_field or by_sort_by, None


def _omit_assigned_to_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, list):
        return result
    new_rows: List[Any] = []
    for row in data:
        if isinstance(row, dict) and "assignedTo" in row:
            new_rows.append({k: v for k, v in row.items() if k != "assignedTo"})
        else:
            new_rows.append(row)
    return {**result, "data": new_rows}


def _resolve_overlap_instant(
    overlap_date: Optional[str],
    overlap_date_iso: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Return (overlap_iso or None, error_message or None)."""
    od = overlap_date.strip() if overlap_date else None
    oi = overlap_date_iso.strip() if overlap_date_iso else None
    if not od and not oi:
        return None, None
    if od and oi:
        parsed = _parse_yyyy_mm_dd(od)
        if not parsed:
            return None, "overlap_date must be YYYY-MM-DD when set."
        expected = _date_to_overlap_iso(parsed)
        if oi != expected:
            return (
                None,
                "overlap_date and overlap_date_iso disagree; set only one, or use the same UTC midnight (YYYY-MM-DDT00:00:00.000Z).",
            )
        return expected, None
    if od:
        parsed = _parse_yyyy_mm_dd(od)
        if not parsed:
            return None, "overlap_date must be YYYY-MM-DD."
        return _date_to_overlap_iso(parsed), None
    return oi, None


@mcp.tool(structured_output=False)
async def absence_find_user_by_name(
    name: str,
    exclude_inactive: bool = True,
    limit: int = 10,
) -> CallToolResult:
    """Resolve absence.io ``user_id`` values from a person's name (compact resolver).

    Prefer this before ``absence_is_user_absent_on_date`` when you only have a name; avoids hand-scanning ``absence_list_users`` pages.

    Input: ``name`` string, optional ``exclude_inactive`` and ``limit``.

    **Use when:**
        You need ``user_id`` values from a person's name without calling ``absence_list_users`` on every page
        and matching manually.

    **Args:**
        name: Required non-empty search string. Matching is case-insensitive; whitespace is normalized.
        exclude_inactive: If true (default), users with ``status`` 3 (inactive) are excluded, same as portal-style
            active listings.
        limit: Max matches to return (default 10). Scanning stops once this many matches are found or users run out.
            At most 50 pages of users (5000 rows) are scanned — beyond that, refine the search string.

    Matching:
        - Exact match on normalized full display name: ``name`` field if non-empty, else ``firstName`` + ``lastName``.
        - Else partial (substring) match on that full string, or on ``firstName``, ``lastName``, or ``name`` individually.
        Exact matches sort before partial matches.

    **Returns:**
        ``{"count": int, "data": [{"id", "firstName", "lastName", "name", "email"|null, "status"}, ...]}``

    Empty:
        ``count`` 0 and ``data`` [] if no user matches (success, not an error).

    **Errors:**
        Empty/whitespace ``name`` → ``{"error": "..."}``. Upstream failures → ``post_json`` error shape.

    **Example:**
        ``absence_find_user_by_name(name="Simão Campos", limit=5)``
    """
    if not name or not str(name).strip():
        return structured_result(
            tool_error(
                "validation_error",
                details="name must be a non-empty string.",
                cause="validation",
                retryable=False,
            )
        )
    lim = max(1, min(int(limit), 100))
    needle_norm = _normalize_search_text(str(name))
    if not needle_norm:
        return structured_result(
            tool_error(
                "validation_error",
                details="name must contain at least one non-space character.",
                cause="validation",
                retryable=False,
            )
        )

    matches, err = await asyncio.to_thread(_scan_users_collect_matches, needle_norm, exclude_inactive, lim)
    if err:
        return _upstream_error_result(err)
    return structured_result(
        with_response_meta(
            {"count": len(matches), "data": matches, "detail_level": "compact"},
            tool="absence_find_user_by_name",
        )
    )


@mcp.tool(structured_output=False)
async def absence_is_user_absent_on_date(
    date: str,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    exclude_inactive: bool = True,
) -> CallToolResult:
    """Answer whether one user has any absence overlapping a calendar date in one call (compact).

    Prefer this over ``absence_list_absences`` for a single yes/no on one date; use list tools when you need full windows.

    Input: ``date`` as ``YYYY-MM-DD`` plus either ``user_id`` or ``user_name`` (id wins if both are set).

    **Use when:**
        Answering e.g. “Is Simão Campos on vacation on 2026-04-09?” in one call. Pass ``user_name`` and ``date``,
        or ``user_id`` and ``date``. If both ``user_id`` and are ``user_name`` set, ``user_id`` wins (name is ignored).

    **Args:**
        date: Required calendar date ``YYYY-MM-DD``.
        user_id: Optional absence.io user id (hex string). Takes precedence over ``user_name`` when both are set.
        user_name: Optional human name; resolved with the same rules as ``absence_find_user_by_name`` (same
            ``exclude_inactive`` default). If multiple users match, the tool does **not** guess (see below).
        exclude_inactive: If true (default), inactive users (status 3) are excluded from name resolution; when
            ``user_id`` is used, the user must exist and not be inactive or you get a ``note`` and no absence query.

    Overlap semantics:
        Absent on ``date`` iff there is at least one absence with ``start <= dateT00:00:00.000Z`` and
        ``end >= dateT00:00:00.000Z`` (inclusive), matching the portal / ``absence_list_absences`` overlap filter.
        This tool returns **API-visible** overlapping absences. It does **not** apply the portal Slack job’s
        extra ``reasonId`` exclusions; those only affect internal reporting labels.

    **Returns:**
        ``{
          "matched_user": {"id": str, "name": str} | null,
          "ambiguous_matches": [{"id", "name"}, ...],
          "date": "YYYY-MM-DD",
          "is_absent": bool,
          "matching_absences": [{"id", "start", "end", "status", "daysCount", "reasonId"|null}, ...],
          "note": str | optional
        }``
        ``status`` on each absence is the absence.io record field when present (may be null).

    Empty name resolution:
        No matches → ``matched_user`` null, ``ambiguous_matches`` [], ``is_absent`` false, ``matching_absences`` [].

    Ambiguous name:
        Two or more matches → ``ambiguous_matches`` lists candidates (id + display name), ``matched_user`` null,
        ``is_absent`` false, ``matching_absences`` []. Call again with ``user_id`` after disambiguation.

    Note:
        If ``user_id`` cannot be resolved or user is inactive (when ``exclude_inactive``), ``note`` explains why;
        ``is_absent`` is false and ``matching_absences`` is [].

    **Errors:**
        Invalid ``date`` format, or neither ``user_id`` nor ``user_name`` provided → ``{"error": "..."}``.
        Upstream failure during absence fetch → ``post_json`` error shape.

    **Example:**
        ``absence_is_user_absent_on_date(user_name="Simão Campos", date="2026-04-09")``
    """
    parsed_date = _parse_yyyy_mm_dd(date)
    if not parsed_date:
        return structured_result(
            tool_error("validation_error", details="date must be YYYY-MM-DD.", cause="validation", retryable=False)
        )

    uid_in = user_id.strip() if user_id else None
    uname_in = user_name.strip() if user_name else None
    if not uid_in and not uname_in:
        return structured_result(
            tool_error(
                "validation_error",
                details="Provide user_id or user_name.",
                cause="validation",
                retryable=False,
            )
        )

    overlap_iso = _date_to_overlap_iso(parsed_date)
    base_out: Dict[str, Any] = {
        "matched_user": None,
        "ambiguous_matches": [],
        "date": parsed_date,
        "is_absent": False,
        "matching_absences": [],
    }

    resolved_compact: Optional[Dict[str, Any]] = None

    if uid_in:
        cuser, note_lookup, err = await asyncio.to_thread(_scan_users_find_by_id, uid_in, exclude_inactive)
        if err:
            return _upstream_error_result(err)
        if note_lookup:
            base_out["note"] = note_lookup
            return structured_result({**base_out, "detail_level": "compact"})
        resolved_compact = cuser
    else:
        assert uname_in is not None
        needle_norm = _normalize_search_text(uname_in)
        if not needle_norm:
            return structured_result(
                tool_error(
                    "validation_error",
                    details="user_name must contain at least one non-space character.",
                    cause="validation",
                    retryable=False,
                )
            )
        matches, err = await asyncio.to_thread(_scan_users_collect_all_matches, needle_norm, exclude_inactive)
        if err:
            return _upstream_error_result(err)
        if len(matches) == 0:
            return structured_result({**base_out, "detail_level": "compact"})
        if len(matches) > 1:
            amb = []
            for m in matches:
                disp = (
                    _user_full_display_name(m)
                    or m.get("name")
                    or f'{m.get("firstName", "")} {m.get("lastName", "")}'.strip()
                )
                amb.append({"id": m["id"], "name": disp or m["id"]})
            base_out["ambiguous_matches"] = amb
            base_out["note"] = "Multiple users matched user_name; pass user_id to disambiguate."
            return structured_result({**base_out, "detail_level": "compact"})
        resolved_compact = matches[0]

    assert resolved_compact is not None
    uid_final = resolved_compact["id"]
    display = (
        _user_full_display_name(resolved_compact)
        or resolved_compact.get("name")
        or f'{resolved_compact.get("firstName", "")} {resolved_compact.get("lastName", "")}'.strip()
        or uid_final
    )
    base_out["matched_user"] = {"id": uid_final, "name": display}

    rows, err2 = await asyncio.to_thread(_fetch_absences_overlap_user, uid_final, overlap_iso)
    if err2:
        return _upstream_error_result(err2)
    base_out["matching_absences"] = [_compact_matching_absence(r) for r in rows if isinstance(r, dict)]
    base_out["is_absent"] = len(base_out["matching_absences"]) > 0
    return structured_result({**base_out, "detail_level": "compact"})


@mcp.tool(structured_output=False)
async def absence_list_absences(
    skip: int = 0,
    limit: int = 100,
    assigned_to_id: Optional[str] = None,
    user_id: Optional[str] = None,
    overlap_date: Optional[str] = None,
    overlap_date_iso: Optional[str] = None,
    relations: Optional[List[str]] = None,
    sort_field: Optional[str] = None,
    sort_direction: int = 1,
    sort_by: Any = None,
    filter_extra: Optional[Dict[str, Any]] = None,
    omit_user_details: bool = True,
    detail_level: str = "compact",
) -> CallToolResult:
    """Page through absence records with assignee, overlap-day, and extra filters (list / read).

    Prefer ``absence_is_user_absent_on_date`` for a single user/date check; use this for reporting spans or many users.

    Typical input: ``skip`` / ``limit``, optional ``user_id`` / ``assigned_to_id``, ``overlap_date`` or ``overlap_date_iso``.

    **Use when:**
        You need absences across users, optionally filtered by assignee, by a specific calendar day (overlap), or by
        extra Mongo-style filters. For “is this person absent on this date?” prefer ``absence_is_user_absent_on_date``.

    **Args:**
        skip: Records to skip (offset pagination).
        limit: Max records to return (clamped 1–100).
        assigned_to_id: Optional absence.io user ID; restricts to that assignee (same as ``assignedToId`` filter).
        user_id: Optional alias for ``assigned_to_id``. If both are set, they must be identical.
        overlap_date: Optional calendar date ``YYYY-MM-DD``; equivalent to overlap at
            ``{date}T00:00:00.000Z`` (see below). Do not set together with a conflicting ``overlap_date_iso``.
        overlap_date_iso: Optional ISO instant. When set, the filter requires ``start <= overlap_date_iso`` and
            ``end >= overlap_date_iso`` (Mongo-style ``$lte`` / ``$gte``). For a full calendar day in UTC, use
            midnight Z, e.g. ``2026-04-09T00:00:00.000Z``. **Overlap pattern:** absent on that instant iff
            ``assigned`` period overlaps it (start on or before, end on or after).
        relations: Optional list sent as ``relations`` on ``POST /absences``. **Opt-in:** default is no relations
            (no expanded ``assignedTo`` blobs). Pass ``["assignedToId"]`` to expand assignee; use ``omit_user_details=false``
            to keep expanded user objects.
        sort_field: Field name to sort by (e.g. ``start``). Use with ``sort_direction``; has no effect if empty.
        sort_direction: ``1`` ascending or ``-1`` descending; must be used with non-empty ``sort_field``.
            ``sort_direction`` alone does not sort.
        sort_by: Advanced sort: Mongo-style ``{\"start\": 1}`` or ``{\"start\": -1}``, or a string field name
            (shorthand for ascending). Do not combine with a conflicting ``sort_field``/``sort_direction``.
        filter_extra: Optional filter dict merged into the request filter (Mongo-style operators).
        omit_user_details: When true (default), removes embedded ``assignedTo`` from each row after fetch;
            ``assignedToId`` remains on the record when the API returns it.
        detail_level: ``compact`` (default) returns slim absence rows; ``full`` keeps API row shape (after omit_user_details).

    **Returns:**
        Passthrough JSON from ``POST /absences`` with ``skip``, ``limit``, ``data``, ``totalCount`` when present,
        and ``count`` = ``len(data)``.

    **Notes:**
        Pagination: offset ``skip`` + ``limit``.

        If neither ``sort_field`` nor ``sort_by`` yields a sort, ``sortBy`` is omitted and absence.io default
        ordering applies. Empty page: ``count`` 0 and ``data`` is ``[]``.

    **Errors:**
        Conflicting ``user_id``/``assigned_to_id`` or overlap params return ``{\"error\": \"...\"}``.
        Invalid sort combination returns ``{\"error\": \"...\"}``. Other failures use ``post_json`` error shape.

    **Example:**
        ``absence_list_absences(overlap_date=\"2026-04-09\", limit=50)``
    """
    dl = parse_detail_level(detail_level, default="compact")
    skip = coerce_int(skip, default=0, minimum=0)
    limit = coerce_int(limit, default=100, minimum=1, maximum=100)
    aid = assigned_to_id.strip() if assigned_to_id else None
    uid = user_id.strip() if user_id else None
    if aid and uid and aid != uid:
        return structured_result(
            tool_error(
                "validation_error",
                details="assigned_to_id and user_id differ; pass only one or use the same id.",
                cause="validation",
                retryable=False,
            )
        )
    effective_assignee = aid or uid

    overlap_iso, oerr = _resolve_overlap_instant(overlap_date, overlap_date_iso)
    if oerr:
        return structured_result(tool_error("validation_error", details=oerr, cause="validation", retryable=False))

    filt: Dict[str, Any] = {}
    if effective_assignee:
        filt["assignedToId"] = effective_assignee
    if overlap_iso:
        filt["start"] = {"$lte": overlap_iso}
        filt["end"] = {"$gte": overlap_iso}
    if filter_extra:
        for k, v in filter_extra.items():
            filt[k] = v
    resolved_sort, sort_err = _resolve_absence_sort(sort_by, sort_field, sort_direction)
    if sort_err:
        return structured_result(tool_error("validation_error", details=sort_err, cause="validation", retryable=False))
    rels = relations
    body = list_absences_payload(skip, limit, filt if filt else None, rels, resolved_sort)
    result = await asyncio.to_thread(post_json, "absences", body)
    if "error" in result and "data" not in result:
        return _upstream_error_result(result)
    if omit_user_details:
        result = _omit_assigned_to_from_result(result)
    data = result.get("data")
    if isinstance(data, list):
        if dl == "full":
            out_data = data
        else:
            out_data = [_compact_absence_row(r) if isinstance(r, dict) else r for r in data]
        total = result.get("totalCount") if isinstance(result, dict) else None
        _has_more = bool(skip + limit < total) if isinstance(total, int) else None
        return structured_result(
            with_response_meta(
                {**result, "data": out_data, "count": len(out_data), "detail_level": dl},
                tool="absence_list_absences",
                pagination=build_pagination_meta(
                    limit=limit,
                    offset=skip,
                    has_more=_has_more,
                    next_offset=skip + limit if _has_more else None,
                    total_count=total if isinstance(total, int) else None,
                ),
            )
        )
    return structured_result(
        with_response_meta(
            {**result, "detail_level": dl} if isinstance(result, dict) else {"data": result, "detail_level": dl},
            tool="absence_list_absences",
            pagination=build_pagination_meta(limit=limit, offset=skip),
        )
    )


@mcp.tool(structured_output=False)
async def absence_get_user_absences(
    user_id: str,
    skip: int = 0,
    limit: int = 100,
    end_on_or_after: Optional[str] = None,
    omit_user_details: bool = True,
    detail_level: str = "compact",
) -> CallToolResult:
    """List absences for a single user (read/list).

    **Use when:**
        You already know the Absence ``user_id`` and want only that user's absences. For a single date check,
        prefer ``absence_is_user_absent_on_date``. Prefer this over ``absence_list_absences`` when listing periods
        for one known user id.

    **Args:**
        user_id: Absence.io user ID (same id as in user list / ``assignedToId`` on absences).
        skip: Records to skip (offset pagination).
        limit: Max records to return (clamped 1–100).
        end_on_or_after: Optional ISO timestamp. When set, adds ``end >= end_on_or_after``. Omit to return all
            matching absences for the user (no implicit \"today\" filter unless you pass it here).
        omit_user_details: Default true strips ``assignedTo`` from each row; false keeps API-expanded user blobs.
        detail_level: ``compact`` (default) or ``full`` for row shape (see absence_list_absences).

    **Returns:**
        Same envelope as ``absence_list_absences``: ``skip``, ``limit``, ``data`` (absence records), ``totalCount``
        when present, and ``count`` = ``len(data)``.

    **Notes:**
        Pagination: offset ``skip`` + ``limit``. Results are sorted by ``start`` ascending (fixed). ``relations`` always
        includes assignee expansion on the API; summary rows default to id-only via ``omit_user_details``.
        Prefer this over ``absence_list_absences`` when scoped to one known user for period listing. Empty page:
        ``count`` 0, ``data`` ``[]``.

    **Errors:**
        ``{"error", "details"}`` on upstream failure.

    **Example:**
        ``absence_get_user_absences(user_id=\"...\", skip=0, limit=50)``
    """
    skip = coerce_int(skip, default=0, minimum=0)
    limit = coerce_int(limit, default=100, minimum=1, maximum=100)
    filt: Dict[str, Any] = {"assignedToId": user_id}
    if end_on_or_after:
        filt["end"] = {"$gte": end_on_or_after}
    body = list_absences_payload(
        skip,
        limit,
        filt,
        relations=["assignedToId"],
        sort_by={"start": 1},
    )
    dl = parse_detail_level(detail_level, default="compact")
    result = await asyncio.to_thread(post_json, "absences", body)
    if "error" in result and "data" not in result:
        return _upstream_error_result(result)
    if omit_user_details:
        result = _omit_assigned_to_from_result(result)
    data = result.get("data")
    if isinstance(data, list):
        if dl == "full":
            out_data = data
        else:
            out_data = [_compact_absence_row(r) if isinstance(r, dict) else r for r in data]
        total = result.get("totalCount") if isinstance(result, dict) else None
        _has_more = bool(skip + limit < total) if isinstance(total, int) else None
        return structured_result(
            with_response_meta(
                {**result, "data": out_data, "count": len(out_data), "detail_level": dl},
                tool="absence_get_user_absences",
                pagination=build_pagination_meta(
                    limit=limit,
                    offset=skip,
                    has_more=_has_more,
                    next_offset=skip + limit if _has_more else None,
                    total_count=total if isinstance(total, int) else None,
                ),
            )
        )
    return structured_result(
        with_response_meta(
            {**result, "detail_level": dl} if isinstance(result, dict) else {"data": result, "detail_level": dl},
            tool="absence_get_user_absences",
            pagination=build_pagination_meta(limit=limit, offset=skip),
        )
    )
