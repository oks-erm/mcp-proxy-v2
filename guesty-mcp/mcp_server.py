"""MCP server for Guesty, exposing Guesty API features as tools."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from guesty_client import GuestyService
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.coerce_numeric import coerce_int, coerce_optional_int
from mcp_platform.detail_level import effective_detail_level, parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result

logger = logging.getLogger(__name__)

_service: Optional[GuestyService] = None


def _get_service() -> GuestyService:
    global _service
    if _service is None:
        _service = GuestyService()
    return _service


_LISTING_COMPACT_SCALAR_KEYS: tuple[str, ...] = (
    "_id",
    "title",
    "nickname",
    "propertyType",
    "roomType",
    "accommodates",
    "bedrooms",
    "bathrooms",
    "isListed",
)


def _listing_compact_thumbnail(row: Dict[str, Any]) -> Optional[str]:
    pic = row.get("picture")
    if isinstance(pic, dict):
        t = pic.get("thumbnail")
        if t:
            return str(t)
    pictures = row.get("pictures")
    if isinstance(pictures, list):
        for p in pictures:
            if isinstance(p, dict) and p.get("thumbnail"):
                return str(p["thumbnail"])
    return None


def _compact_listing_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Whitelist compact shape for list/search listing rows (ids, summary fields, light nested).
    Omits keys not present on the source row (except nested objects built when partial data exists).
    """
    out: Dict[str, Any] = {}
    for key in _LISTING_COMPACT_SCALAR_KEYS:
        if key in row:
            out[key] = row[key]
    addr = row.get("address")
    if isinstance(addr, dict):
        sub: Dict[str, Any] = {}
        if "city" in addr:
            sub["city"] = addr["city"]
        if "country" in addr:
            sub["country"] = addr["country"]
        if sub:
            out["address"] = sub
    cs = row.get("cleaningStatus")
    if isinstance(cs, dict) and "value" in cs:
        out["cleaningStatus"] = {"value": cs["value"]}
    thumb = _listing_compact_thumbnail(row)
    if thumb is not None:
        out["picture"] = {"thumbnail": thumb}
    return out


def _compact_reservation_row(row: Dict[str, Any]) -> Dict[str, Any]:
    g = row.get("guest")
    guest_out = None
    if isinstance(g, dict):
        guest_out = {
            k: g.get(k) for k in ("fullName", "firstName", "lastName", "email", "phone") if g.get(k) is not None
        }
        if not guest_out:
            guest_out = None
    out: Dict[str, Any] = {}
    for k in (
        "_id",
        "confirmationCode",
        "listingId",
        "status",
        "checkIn",
        "checkOut",
        "nightsCount",
        "source",
        "integration",
    ):
        if k in row:
            out[k] = row[k]
    if guest_out:
        out["guest"] = guest_out
    return out


def _compact_reservations_response(data: Dict[str, Any], detail_level: str) -> Dict[str, Any]:
    results = data.get("results")
    if not isinstance(results, list):
        return {**data, "detail_level": detail_level}
    out = dict(data)
    out["results"] = [_compact_reservation_row(r) for r in results if isinstance(r, dict)]
    out["detail_level"] = detail_level
    return out


def _guesty_offset_pagination(
    result: Any,
    limit: Optional[int],
    skip: int,
    fetch_all: bool = False,
) -> Dict[str, Any]:
    """Build normalized meta.pagination for Guesty offset-based list/search tools."""
    total = result.get("count") if isinstance(result, dict) else None
    # Guesty's ``count`` is the total across all pages. Derive page size from the results list.
    results = result.get("results") if isinstance(result, dict) else None
    page_size = len(results) if isinstance(results, list) else 0
    if fetch_all:
        # fetch_all exhausts all pages — no continuation available
        return build_pagination_meta(
            limit=limit,
            offset=0,
            has_more=False,
            total_count=total if isinstance(total, int) else None,
        )
    eff_limit = limit
    eff_skip = skip
    _has_more = bool(eff_skip + page_size < total) if isinstance(total, int) and eff_limit is not None else None
    return build_pagination_meta(
        limit=eff_limit,
        offset=eff_skip,
        has_more=_has_more,
        next_offset=eff_skip + page_size if _has_more else None,
        total_count=total if isinstance(total, int) else None,
    )


def _compact_listings_response(data: Dict[str, Any], detail_level: str = "compact") -> Dict[str, Any]:
    results = data.get("results")
    if not isinstance(results, list):
        return {"data": [], "detail_level": detail_level}
    rows = [_compact_listing_row(r) for r in results if isinstance(r, dict)]
    return {"data": rows, "detail_level": detail_level}


def _guesty_listings_full_with_data_key(result: Dict[str, Any]) -> Dict[str, Any]:
    """For full/raw mode: add top-level ``data`` to the Guesty response without stripping native keys."""
    out = dict(result)
    results = out.get("results")
    if isinstance(results, list):
        out["data"] = results
    return out


def _listing_address_text(addr: Any) -> str:
    if not isinstance(addr, dict):
        return ""
    parts: List[str] = []
    for k in ("full", "city", "country", "street", "neighborhood"):
        v = addr.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " ".join(parts)


def _listing_match_tier(row: Dict[str, Any], needle: str) -> Optional[tuple[int, int]]:
    """Rank tuple (tier, tie_breaker); lower tier wins. None = no match."""
    n = needle.casefold().strip()
    if not n:
        return None
    title = str(row.get("title") or "")
    nick = str(row.get("nickname") or "")
    addr_t = _listing_address_text(row.get("address"))
    tc, nc, ac = title.casefold(), nick.casefold(), addr_t.casefold()
    if title and tc == n:
        return (0, len(title))
    if nick and nc == n:
        return (1, len(nick))
    if title and n in tc:
        return (2, len(title))
    if nick and n in nc:
        return (3, len(nick))
    if addr_t and n in ac:
        return (4, len(addr_t))
    return None


def _find_listing_output_row(row: Dict[str, Any]) -> Dict[str, Any]:
    addr = row.get("address") if isinstance(row.get("address"), dict) else {}
    city = addr.get("city") if isinstance(addr, dict) else None
    country = addr.get("country") if isinstance(addr, dict) else None
    il = row.get("isListed")
    is_listed: Optional[bool]
    if isinstance(il, bool):
        is_listed = il
    elif isinstance(il, (int, float)):
        is_listed = bool(il)
    elif il is None:
        is_listed = None
    else:
        is_listed = bool(il)
    return {
        "id": str(row.get("_id") or ""),
        "title": str(row.get("title") or ""),
        "nickname": row.get("nickname") if row.get("nickname") is not None else None,
        "city": str(city) if city is not None else None,
        "country": str(country) if country is not None else None,
        "isListed": is_listed,
    }


