"""MCP server for Stripe, exposing payouts, charges, balance, and refunds."""

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.transport import structured_result
from stripe_ops import (
    get_balance,
    get_balance_transaction,
    get_charge,
    get_charges,
    get_payment_intent,
    get_payout_transactions,
    get_payouts,
    get_refund,
)

mcp = FastMCP(
    "stripe",
    instructions=(
        "You have access to Stripe payouts, charges, payment intents, balance, and refunds. "
        "Use stripe_get_payouts to list payouts (optionally with date range). "
        "Use stripe_get_payout_transactions for balance transactions in a payout. "
        "Use stripe_get_charges / stripe_get_charge for charges (ch_xxx). "
        "Use stripe_get_payment_intent for payment intents (pi_xxx). "
        "Use stripe_get_balance and stripe_get_balance_transaction for balance. "
        "Use stripe_get_refund to get a refund by ID."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


@mcp.tool(structured_output=False)
async def stripe_get_payouts(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    detail_level: str = "compact",
) -> CallToolResult:
    """List Stripe payouts for the connected account.

    **Use when:**
        You need payout history for reconciliation; follow with stripe_get_payout_transactions using payout ids.

    **Args:**
        start_date: Optional ISO date/datetime; filtering applies only if both start_date and end_date are set.
        end_date: Optional ISO date/datetime; pair with start_date for a custom window.
        detail_level: ``compact`` (default), ``summary``, or ``full`` row shape for each payout.

    **Returns:**
        ``{"data": [...], "detail_level": string, "meta": {...}}`` on success.

    **Notes:**
        If either date is omitted, defaults to previous calendar month (UTC). Amounts are in smallest currency unit.

    **Errors:**
        ``{"error": string, "details": string}`` on API failure.

    **Example:**
        ``stripe_get_payouts()`` then ``stripe_get_payout_transactions(payout_id="po_...")``
    """
    return structured_result(await get_payouts(start_date=start_date, end_date=end_date, detail_level=detail_level))


@mcp.tool(structured_output=False)
async def stripe_get_payout_transactions(
    payout_id: Optional[str] = None,
    detail_level: str = "compact",
) -> CallToolResult:
    """List balance transactions included in a Stripe payout.

    **Use when:**
        You need ledger lines for one payout (not the payout object itself).

    **Args:**
        payout_id: Payout id (``po_...``).
        detail_level: ``compact`` (default) trims each balance transaction row; ``full`` returns full objects.

    **Returns:**
        ``{"payout_id", "transactions", "detail_level"}``; empty ``transactions`` is normal.

    **Notes:**
        Synthetic ``payout`` type rows are excluded.

    **Errors:**
        ``{"error", "details"}`` for validation or API errors.

    **Example:**
        ``stripe_get_payout_transactions(payout_id="po_123")``
    """
    return structured_result(await get_payout_transactions(payout_id=payout_id, detail_level=detail_level))


@mcp.tool()
async def stripe_get_charges(limit: int = 100) -> Dict[str, Any]:
    """List charges for this Stripe account. Optional limit (default 100)."""
    return await get_charges(limit=limit)


@mcp.tool(structured_output=False)
async def stripe_get_charge(charge_id: Optional[str] = None) -> CallToolResult:
    """Get one charge by id (``ch_...``).

    **Use when:**
        You have a charge id and need the Charge object.

    **Args:**
        charge_id: Stripe charge id (``ch_`` prefix). Use stripe_get_payment_intent for ``pi_``.

    **Returns:**
        Serialized Charge dict on success.

    **Notes:**
        Passing a ``pi_`` id returns ``wrong_id_prefix`` — use the correct tool.

    **Errors:**
        ``{"error", "details"}``; optional ``hint`` for pi_/ch_ mix-ups.

    **Example:**
        ``stripe_get_charge(charge_id="ch_123")``
    """
    return structured_result(await get_charge(charge_id=charge_id))


@mcp.tool(structured_output=False)
async def stripe_get_payment_intent(payment_intent_id: Optional[str] = None) -> CallToolResult:
    """Get one PaymentIntent by id (``pi_...``).

    **Use when:**
        You have a PaymentIntent id.

    **Args:**
        payment_intent_id: ``pi_`` id only.

    **Returns:**
        Serialized PaymentIntent on success.

    **Notes:**
        Use stripe_get_charge for ``ch_`` ids.

    **Errors:**
        ``{"error", "details"}``.

    **Example:**
        ``stripe_get_payment_intent(payment_intent_id="pi_123")``
    """
    return structured_result(await get_payment_intent(payment_intent_id=payment_intent_id))


@mcp.tool(structured_output=False)
async def stripe_get_balance(detail_level: str = "compact") -> CallToolResult:
    """Get current Stripe account balance.

    **Use when:**
        You need available vs pending funds at a glance.

    **Args:**
        detail_level: ``compact`` (default) returns object/available/pending/livemode; ``full`` returns full Balance.

    **Returns:**
        ``{"data": {balance fields}, "detail_level", "meta": {...}}``.
        ``data`` is the authoritative singleton.

    **Notes:**
        Amounts are smallest currency unit.

    **Errors:**
        ``{"error", "details"}``.

    **Example:**
        ``stripe_get_balance()``
    """
    return structured_result(await get_balance(detail_level=detail_level))


@mcp.tool(structured_output=False)
async def stripe_get_balance_transaction(transaction_id: Optional[str] = None) -> CallToolResult:
    """Get one balance transaction (``txn_...``).

    **Use when:**
        You need a single ledger line.

    **Args:**
        transaction_id: Balance transaction id.

    **Returns:**
        Serialized BalanceTransaction on success.

    **Notes:**
        Id prefix for balance transactions is ``txn_``.

    **Errors:**
        ``{"error", "details"}``.

    **Example:**
        ``stripe_get_balance_transaction(transaction_id="txn_123")``
    """
    return structured_result(await get_balance_transaction(transaction_id=transaction_id))


@mcp.tool(structured_output=False)
async def stripe_get_refund(refund_id: Optional[str] = None) -> CallToolResult:
    """Get one refund (``re_...``).

    **Use when:**
        You have a refund id.

    **Args:**
        refund_id: Stripe refund id.

    **Returns:**
        Serialized Refund on success.

    **Notes:**
        Refund ids use prefix ``re_``.

    **Errors:**
        ``{"error", "details"}``.

    **Example:**
        ``stripe_get_refund(refund_id="re_123")``
    """
    return structured_result(await get_refund(refund_id=refund_id))


# ---------------------------------------------------------------------------
# Machine-readable tool metadata
# ---------------------------------------------------------------------------

TOOL_METADATA: dict = {
    "stripe_get_payouts": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": False,
    },
    "stripe_get_payout_transactions": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": True,
        "primary_param": "payout_id",
    },
    "stripe_get_charges": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "cursor",
        "requires_real_fixture": False,
    },
    "stripe_get_charge": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "charge_id",
    },
    "stripe_get_balance": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
    },
    "stripe_get_balance_transaction": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "transaction_id",
    },
    "stripe_get_refund": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": True,
        "primary_param": "refund_id",
    },
}
