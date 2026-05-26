"""MCP tools for PriceLabs Customer API (read-only initial scope)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pricelabs_client
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.envelope import tool_error
from mcp_platform.meta import with_response_meta
from mcp_platform.transport import structured_result

mcp = FastMCP(
    "pricelabs",
    instructions=(
        "PriceLabs Customer API read-only MCP. Tools: pricelabs_get_neighborhood_data and "
        "pricelabs_get_date_specific_overrides. Provide PriceLabs listing_id and pms exactly as known "
        "in PriceLabs. This server does not expose Date Specific Override writes. "
        "Behind mcp-proxy id 'pricelabs' tools stay pricelabs_*."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _client_error_result(exc: Exception) -> CallToolResult:
    if isinstance(exc, pricelabs_client.PriceLabsAPIError):
        return structured_result(
            tool_error(
                exc.code,
                details=exc.details,
                cause=exc.cause,  # type: ignore[arg-type]
                retryable=exc.retryable,
                status_code=exc.status_code,
                suggested_fix=(
                    "Retry later or reduce request frequency; PriceLabs limits API keys to 60 requests/minute "
                    "and 1000 requests/hour."
                    if exc.code == "rate_limited"
                    else None
                ),
            )
        )
    return structured_result(
        tool_error(
            "upstream_failed",
            details=str(exc),
            cause="upstream_error",
            retryable=True,
            suggested_fix="Check PriceLabs API credentials and service status; retry later.",
        )
    )


@mcp.tool(structured_output=False)
async def pricelabs_get_neighborhood_data(pms: str, listing_id: str) -> CallToolResult:
    """Get PriceLabs Neighborhood Data for one listing.

    Args:
        pms: PriceLabs PMS name for the listing.
        listing_id: PriceLabs listing id.

    Returns:
        Upstream PriceLabs payload with ``status`` and ``data`` preserved, plus MCP response meta.
    """
    try:
        pms_value = _required_text(pms, "pms")
        listing_value = _required_text(listing_id, "listing_id")
    except ValueError as exc:
        return structured_result(tool_error("validation_error", details=str(exc), cause="validation", retryable=False))

    try:
        result = await asyncio.to_thread(
            pricelabs_client.get_neighborhood_data,
            pms=pms_value,
            listing_id=listing_value,
        )
    except Exception as exc:
        return _client_error_result(exc)

    payload: Dict[str, Any] = dict(result)
    return structured_result(with_response_meta(payload, tool="pricelabs_get_neighborhood_data"))


@mcp.tool(structured_output=False)
async def pricelabs_get_date_specific_overrides(listing_id: str, pms: str) -> CallToolResult:
    """Fetch existing PriceLabs Date Specific Overrides for one listing.

    Args:
        listing_id: PriceLabs listing id.
        pms: PriceLabs PMS name for the listing.

    Returns:
        ``{"overrides": [...], "count": n, "meta": ...}``.
    """
    try:
        listing_value = _required_text(listing_id, "listing_id")
        pms_value = _required_text(pms, "pms")
    except ValueError as exc:
        return structured_result(tool_error("validation_error", details=str(exc), cause="validation", retryable=False))

    try:
        result = await asyncio.to_thread(
            pricelabs_client.get_date_specific_overrides,
            listing_id=listing_value,
            pms=pms_value,
        )
    except Exception as exc:
        return _client_error_result(exc)

    overrides = result.get("overrides")
    if not isinstance(overrides, list):
        return structured_result(
            tool_error(
                "upstream_failed",
                details="Unexpected PriceLabs DSO response shape: missing overrides array",
                cause="upstream_error",
                retryable=True,
            )
        )
    payload = dict(result)
    payload["count"] = len(overrides)
    return structured_result(with_response_meta(payload, tool="pricelabs_get_date_specific_overrides"))