def _reservation_resolver_row(row: Dict[str, Any]) -> Dict[str, Any]:
    guest = row.get("guest") if isinstance(row.get("guest"), dict) else {}
    guest_name: Optional[str] = None
    if guest:
        guest_name = guest.get("fullName") or guest.get("full_name")
        if not guest_name:
            fn = str(guest.get("firstName") or "")
            ln = str(guest.get("lastName") or "")
            guest_name = f"{fn} {ln}".strip() or None
    listing = row.get("listing") if isinstance(row.get("listing"), dict) else {}
    listing_title = listing.get("title") if listing else None
    lid = row.get("listingId")
    return {
        "id": str(row.get("_id") or ""),
        "confirmationCode": row.get("confirmationCode") if row.get("confirmationCode") is not None else None,
        "guestName": guest_name,
        "listingId": str(lid) if lid is not None else None,
        "listingTitle": str(listing_title) if listing_title is not None else None,
        "checkIn": row.get("checkIn") if row.get("checkIn") is not None else None,
        "checkOut": row.get("checkOut") if row.get("checkOut") is not None else None,
        "status": str(row["status"]) if row.get("status") is not None else None,
    }


def _reservation_matches_guest_and_dates(
    row: Dict[str, Any],
    guest_name_filter: Optional[str],
    check_in: Optional[str],
    check_out: Optional[str],
) -> bool:
    """Post-filter: substring guest name (case-insensitive) and exact YYYY-MM-DD for check-in/out when provided."""
    if guest_name_filter and guest_name_filter.strip():
        gn = guest_name_filter.strip().casefold()
        g = row.get("guest") if isinstance(row.get("guest"), dict) else {}
        blob = " ".join(
            str(g.get(k) or "") for k in ("fullName", "full_name", "firstName", "lastName", "email")
        ).casefold()
        if gn not in blob:
            return False
    cin_pref = (check_in or "").strip()[:10]
    if cin_pref:
        if _parse_iso_date(cin_pref) is None:
            return False
        c = row.get("checkIn")
        if not c or str(c)[:10] != cin_pref:
            return False
    cout_pref = (check_out or "").strip()[:10]
    if cout_pref:
        if _parse_iso_date(cout_pref) is None:
            return False
        c = row.get("checkOut")
        if not c or str(c)[:10] != cout_pref:
            return False
    return True


