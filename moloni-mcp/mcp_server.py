"""MCP tools for Moloni (read-only): invoices and credit notes."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import moloni_client
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result

_MAX_QTY = 50

mcp = FastMCP(
    "moloni",
    instructions=(
        "Moloni accounting read-only MCP. Tools: moloni_list_invoices, moloni_get_invoice, "
        "moloni_list_credit_notes, moloni_get_credit_note. "
        "Success payloads include meta.schema_version, meta.tool, and meta.pagination for list tools. "
        "Moloni caps qty at 50 per request; limit is clamped automatically. "
        "company_id comes from configured credentials. "
        "Behind mcp-proxy id 'moloni' tools stay moloni_*."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


def _clamp_qty(limit: int) -> int:
    if limit < 1:
        return 1
    return min(limit, _MAX_QTY)


def _moloni_failure(raw: Any) -> Optional[dict[str, Any]]:
    """Detect Moloni/API error dicts (not a document row)."""
    if not isinstance(raw, dict):
        return None
    err = raw.get("error")
    if err in (None, "", 0, False):
        return None
    if "document_id" in raw and isinstance(raw.get("document_id"), (int, str)) and str(raw["document_id"]).isdigit():
        return None
    details = str(raw.get("error_description") or raw.get("error_message") or err)
    return tool_error(
        "upstream_failed",
        details=details[:2000],
        cause="upstream_error",
        retryable=True,
        suggested_fix="Check Moloni API status or credentials; retry later.",
    )


def _company_payload(extra: Dict[str, Any]) -> Dict[str, Any]:
    try:
        company_id = moloni_client.get_client().company_id
    except RuntimeError:
        company_id = None
    if company_id is None or company_id == "":
        raise ValueError("company_id missing in Moloni credentials")
    out = {"company_id": company_id, **extra}
    return {k: v for k, v in out.items() if v is not None and v != ""}


def _finish_list(
    *,
    tool: str,
    rows: List[Any],
    limit: int,
    offset: int,
) -> CallToolResult:
    eff = _clamp_qty(limit)
    has_more = len(rows) >= eff and eff > 0
    pag = build_pagination_meta(
        limit=eff, offset=offset, has_more=has_more, next_offset=offset + eff if has_more else None
    )
    payload = with_response_meta({"data": rows}, tool=tool, pagination=pag)
    return structured_result(payload)


def _finish_one(*, tool: str, doc: Dict[str, Any]) -> CallToolResult:
    if err := _moloni_failure(doc):
        return structured_result(err)
    payload = with_response_meta(dict(doc), tool=tool)
    return structured_result(payload)


@mcp.tool(structured_output=False)
async def moloni_list_invoices(
    offset: int = 0,
    limit: int = 50,
    customer_id: Optional[int] = None,
    document_set_id: Optional[int] = None,
    number: Optional[int] = None,
    date: Optional[str] = None,
    year: Optional[int] = None,
    your_reference: Optional[str] = None,
    our_reference: Optional[str] = None,
) -> CallToolResult:
    """List invoices (read). Paginated; Moloni max qty is 50.

    Args:
        offset: Row offset (Moloni ``offset``).
        limit: Page size (Moloni ``qty``, capped at 50).
        customer_id, document_set_id, number, date, year, your_reference, our_reference: optional filters.

    Returns:
        { data: [...], meta: { tool, pagination } }.
    """
    try:
        eff = _clamp_qty(limit)
        body = _company_payload(
            {
                "qty": eff,
                "offset": max(0, offset),
                "customer_id": customer_id,
                "document_set_id": document_set_id,
                "number": number,
                "date": date,
                "year": year,
                "your_reference": your_reference,
                "our_reference": our_reference,
            }
        )
    except ValueError as e:
        return structured_result(tool_error("validation_error", details=str(e), cause="validation", retryable=False))

    raw = await asyncio.to_thread(moloni_client.api_post, "invoices/getAll", body)
    if err := _moloni_failure(raw):
        return structured_result(err)
    if not isinstance(raw, list):
        return structured_result(
            tool_error(
                "upstream_failed",
                details="Unexpected Moloni invoices/getAll response shape",
                cause="upstream_error",
                retryable=True,
            )
        )
    return _finish_list(tool="moloni_list_invoices", rows=raw, limit=limit, offset=max(0, offset))


@mcp.tool(structured_output=False)
async def moloni_get_invoice(document_id: str) -> CallToolResult:
    """Fetch one invoice by Moloni ``document_id`` (read)."""
    did = (document_id or "").strip()
    if not did or not did.isdigit():
        return structured_result(
            tool_error(
                "validation_error", details="document_id must be a numeric string", cause="validation", retryable=False
            )
        )
    try:
        body = _company_payload({"document_id": did})
    except ValueError as e:
        return structured_result(tool_error("validation_error", details=str(e), cause="validation", retryable=False))

    raw = await asyncio.to_thread(moloni_client.api_post, "invoices/getOne", body)
    one = moloni_client.unwrap_single_item(raw)
    if not isinstance(one, dict):
        return structured_result(
            tool_error("not_found", details=f"Invoice {did} not found", cause="not_found", retryable=False)
        )
    if err := _moloni_failure(one):
        return structured_result(err)
    if one.get("document_id") is None:
        return structured_result(
            tool_error("not_found", details=f"Invoice {did} not found", cause="not_found", retryable=False)
        )
    return _finish_one(tool="moloni_get_invoice", doc=one)


@mcp.tool(structured_output=False)
async def moloni_list_credit_notes(
    offset: int = 0,
    limit: int = 50,
    customer_id: Optional[int] = None,
    document_set_id: Optional[int] = None,
    number: Optional[int] = None,
    date: Optional[str] = None,
    year: Optional[int] = None,
    your_reference: Optional[str] = None,
    our_reference: Optional[str] = None,
) -> CallToolResult:
    """List credit notes (read). Paginated; Moloni max qty is 50."""
    try:
        eff = _clamp_qty(limit)
        body = _company_payload(
            {
                "qty": eff,
                "offset": max(0, offset),
                "customer_id": customer_id,
                "document_set_id": document_set_id,
                "number": number,
                "date": date,
                "year": year,
                "your_reference": your_reference,
                "our_reference": our_reference,
            }
        )
    except ValueError as e:
        return structured_result(tool_error("validation_error", details=str(e), cause="validation", retryable=False))

    raw = await asyncio.to_thread(moloni_client.api_post, "creditNotes/getAll", body)
    if err := _moloni_failure(raw):
        return structured_result(err)
    if not isinstance(raw, list):
        return structured_result(
            tool_error(
                "upstream_failed",
                details="Unexpected Moloni creditNotes/getAll response shape",
                cause="upstream_error",
                retryable=True,
            )
        )
    return _finish_list(tool="moloni_list_credit_notes", rows=raw, limit=limit, offset=max(0, offset))


@mcp.tool(structured_output=False)
async def moloni_get_credit_note(document_id: str) -> CallToolResult:
    """Fetch one credit note by Moloni ``document_id`` (read)."""
    did = (document_id or "").strip()
    if not did or not did.isdigit():
        return structured_result(
            tool_error(
                "validation_error", details="document_id must be a numeric string", cause="validation", retryable=False
            )
        )
    try:
        body = _company_payload({"document_id": did})
    except ValueError as e:
        return structured_result(tool_error("validation_error", details=str(e), cause="validation", retryable=False))

    raw = await asyncio.to_thread(moloni_client.api_post, "creditNotes/getOne", body)
    one = moloni_client.unwrap_single_item(raw)
    if not isinstance(one, dict):
        return structured_result(
            tool_error("not_found", details=f"Credit note {did} not found", cause="not_found", retryable=False)
        )
    if err := _moloni_failure(one):
        return structured_result(err)
    if one.get("document_id") is None:
        return structured_result(
            tool_error("not_found", details=f"Credit note {did} not found", cause="not_found", retryable=False)
        )
    return _finish_one(tool="moloni_get_credit_note", doc=one)