def _parse_iso_date(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _calendar_days_list(calendar_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(calendar_payload, dict):
        return []
    data = calendar_payload.get("data")
    if isinstance(data, dict):
        days = data.get("days")
        if isinstance(days, list):
            return [d for d in days if isinstance(d, dict)]
    days = calendar_payload.get("days")
    if isinstance(days, list):
        return [d for d in days if isinstance(d, dict)]
    return []


def _day_blocks_stay(day: Dict[str, Any]) -> bool:
    """True if day should block a stay night (booked / manual block / similar)."""
    blocks = day.get("blocks")
    if isinstance(blocks, dict):
        for key in ("b", "m", "bd", "sr", "o", "pt", "a", "abl", "bw"):
            if blocks.get(key):
                return True
    status = (day.get("status") or "").lower()
    if status in ("booked", "reserved", "unavailable"):
        return True
    allot = day.get("allotment")
    if isinstance(allot, (int, float)) and allot <= 0:
        return True
    return False


def _day_available_for_stay_night(day: Dict[str, Any]) -> bool:
    """Availability for occupying this calendar night (see Guesty calendar + allotment note in API docs)."""
    if _day_blocks_stay(day):
        return False
    allot = day.get("allotment")
    if isinstance(allot, (int, float)):
        return allot > 0
    status = (day.get("status") or "").lower()
    return status == "available"


mcp = FastMCP(
    "guesty",
    instructions=(
        "You have access to Guesty (vacation rental PMS) data: reservations, listings, guests, "
        "owners, reviews, webhooks, custom fields, and listing calendar. Resolvers: find_listing (count/data), "
        "find_reservation (count/data), get_listing_summary, is_listing_available_on_dates. "
        "Also: get_reservations, search_reservations, get_listings, search_listings, get_guest, search_guests, "
        "get_property_logs, get_listing_calendar, search_reviews, search_owners. "
        "Behind mcp-proxy id 'guesty' names are guesty_*. "
        "Prefer search_* tools over get_* when you know structured filters; use find_* for human text. "
        "get_listings and search_listings default to compact (whitelist summary rows); set compact=false for full docs. "
        "fetch_all=True follows every page — higher cost and latency. "
        "Webhooks: create_webhook / update_webhook; list events via guesty_webhook_events resource."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


# -- Reservations -----------------------------------------------------------


@mcp.tool(structured_output=False)
async def get_reservations(
    limit: Optional[int] = None,
    skip: Optional[int] = None,
    sort: Optional[str] = None,
    fields: Optional[str] = None,
    detail_level: str = "compact",
) -> CallToolResult:
    """List reservations (read/list).

    **Use when:**
        You need a page of reservations with optional Guesty query parameters.

    **Args:**
        limit, skip, sort, fields: Forwarded to Guesty list API when set.

    **Returns:**
        Guesty list JSON; compact modes trim heavy fields. Empty ``results`` is success.

    **Notes:**
        Pagination: offset ``skip`` + ``limit``. Prefer ``search_reservations`` when you need structured filters.
        ``detail_level`` ``full`` keeps nested payloads; default masks listing rows.

    **Errors:**
        ``{"error", "details"}`` when Guesty returns no data or the request fails.

    **Example:**
        ``get_reservations(limit=10, skip=0, detail_level=\"compact\")``
    """
    lim_coerced = coerce_optional_int(limit, minimum=0)
    skip_coerced = coerce_optional_int(skip, minimum=0)
    service = _get_service()
    params: Dict[str, Any] = {}
    if lim_coerced is not None:
        params["limit"] = lim_coerced
    if skip_coerced is not None:
        params["skip"] = skip_coerced
    if sort is not None:
        params["sort"] = sort
    if fields is not None:
        params["fields"] = fields
    result = await asyncio.to_thread(service.get_reservations, **params)
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    dl = parse_detail_level(detail_level, default="compact")
    pagination = _guesty_offset_pagination(result, lim_coerced, skip_coerced or 0)
    if dl != "full" and isinstance(result, dict):
        body = _compact_reservations_response(result, dl)
        return structured_result(
            with_response_meta(
                body, tool="guesty_get_reservations", pagination=pagination or None, data_from="results"
            ),
        )
    full_body = (
        {**result, "detail_level": "full"} if isinstance(result, dict) else {"data": result, "detail_level": "full"}
    )
    return structured_result(
        with_response_meta(
            full_body, tool="guesty_get_reservations", pagination=pagination or None, data_from="results"
        ),
    )


@mcp.tool(structured_output=False)
async def search_reservations(
    filters: Optional[List[Dict[str, Any]]] = None,
    fields: Optional[str] = None,
    sort: str = "_id",
    limit: int = 25,
    skip: int = 0,
    fetch_all: bool = False,
    detail_level: str = "compact",
) -> CallToolResult:
    """Return Guesty reservation rows matching structured filter objects (search / read).

    Prefer this over ``find_reservation`` when you already have Guesty filter JSON (listing id, status, dates); prefer ``find_reservation`` for human confirmation codes, guest names, or listing hints.

    Typical input: ``filters`` as a list of ``{field, operator, value}`` objects plus ``limit`` / ``skip`` pagination.

    **Use when:**
        You need reservation lookup by structured conditions (listing, status, dates, etc.).

    **Args:**
        filters: Optional list of filter objects, e.g.
            ``[{\"field\": \"listingId\", \"operator\": \"eq\", \"value\": \"...\"}]``.
        fields: Optional comma-separated fields to return.
        sort: Guesty sort expression (default ``_id``).
        limit: Max results per page (capped in the client).
        skip: Offset for pagination.
        fetch_all: When true, fetches all pages. **Warning:** higher latency and load — use only when the full set
            is required; prefer smaller paged requests for routine agent use.

    **Returns:**
        Guesty-style dict with ``results``, ``count``, ``limit``, and ``skip`` (or aggregated equivalents when
        ``fetch_all``). Successful empty search → ``results: []`` — not a generic upstream failure.

    **Notes:**
        **Prefer this over list/get when you know the search condition.** A missing or failed HTTP response is
        distinct from an empty ``results`` list.

    **Errors:**
        ``{"error", "details"}`` when Guesty returns no data or the request fails.

    **Example:**
        ``search_reservations(filters=[{\"field\": \"listingId\", \"operator\": \"eq\", \"value\": \"...\"}], limit=10)``
    """
    limit = coerce_int(limit, default=25, minimum=0)
    skip = coerce_int(skip, default=0, minimum=0)
    service = _get_service()
    result = await asyncio.to_thread(
        service.search_reservations,
        filters=filters,
        fields=fields,
        sort=sort,
        limit=limit,
        skip=skip,
        fetch_all=fetch_all,
    )
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    dl = parse_detail_level(detail_level, default="compact")
    pagination = _guesty_offset_pagination(result, limit, skip, fetch_all=fetch_all)
    if dl != "full" and isinstance(result, dict):
        body = _compact_reservations_response(result, dl)
        return structured_result(
            with_response_meta(body, tool="guesty_search_reservations", pagination=pagination, data_from="results")
        )
    full_body = (
        {**result, "detail_level": "full"} if isinstance(result, dict) else {"data": result, "detail_level": "full"}
    )
    return structured_result(
        with_response_meta(full_body, tool="guesty_search_reservations", pagination=pagination, data_from="results"),
    )


@mcp.tool(structured_output=False)
async def find_reservation(
    confirmation_code: Optional[str] = None,
    guest_name: Optional[str] = None,
    listing_query: Optional[str] = None,
    check_in: Optional[str] = None,
    check_out: Optional[str] = None,
    limit: int = 10,
) -> CallToolResult:
    """Resolve reservations from human clues (confirmation code, guest name, listing hint, stay dates).

    Prefer this over ``search_reservations`` when the user did not supply structured Guesty filters — it builds the search for you.

    Pass any of ``confirmation_code``, ``guest_name``, ``listing_query``, ``check_in``, ``check_out`` (at least one required).

    Use when:
        You have a confirmation code, guest name fragment, listing name hint, and/or exact stay dates — without
        crafting Guesty filter JSON manually. Prefer this over ``search_reservations`` for human-entered identifiers.

    Args:
        confirmation_code: Exact confirmation code when known.
        guest_name: Case-insensitive partial match against guest full name / email fields (post-filter).
        listing_query: Resolves one or more listing ids via ``search_listings`` (same human text as ``find_listing``);
            reservations for every resolved listing are merged (no single-listing guess).
        check_in, check_out: When set, calendar dates ``YYYY-MM-DD`` matched exactly on ``checkIn`` / ``checkOut``.
        limit: Max rows returned after ranking (default 10).

    Returns:
        ``{"count", "data", "detail_level": "compact"}`` where ``data`` holds stable resolver fields only.
        Multiple matches are all returned up to ``limit`` (ambiguous stays are not collapsed).

    Notes:
        Empty ``data`` is normal when nothing matches. ``listing_query`` first resolves listings; if that yields no ids,
        ``count`` is zero. Widen dates or drop filters before assuming an API failure.

    Errors:
        ``{"error": "validation_error", "details": ...}`` when no identifier is provided or dates are invalid.
        ``{"error": "guesty_api_error", "details": ...}`` on transport failure.

    Example:
        ``find_reservation(confirmation_code="ABC", limit=5)``
    """
    has_cc = bool(confirmation_code and str(confirmation_code).strip())
    has_gn = bool(guest_name and str(guest_name).strip())
    has_lq = bool(listing_query and str(listing_query).strip())
    has_ci = bool(check_in and str(check_in).strip())
    has_co = bool(check_out and str(check_out).strip())
    if not (has_cc or has_gn or has_lq or has_ci or has_co):
        return structured_result(
            tool_error(
                "validation_error",
                details="Provide at least one of confirmation_code, guest_name, listing_query, check_in, check_out.",
                cause="validation",
                retryable=False,
            )
        )
    limit = coerce_int(limit, default=10, minimum=1, maximum=50)
    cin_s = str(check_in).strip()[:10] if check_in else ""
    cout_s = str(check_out).strip()[:10] if check_out else ""
    if has_ci and _parse_iso_date(cin_s) is None:
        return structured_result(
            tool_error(
                "validation_error",
                details="check_in must be YYYY-MM-DD when provided.",
                cause="validation",
                retryable=False,
            )
        )
    if has_co and _parse_iso_date(cout_s) is None:
        return structured_result(
            tool_error(
                "validation_error",
                details="check_out must be YYYY-MM-DD when provided.",
                cause="validation",
                retryable=False,
            )
        )

    service = _get_service()
    listing_ids: Optional[List[str]] = None
    if has_lq:
        lr = await asyncio.to_thread(
            service.search_listings, limit=25, skip=0, q=str(listing_query).strip(), sort="_id"
        )
        if lr is None:
            return structured_result(
                tool_error(
                    "guesty_api_error",
                    details="Guesty API request failed or returned no data",
                    cause="upstream_error",
                    retryable=True,
                    suggested_fix="Retry later or check Guesty API status.",
                )
            )
        listing_ids = [
            str(r["_id"]) for r in (lr.get("results") or []) if isinstance(r, dict) and r.get("_id") is not None
        ]
        if not listing_ids:
            return structured_result(
                with_response_meta(
                    {"count": 0, "data": [], "detail_level": "compact"},
                    tool="guesty_find_reservation",
                )
            )

    base_filters: List[Dict[str, Any]] = []
    if has_cc:
        base_filters.append({"operator": "$eq", "field": "confirmationCode", "value": str(confirmation_code).strip()})

    cap = min(100, max(limit * 5, 30))
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()

    gn_arg = str(guest_name).strip() if has_gn else None
    ci_arg = cin_s if has_ci else None
    co_arg = cout_s if has_co else None

    def _consume_result(res: Optional[Dict[str, Any]]) -> None:
        if not res or not isinstance(res, dict):
            return
        for r in res.get("results") or []:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("_id") or "")
            if rid and rid in seen:
                continue
            if not _reservation_matches_guest_and_dates(r, gn_arg, ci_arg, co_arg):
                continue
            seen.add(rid)
            merged.append(r)

    if listing_ids:
        api_failed = False
        for lid in listing_ids[:15]:
            flt = list(base_filters)
            flt.append({"operator": "$eq", "field": "listingId", "value": lid})
            res = await asyncio.to_thread(
                service.search_reservations,
                filters=flt,
                limit=cap,
                skip=0,
                sort="_id",
            )
            if res is None:
                api_failed = True
                break
            _consume_result(res)
        if api_failed and not merged:
            return structured_result(
                tool_error(
                    "guesty_api_error",
                    details="Guesty API request failed or returned no data",
                    cause="upstream_error",
                    retryable=True,
                    suggested_fix="Retry later or check Guesty API status.",
                )
            )
    else:
        if base_filters:
            res = await asyncio.to_thread(
                service.search_reservations,
                filters=base_filters,
                limit=cap,
                skip=0,
                sort="_id",
            )
            if res is None:
                return structured_result(
                    tool_error(
                        "guesty_api_error",
                        details="Guesty API request failed or returned no data",
                        cause="upstream_error",
                        retryable=True,
                        suggested_fix="Retry later or check Guesty API status.",
                    )
                )
            _consume_result(res)
        else:
            res = await asyncio.to_thread(service.get_reservations, limit=cap, skip=0)
            if res is None:
                return structured_result(
                    tool_error(
                        "guesty_api_error",
                        details="Guesty API request failed or returned no data",
                        cause="upstream_error",
                        retryable=True,
                        suggested_fix="Retry later or check Guesty API status.",
                    )
                )
            _consume_result(res)

    sliced = merged[: max(1, min(limit, 50))]
    return structured_result(
        with_response_meta(
            {
                "count": len(sliced),
                "data": [_reservation_resolver_row(dict(r)) for r in sliced],
                "detail_level": "compact",
            },
            tool="guesty_find_reservation",
        )
    )


# -- Listings ---------------------------------------------------------------


@mcp.tool(structured_output=False)
async def get_listings(
    limit: Optional[int] = None,
    skip: Optional[int] = None,
    sort: Optional[str] = None,
    compact: Optional[bool] = None,
    detail_level: str = "compact",
) -> CallToolResult:
    """List listings (read/list).

    **Use when:**
        You need listing inventory; default compact mode returns a whitelisted summary per row.

    **Args:**
        limit, skip, sort: Guesty list parameters.
        compact: When true (default), each ``results`` row includes only: ``_id``, ``title``, ``nickname``,
            ``address`` (``city``, ``country`` only when present), ``propertyType``, ``roomType``, ``accommodates``,
            ``bedrooms``, ``bathrooms``, ``isListed``, and when present ``cleaningStatus.value``, ``picture.thumbnail``
            (from ``picture`` or the first ``pictures[]`` entry with a thumbnail). False returns full API docs.

    **Returns:**
        ``{"data": [...], "detail_level", "meta": {...}}``.
        ``data`` is the authoritative list field.

    **Notes:**
        Pagination uses ``skip`` + ``limit``. Full documents may include operational or PII fields — use compact
        unless you need them. Prefer ``search_listings`` for filtered discovery.

    **Errors:**
        ``{"error", "details"}`` when Guesty returns no data or the request fails.

    **Example:**
        ``get_listings(limit=20, skip=0, detail_level=\"compact\")``
    """
    lim_coerced = coerce_optional_int(limit, minimum=0)
    skip_coerced = coerce_optional_int(skip, minimum=0)
    service = _get_service()
    params: Dict[str, Any] = {}
    if lim_coerced is not None:
        params["limit"] = lim_coerced
    if skip_coerced is not None:
        params["skip"] = skip_coerced
    if sort is not None:
        params["sort"] = sort
    result = await asyncio.to_thread(service.get_listings, **params)
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    dl = effective_detail_level(detail_level, default="compact", compact=compact)
    pagination = _guesty_offset_pagination(result, lim_coerced, skip_coerced or 0)
    if dl != "full" and isinstance(result, dict):
        body = _compact_listings_response(result, dl)
        return structured_result(with_response_meta(body, tool="guesty_get_listings", pagination=pagination or None))
    full_body = (
        {**_guesty_listings_full_with_data_key(result), "detail_level": "full"}
        if isinstance(result, dict)
        else {"data": result, "detail_level": "full"}
    )
    return structured_result(
        with_response_meta(full_body, tool="guesty_get_listings", pagination=pagination or None),
    )


@mcp.tool(structured_output=False)
async def search_listings(
    limit: int = 25,
    skip: int = 0,
    fetch_all: bool = False,
    sort: str = "_id",
    compact: Optional[bool] = None,
    detail_level: str = "compact",
) -> CallToolResult:
    """Browse or slice Guesty listings via the listing search API with sort and pagination (search / read).

    Prefer ``find_listing`` when the user gave a human property name; use this for paged scans or when you need API search semantics without name resolution.

    Typical input: ``limit``, ``skip``, ``sort``, optional ``fetch_all`` (expensive).

    **Use when:**
        You need listing retrieval with search/sort pagination rather than the plain list endpoint.

    **Args:**
        limit: Max results per page (capped in the client).
        skip: Offset.
        sort: Guesty sort expression (default ``_id``).
        fetch_all: When true, loads all pages. **Warning:** higher cost and latency — use only when you need every
            listing; prefer paged calls for interactive use.
        compact: When true (default), each ``results`` row uses the same whitelisted summary as ``get_listings``.
            Set false for full listing documents from the API.

    **Returns:**
        Response including ``results``, ``count``, ``limit``, and ``skip`` (Guesty/API shape). Empty search →
        ``results: []``. When compact, includes ``compact: true`` on the payload.

    **Notes:**
        **Prefer this over plain list/get when you know the search condition** (e.g. sorted slice or future
        filter extensions).

    **Errors:**
        ``{"error", "details"}`` on transport failure; empty ``results`` is still success.

    **Example:**
        ``search_listings(limit=10, skip=0, fetch_all=False)``
    """
    limit = coerce_int(limit, default=25, minimum=0)
    skip = coerce_int(skip, default=0, minimum=0)
    service = _get_service()
    result = await asyncio.to_thread(
        service.search_listings,
        limit=limit,
        skip=skip,
        fetch_all=fetch_all,
        sort=sort,
    )
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    dl = effective_detail_level(detail_level, default="compact", compact=compact)
    pagination = _guesty_offset_pagination(result, limit, skip, fetch_all=fetch_all)
    if dl != "full" and isinstance(result, dict):
        body = _compact_listings_response(result, dl)
        return structured_result(with_response_meta(body, tool="guesty_search_listings", pagination=pagination))
    full_body = (
        {**_guesty_listings_full_with_data_key(result), "detail_level": "full"}
        if isinstance(result, dict)
        else {"data": result, "detail_level": "full"}
    )
    return structured_result(with_response_meta(full_body, tool="guesty_search_listings", pagination=pagination))


@mcp.tool(structured_output=False)
async def find_listing(
    query: str,
    limit: int = 10,
    detail_level: str = "compact",
) -> CallToolResult:
    """Turn a human listing name, nickname, or address fragment into ranked Guesty listing candidates (resolver).

    Prefer this over ``guesty_search_listings`` for fuzzy name resolution; use ``search_listings`` for structured paging without ranking.

    Required: ``query`` string (never ``name`` / ``title``); proxied as ``guesty_find_listing``.

    IMPORTANT: The search parameter is named ``query`` — do not pass ``name``, ``title``, or ``listing_name``.
    Via mcp-proxy this tool is exposed as ``guesty_find_listing``.

    Use when:
        You know the human listing title or nickname and need stable ids — without interpreting raw
        ``guesty_search_listings`` pages yourself.

    Args:
        query: Required human text; matched case-insensitively for exact title/nickname first, then partial matches
            on title, nickname, and key address fields returned by Guesty.
        limit: Max listings to return after ranking (default 10).
        detail_level: Only ``compact`` is emitted for resolver rows; other values are accepted for forward-compat but
            output shape is unchanged.

    Returns:
        ``{"count", "data", "detail_level": "compact"}`` with rows: id, title, nickname, city, country, isListed.

    Notes:
        Prefer this over ``guesty_search_listings`` when you know the human listing name or nickname. Empty ``data``
        is success. Ambiguous matches return several rows — never auto-pick a single listing.

    Errors:
        ``{"error": "validation_error", "details": ...}`` when ``query`` is empty.
        ``{"error": "guesty_api_error", "details": ...}`` on transport failure.

    Example:
        ``find_listing(query="Ocean View", limit=5)``
    """
    raw_q = (query or "").strip()
    if not raw_q:
        return structured_result(
            tool_error("validation_error", details="query is required", cause="validation", retryable=False)
        )
    limit = coerce_int(limit, default=10, minimum=1, maximum=50)
    dl = parse_detail_level(detail_level, default="compact")
    service = _get_service()
    fetch_n = min(100, max(limit * 4, 20))
    result = await asyncio.to_thread(service.search_listings, limit=fetch_n, skip=0, q=raw_q, sort="_id")
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        rows = []
    ranked: List[Tuple[Tuple[int, int], Dict[str, Any]]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        tier = _listing_match_tier(r, raw_q)
        if tier is None:
            continue
        ranked.append((tier, r))
    ranked.sort(key=lambda x: (x[0][0], x[0][1]))
    out_rows = [_find_listing_output_row(dict(r)) for _, r in ranked[: max(1, min(limit, 50))]]
    eff_dl = "compact" if dl == "compact" else dl
    return structured_result(
        with_response_meta(
            {"count": len(out_rows), "data": out_rows, "detail_level": eff_dl},
            tool="guesty_find_listing",
        )
    )


@mcp.tool(structured_output=False)
async def get_listing_summary(
    listing_id: str,
) -> CallToolResult:
    """Return a compact, agent-safe listing summary for one exact Guesty ``listing_id`` (detail / read).

    Prefer this after ``find_listing`` or when an id is already known; avoid broad list/search calls just to enrich one id.

    Input: ``listing_id`` string (Guesty ``_id``).

    Use when:
        You already have the Guesty ``listing_id`` (from ``find_listing`` or reservation payloads).

    Args:
        listing_id: Guesty listing ``_id``.

    Returns:
        Summary fields with ``detail_level: "summary"`` (stable subset only).

    Notes:
        Fetches ``GET /listings/{id}``; avoids broad list/search cost.

    Errors:
        ``{"error": "validation_error", "details": ...}`` when id missing.
        ``{"error": "guesty_api_error", "details": ...}`` when the listing cannot be retrieved.

    Example:
        ``get_listing_summary(listing_id="6671a2b3c4d5e6f708090a0b")``
    """
    lid = (listing_id or "").strip()
    if not lid:
        return structured_result(
            tool_error("validation_error", details="listing_id is required", cause="validation", retryable=False)
        )
    service = _get_service()
    row = await asyncio.to_thread(service.get_listing, lid)
    if row is None or not isinstance(row, dict):
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or listing not found",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Verify listing_id or retry later.",
            )
        )
    addr = row.get("address") if isinstance(row.get("address"), dict) else {}
    city = addr.get("city") if isinstance(addr, dict) else None
    country = addr.get("country") if isinstance(addr, dict) else None
    cs = row.get("cleaningStatus")
    cleaning: Optional[str] = None
    if isinstance(cs, dict) and cs.get("value") is not None:
        cleaning = str(cs["value"])
    il = row.get("isListed")
    is_listed: Optional[bool]
    if isinstance(il, bool):
        is_listed = il
    elif isinstance(il, (int, float)):
        is_listed = bool(il)
    elif il is None:
        is_listed = None
    else:
        is_listed = bool(il)
    return structured_result(
        {
            "id": str(row.get("_id") or lid),
            "title": str(row.get("title") or ""),
            "nickname": row.get("nickname") if row.get("nickname") is not None else None,
            "propertyType": str(row["propertyType"]) if row.get("propertyType") is not None else None,
            "roomType": str(row["roomType"]) if row.get("roomType") is not None else None,
            "accommodates": int(row["accommodates"]) if isinstance(row.get("accommodates"), (int, float)) else None,
            "bedrooms": int(row["bedrooms"]) if isinstance(row.get("bedrooms"), (int, float)) else None,
            "bathrooms": float(row["bathrooms"]) if isinstance(row.get("bathrooms"), (int, float)) else None,
            "isListed": is_listed,
            "city": str(city) if city is not None else None,
            "country": str(country) if country is not None else None,
            "cleaningStatus": cleaning,
            "detail_level": "summary",
        }
    )


@mcp.tool(structured_output=False)
async def is_listing_available_on_dates(
    listing_id: str,
    check_in: str,
    check_out: str,
    include_allotment: bool = True,
    ignore_inactive_child_allotment: bool = False,
    ignore_unlisted_child_allotment: bool = False,
) -> CallToolResult:
    """Answer whether a listing is available for every night of a stay (read/summary).

    **Use when:**
        You need a yes/no availability answer for a stay window without fetching the full calendar yourself.

    **Args:**
        listing_id: Guesty listing id.
        check_in, check_out: ``YYYY-MM-DD``; stay nights are ``check_in`` .. ``check_out - 1 day``.
        include_allotment, ignore_*: Forwarded to ``get_listing_calendar``.

    **Returns:**
        ``available`` (bool), ``nights_checked``, ``unavailable_dates`` (with brief reasons).

    **Notes:**
        Each **night** runs from ``check_in`` through the day **before** ``check_out`` (checkout exclusive). A night is
        unavailable when blocks show booking/manual holds or ``allotment <= 0``; otherwise ``status`` should be
        ``available`` when allotment is absent. See Guesty calendar block documentation.

    **Errors:**
        ``{"error", "details"}`` for bad ids/dates or calendar API failure.

    **Example:**
        ``is_listing_available_on_dates(listing_id=\"...\", check_in=\"2026-06-01\", check_out=\"2026-06-05\")``
    """
    lid = (listing_id or "").strip()
    if not lid:
        return structured_result(
            tool_error("validation_error", details="listing_id is required", cause="validation", retryable=False)
        )
    d_in = _parse_iso_date(check_in or "")
    d_out = _parse_iso_date(check_out or "")
    if not d_in or not d_out:
        return structured_result(
            tool_error(
                "validation_error",
                details="check_in and check_out must be YYYY-MM-DD",
                cause="validation",
                retryable=False,
            )
        )
    if d_out <= d_in:
        return structured_result(
            tool_error(
                "validation_error",
                details="check_out must be after check_in",
                cause="validation",
                retryable=False,
            )
        )

    service = _get_service()
    cal = await asyncio.to_thread(
        service.get_listing_calendar,
        listing_id=lid,
        start_date=check_in.strip()[:10],
        end_date=check_out.strip()[:10],
        include_allotment=include_allotment,
        ignore_inactive_child_allotment=ignore_inactive_child_allotment,
        ignore_unlisted_child_allotment=ignore_unlisted_child_allotment,
    )
    if cal is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty calendar request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify dates and listing id.",
            )
        )

    days = _calendar_days_list(cal if isinstance(cal, dict) else {})
    by_date = {str(d.get("date", ""))[:10]: d for d in days}

    unavailable: List[Dict[str, Any]] = []
    cur = d_in
    last_night = d_out - timedelta(days=1)
    nights = 0
    while cur <= last_night:
        nights += 1
        key = cur.strftime("%Y-%m-%d")
        day = by_date.get(key)
        if day is None:
            unavailable.append({"date": key, "reason": "missing_day_in_calendar_response"})
        elif not _day_available_for_stay_night(day):
            unavailable.append(
                {
                    "date": key,
                    "reason": "blocked_or_unavailable",
                    "status": day.get("status"),
                    "blocks": day.get("blocks"),
                    "allotment": day.get("allotment"),
                }
            )
        cur += timedelta(days=1)

    return structured_result(
        {
            "available": len(unavailable) == 0,
            "listing_id": lid,
            "check_in": check_in.strip()[:10],
            "check_out": check_out.strip()[:10],
            "nights_checked": nights,
            "unavailable_dates": unavailable,
            "detail_level": "compact",
        }
    )


# -- Reviews ----------------------------------------------------------------


@mcp.tool(structured_output=False)
async def search_reviews(
    limit: int = 25,
    skip: int = 0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    fetch_all: bool = False,
) -> CallToolResult:
    """Search Guesty reviews (read/search).

    **Use when:**
        You need review records, optionally scoped by date range.

    **Args:**
        start_date: Optional ISO date (sent as ``startDate`` to Guesty); inclusive/exclusive bounds follow Guesty's API.
        end_date: Optional ISO date (sent as ``endDate``).
        limit: Max reviews per page.
        skip: Offset.
        fetch_all: When true, paginates through all matching reviews. **Warning:** can be slow and heavy — enable only
            when you need the complete review set.

    **Returns:**
        Normalized dict with ``results`` (list), ``count``, ``limit``, and ``skip`` where applicable. Empty query →
        ``results: []``.

    **Notes:**
        **Prefer this over ad-hoc listing when you know the search condition** (e.g. date window).

    **Errors:**
        ``{"error", "details"}`` on transport failure; empty ``results`` is success.

    **Example:**
        ``search_reviews(start_date=\"2026-01-01\", end_date=\"2026-01-31\", limit=10)``
    """
    limit = coerce_int(limit, default=25, minimum=0)
    skip = coerce_int(skip, default=0, minimum=0)
    service = _get_service()
    result = await asyncio.to_thread(
        service.search_reviews,
        limit=limit,
        skip=skip,
        start_date=start_date,
        end_date=end_date,
        fetch_all=fetch_all,
    )
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    pag = {"limit": limit, "skip": skip, "fetch_all": fetch_all}
    if isinstance(result, dict):
        return structured_result(
            with_response_meta(result, tool="guesty_search_reviews", pagination=pag, data_from="results")
        )
    return structured_result(
        with_response_meta(
            {"data": result, "detail_level": "compact"},
            tool="guesty_search_reviews",
            pagination=pag,
        )
    )


# -- Owners -----------------------------------------------------------------


@mcp.tool(structured_output=False)
async def search_owners(
    limit: int = 25,
    skip: int = 0,
    fetch_all: bool = False,
) -> CallToolResult:
    """Search owners (read/search).

    **Use when:**
        You need owner records from Guesty owner search.

    **Args:**
        limit, skip, fetch_all: Pagination; ``fetch_all`` auto-paginates.

    **Returns:**
        Dict result, or if API returns a list, ``{\"items\": [...], \"count\": n}``.

    **Notes:**
        Pagination uses ``skip``/``limit`` or ``fetch_all`` for full scans.

    **Errors:**
        ``{"error", "details"}`` when Guesty returns no data or the request fails.

    **Example:**
        ``search_owners(limit=10, skip=0)``
    """
    limit = coerce_int(limit, default=25, minimum=0)
    skip = coerce_int(skip, default=0, minimum=0)
    service = _get_service()
    result = await asyncio.to_thread(
        service.search_owners,
        limit=limit,
        skip=skip,
        fetch_all=fetch_all,
    )
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    pag = {"limit": limit, "skip": skip, "fetch_all": fetch_all}
    # API may return a list; normalize to dict so MCP output schema (result: object) is satisfied
    if isinstance(result, list):
        return structured_result(
            with_response_meta(
                {"items": result, "count": len(result), "detail_level": "compact"},
                tool="guesty_search_owners",
                pagination=pag,
                data_from="items",
            )
        )
    if isinstance(result, dict):
        _df = (
            "results"
            if isinstance(result.get("results"), list)
            else ("items" if isinstance(result.get("items"), list) else None)
        )
        return structured_result(with_response_meta(result, tool="guesty_search_owners", pagination=pag, data_from=_df))
    return structured_result(
        with_response_meta(
            {"data": result, "detail_level": "compact"},
            tool="guesty_search_owners",
            pagination=pag,
        )
    )


# -- Guests -----------------------------------------------------------------


@mcp.tool(structured_output=False)
async def get_guests(
    limit: Optional[int] = None,
    skip: Optional[int] = None,
) -> CallToolResult:
    """List guests (read/list).

    **Use when:**
        You need a page of guests.

    **Returns:**
        Guesty guest list JSON.

    **Notes:**
        Pagination: ``skip`` + ``limit``. Prefer ``search_guests`` for field-scoped guest queries.

    **Errors:**
        ``{"error", "details"}`` when Guesty returns no data or the request fails.

    **Example:**
        ``get_guests(limit=25, skip=0)``
    """
    lim_coerced = coerce_optional_int(limit, minimum=0)
    skip_coerced = coerce_optional_int(skip, minimum=0)
    service = _get_service()
    params: Dict[str, Any] = {}
    if lim_coerced is not None:
        params["limit"] = lim_coerced
    if skip_coerced is not None:
        params["skip"] = skip_coerced
    result = await asyncio.to_thread(service.get_guests, **params)
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    pag = {k: v for k, v in {"limit": lim_coerced, "skip": skip_coerced}.items() if v is not None}
    if isinstance(result, dict):
        return structured_result(
            with_response_meta(result, tool="guesty_get_guests", pagination=pag or None, data_from="results")
        )
    return structured_result(
        with_response_meta(
            {"data": result, "detail_level": "compact"},
            tool="guesty_get_guests",
            pagination=pag or None,
        )
    )


@mcp.tool(structured_output=False)
async def get_guest(guest_id: Optional[str] = None) -> CallToolResult:
    """Get one guest by id (read/detail).

    **Use when:**
        You already have the Guesty guest id.

    **Args:**
        guest_id: Guesty guest document id.

    **Returns:**
        Guest record payload.

    **Errors:**
        ``{"error", "details"}`` for missing id or not found.

    **Example:**
        ``get_guest(guest_id=\"507f1f77bcf86cd799439011\")``
    """
    if not guest_id or not str(guest_id).strip():
        return structured_result(
            tool_error("validation_error", details="guest_id is required", cause="validation", retryable=False)
        )
    service = _get_service()
    result = await asyncio.to_thread(service.get_guest, guest_id)
    if result is None:
        return structured_result(
            tool_error(
                "not_found",
                details="Guesty API request failed or guest not found",
                cause="not_found",
                retryable=False,
                suggested_fix="Verify guest_id exists in Guesty.",
            )
        )
    return structured_result(result if isinstance(result, dict) else {"guest": result, "detail_level": "full"})


@mcp.tool(structured_output=False)
async def search_guests(
    limit: int = 25,
    skip: int = 0,
    columns: Optional[str] = None,
    fetch_all: bool = False,
) -> CallToolResult:
    """Search guests (read/search).

    **Use when:**
        You need filtered guest rows or specific ``columns``.

    **Args:**
        columns: Optional comma/space-separated field projection string.
        limit, skip, fetch_all: Pagination.

    **Returns:**
        Guesty search dict.

    **Notes:**
        ``columns`` controls field projection when supported by the API.

    **Errors:**
        ``{"error", "details"}`` when Guesty returns no data or the request fails.

    **Example:**
        ``search_guests(columns=\"firstName lastName email\", limit=10)``
    """
    limit = coerce_int(limit, default=25, minimum=0)
    skip = coerce_int(skip, default=0, minimum=0)
    service = _get_service()
    kwargs: Dict[str, Any] = {"limit": limit, "skip": skip, "fetch_all": fetch_all}
    if columns is not None:
        kwargs["columns"] = columns
    result = await asyncio.to_thread(service.search_guests, **kwargs)
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    pag = {"limit": limit, "skip": skip, "fetch_all": fetch_all}
    if isinstance(result, dict):
        return structured_result(
            with_response_meta(result, tool="guesty_search_guests", pagination=pag, data_from="results")
        )
    return structured_result(
        with_response_meta(
            {"data": result, "detail_level": "compact"},
            tool="guesty_search_guests",
            pagination=pag,
        )
    )


# -- Property logs ----------------------------------------------------------


@mcp.tool(structured_output=False)
async def get_property_logs(
    listing_id: Optional[str] = None,
    limit: Optional[int] = None,
    skip: Optional[int] = None,
    sort: Optional[str] = None,
    fields: Optional[str] = None,
) -> CallToolResult:
    """Listing property change log (read/list).

    **Use when:**
        Auditing what changed on a listing or building an update timeline.

    **Args:**
        listing_id: Guesty listing ``_id`` (required).
        limit: Max log entries (API default / max 100).
        skip: Offset pagination.
        sort: Guesty sort string (e.g. ``-createdAt``).
        fields: Comma-separated projection for log entries.

    **Returns:**
        Guesty logs payload.

    **Notes:**
        Pagination: ``skip`` + ``limit`` (API max applies).

    **Errors:**
        ``{"error", "details"}`` for missing listing or API failure.

    **Example:**
        ``get_property_logs(listing_id=\"...\", limit=20, sort=\"-createdAt\")``
    """
    if not listing_id or not str(listing_id).strip():
        return structured_result(
            tool_error("validation_error", details="listing_id is required", cause="validation", retryable=False)
        )
    service = _get_service()
    limit_coerced = coerce_optional_int(limit, minimum=1, maximum=100)
    skip_coerced = coerce_optional_int(skip, minimum=0)
    params: Dict[str, Any] = {}
    if limit_coerced is not None:
        params["limit"] = limit_coerced
    if skip_coerced is not None:
        params["skip"] = skip_coerced
    if sort is not None:
        params["sort"] = sort
    if fields is not None:
        params["fields"] = fields
    result = await asyncio.to_thread(service.get_property_logs, listing_id, **params)
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    return structured_result(result if isinstance(result, dict) else {"logs": result, "detail_level": "compact"})


# -- Listing calendar ------------------------------------------------------


@mcp.tool(structured_output=False)
async def get_listing_calendar(
    listing_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    include_allotment: bool = True,
    ignore_inactive_child_allotment: bool = False,
    ignore_unlisted_child_allotment: bool = False,
) -> CallToolResult:
    """Listing availability / pricing calendar slice (read/detail).

    **Use when:**
        You need calendar or rate data for a date span on one listing.

    **Args:**
        listing_id: Guesty listing id (required).
        start_date, end_date: Inclusive range as ``YYYY-MM-DD`` (required).
        include_allotment, ignore_*_allotment: Forwarded boolean flags for Guesty calendar API.

    **Returns:**
        Calendar JSON from Guesty.

    **Notes:**
        ``start_date``/``end_date`` are inclusive ``YYYY-MM-DD`` per tool validation.

    **Errors:**
        ``{"error", "details"}`` for missing listing/dates or API failure.

    **Example:**
        ``get_listing_calendar(listing_id=\"...\", start_date=\"2026-01-01\", end_date=\"2026-01-31\")``
    """
    if not listing_id or not str(listing_id).strip():
        return structured_result(
            tool_error("validation_error", details="listing_id is required", cause="validation", retryable=False)
        )
    if not start_date or not str(start_date).strip():
        return structured_result(
            tool_error(
                "validation_error",
                details="start_date is required (YYYY-MM-DD)",
                cause="validation",
                retryable=False,
            )
        )
    if not end_date or not str(end_date).strip():
        return structured_result(
            tool_error(
                "validation_error",
                details="end_date is required (YYYY-MM-DD)",
                cause="validation",
                retryable=False,
            )
        )
    service = _get_service()
    result = await asyncio.to_thread(
        service.get_listing_calendar,
        listing_id=listing_id,
        start_date=start_date,
        end_date=end_date,
        include_allotment=include_allotment,
        ignore_inactive_child_allotment=ignore_inactive_child_allotment,
        ignore_unlisted_child_allotment=ignore_unlisted_child_allotment,
    )
    if result is None:
        return structured_result(
            tool_error(
                "guesty_api_error",
                details="Guesty API request failed or returned no data",
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Guesty API status.",
            )
        )
    return structured_result(result if isinstance(result, dict) else {"calendar": result, "detail_level": "full"})


# ---------------------------------------------------------------------------
# Machine-readable tool metadata
# ---------------------------------------------------------------------------

TOOL_METADATA: dict = {
    "guesty_get_reservations": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "guesty_search_reservations": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "guesty_get_listings": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "guesty_search_listings": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "guesty_find_listing": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "resolver": True,
        "primary_param": "query",
    },
    "guesty_find_reservation": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "resolver": True,
        "primary_param": "confirmation_code",
    },
    "guesty_get_listing_summary": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "listing_id",
    },
    "guesty_search_reviews": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "guesty_search_owners": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "guesty_get_guests": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "guesty_get_guest": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "guest_id",
    },
    "guesty_search_guests": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
    },
    "guesty_get_webhooks": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "guesty_get_webhook": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "webhook_id",
    },
    "guesty_create_webhook": {
        "read_only": False,
        "mutation": True,
        "idempotent": False,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "guesty_update_webhook": {
        "read_only": False,
        "mutation": True,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "webhook_id",
    },
    "guesty_delete_webhook": {
        "read_only": False,
        "mutation": True,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "webhook_id",
    },
    "guesty_get_listing_calendar": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "listing_id",
    },
    "guesty_is_listing_available_on_dates": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
    },
    "guesty_update_reservation_custom_fields": {
        "read_only": False,
        "mutation": True,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
    },
    "guesty_update_listing_custom_fields": {
        "read_only": False,
        "mutation": True,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
    },
}
