"""Stripe API operations: client holder, serialization, and API calls."""

import asyncio
import calendar
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import stripe as _stripe
from mcp_platform.detail_level import parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta

logger = logging.getLogger(__name__)

# Set at runtime by main.py via set_stripe_client()
_stripe_client: Any = None

# Timeout for Stripe API calls that may be slow or rate-limited
# Below mcp-proxy upstream HTTP timeout so list/detail calls can finish before the proxy cuts the connection.
STRIPE_CALL_TIMEOUT = 50.0

_PAYOUT_COMPACT_KEYS = frozenset(
    {"id", "amount", "currency", "status", "arrival_date", "created", "description", "type", "method"}
)
_PAYOUT_SUMMARY_EXTRA = frozenset({"failure_code", "failure_message", "destination", "statement_descriptor"})
_CHARGE_COMPACT_KEYS = frozenset(
    {
        "id",
        "amount",
        "amount_captured",
        "currency",
        "created",
        "status",
        "paid",
        "refunded",
        "description",
        "failure_code",
        "failure_message",
        "payment_intent",
    }
)
_CHARGE_SUMMARY_EXTRA = frozenset({"outcome", "billing_details", "calculated_statement_descriptor"})
_BTX_COMPACT_KEYS = frozenset(
    {"id", "amount", "fee", "net", "currency", "type", "created", "description", "status", "reporting_category"}
)


def set_stripe_client(client: Any) -> None:
    """Set the Stripe client (called during app lifespan)."""
    global _stripe_client
    _stripe_client = client


def _get_client():
    """Return the Stripe client (set during app lifespan)."""
    if _stripe_client is None:
        raise RuntimeError("Stripe client not initialized — MCP started before lifespan?")
    return _stripe_client


def to_serializable(obj: Any) -> Any:
    """Convert Stripe objects and nested structures to JSON-serializable form."""
    if obj is None:
        return None
    if hasattr(obj, "to_dict_recursive") and callable(obj.to_dict_recursive):
        return obj.to_dict_recursive()
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_serializable(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)):
        return obj
    try:
        return to_serializable(dict(obj))
    except (TypeError, ValueError):
        return str(obj)


def _pick_keys(d: Any, keys: frozenset) -> Any:
    if not isinstance(d, dict):
        return d
    return {k: d[k] for k in keys if k in d}


def _shape_payout_row(row: Any, detail_level: str) -> Any:
    d = to_serializable(row)
    if not isinstance(d, dict):
        return d
    dl = parse_detail_level(detail_level, default="compact")
    if dl == "full":
        return d
    if dl == "summary":
        keys = _PAYOUT_COMPACT_KEYS | _PAYOUT_SUMMARY_EXTRA
        return _pick_keys(d, keys)
    return _pick_keys(d, _PAYOUT_COMPACT_KEYS)


def _shape_charge_row(row: Any, detail_level: str) -> Any:
    d = to_serializable(row)
    if not isinstance(d, dict):
        return d
    dl = parse_detail_level(detail_level, default="compact")
    if dl == "full":
        return d
    if dl == "summary":
        keys = _CHARGE_COMPACT_KEYS | _CHARGE_SUMMARY_EXTRA
        return _pick_keys(d, keys)
    return _pick_keys(d, _CHARGE_COMPACT_KEYS)


def _shape_btx_row(row: Any, detail_level: str) -> Any:
    d = to_serializable(row)
    if not isinstance(d, dict):
        return d
    dl = parse_detail_level(detail_level, default="compact")
    if dl == "full":
        return d
    return _pick_keys(d, _BTX_COMPACT_KEYS)


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------


async def get_payouts(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> Dict[str, Any]:
    """List payouts, optionally filtered by date range. Returns JSON-serializable dict."""
    client = _get_client()
    dl = parse_detail_level(detail_level, default="compact")

    def _run():
        if start_date and end_date:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            start_ts = int(start_dt.timestamp())
            end_ts = int(end_dt.timestamp())
        else:
            now = datetime.now(timezone.utc)
            start_of_month = datetime(now.year, now.month - 1, 1, tzinfo=timezone.utc)
            end_of_month_day = calendar.monthrange(now.year, now.month - 1)[1]
            end_of_month = datetime(now.year, now.month - 1, end_of_month_day, 23, 59, 59, tzinfo=timezone.utc)
            start_ts = int(start_of_month.timestamp())
            end_ts = int(end_of_month.timestamp())

        payouts = client.v1.payouts.list(params={"created": {"gte": start_ts, "lte": end_ts}})
        return list(payouts.auto_paging_iter())

    try:
        payouts = await asyncio.to_thread(_run)
        rows: List[Any] = [_shape_payout_row(p, dl) for p in payouts]
        return with_response_meta({"data": rows, "detail_level": dl}, tool="stripe_get_payouts")
    except Exception as e:
        logger.exception("get_payouts failed: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check Stripe API status.",
        )


async def get_payout_transactions(
    payout_id: Optional[str] = None,
    detail_level: Optional[str] = None,
) -> Dict[str, Any]:
    """List balance transactions for a payout (excluding the payout itself)."""
    if not payout_id or not str(payout_id).strip():
        return tool_error("validation_error", details="payout_id is required", cause="validation", retryable=False)
    client = _get_client()
    dl = parse_detail_level(detail_level, default="compact")

    def _run():
        balance_transactions = client.v1.balance_transactions.list(params={"payout": payout_id})
        out = []
        for t in balance_transactions.auto_paging_iter():
            if t.type == "payout":
                continue
            out.append(t)
        return out

    try:
        transactions = await asyncio.to_thread(_run)
        rows = [_shape_btx_row(t, dl) for t in transactions]
        return with_response_meta(
            {"payout_id": payout_id, "transactions": rows, "detail_level": dl},
            tool="stripe_get_payout_transactions",
            data_from="transactions",
        )
    except Exception as e:
        logger.exception("get_payout_transactions failed: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check Stripe API status.",
        )


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------


async def get_charges(limit: int = 100, detail_level: Optional[str] = None) -> Dict[str, Any]:
    """List charges. Optional limit (default 100)."""
    client = _get_client()
    dl = parse_detail_level(detail_level, default="compact")

    def _run():
        # Use a single list page only. auto_paging_iter() walks every page and can exceed proxy timeouts.
        page = client.v1.charges.list(params={"limit": min(limit, 100)})
        items = list(getattr(page, "data", None) or [])
        stripe_has_more = bool(getattr(page, "has_more", False))
        # Stripe cursor-based continuation: next call uses starting_after=last_id
        last_id = items[-1].id if stripe_has_more and items else None
        return items, stripe_has_more, last_id

    def _run() -> List[Any]:
        if has_start and has_end:
            start_dt = datetime.fromisoformat(str(start_date).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            params: Dict[str, Any] = {
                "limit": 100,
                "created": {"gte": int(start_dt.timestamp()), "lte": int(end_dt.timestamp())},
            }
            out: List[Any] = []
            for c in client.v1.charges.list(params=params).auto_paging_iter():
                out.append(c)
                if max_total is not None and len(out) >= max_total:
                    break
            return out

        page_limit = min(max(int(limit), 1), 100)
        lst = client.v1.charges.list(params={"limit": page_limit})
        return list(lst.data)

    timeout = STRIPE_CHARGES_RANGE_TIMEOUT if (has_start and has_end) else STRIPE_CALL_TIMEOUT
    try:
        charges, stripe_has_more, next_cursor = await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=STRIPE_CALL_TIMEOUT
        )
        rows = [_shape_charge_row(c, dl) for c in charges]
        return with_response_meta(
            {"data": rows, "detail_level": dl},
            tool="stripe_get_charges",
            pagination=build_pagination_meta(
                limit=limit,
                has_more=stripe_has_more,
                next_cursor=next_cursor,
            ),
        )
    except asyncio.TimeoutError:
        logger.warning("get_charges timed out after %.0fs", STRIPE_CALL_TIMEOUT)
        return tool_error(
            "timeout",
            details="Stripe API request timed out. Try a lower limit or retry later.",
            cause="timeout",
            retryable=True,
            suggested_fix="Retry with a smaller limit or wait and retry; check Stripe status if it persists.",
            timeout_seconds=STRIPE_CALL_TIMEOUT,
        )
    except Exception as e:
        logger.exception("get_charges failed: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or verify API credentials and Stripe service status.",
        )


async def get_charge(charge_id: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve a single charge by ID (ch_xxx). Returns friendly error if given pi_xxx."""
    if not charge_id or not str(charge_id).strip():
        return tool_error("validation_error", details="charge_id is required", cause="validation", retryable=False)
    cid = str(charge_id).strip()
    if cid.startswith("pi_"):
        return tool_error(
            "wrong_id_prefix",
            details=(
                f"'{cid}' is a PaymentIntent ID (pi_...), not a charge. Use stripe_get_payment_intent for pi_ IDs."
            ),
            hint="Use stripe_get_payment_intent; response may include latest_charge (ch_...).",
            cause="validation",
            retryable=False,
        )
    client = _get_client()

    def _run():
        return client.v1.charges.retrieve(cid)

    try:
        charge = await asyncio.to_thread(_run)
        if charge is None:
            return tool_error("not_found", details=f"Charge {cid} not found", cause="not_found", retryable=False)
        return to_serializable(charge)
    except _stripe.InvalidRequestError as e:
        http_status = getattr(e, "http_status", None)
        code = getattr(e, "code", None)
        if http_status == 404 or code == "resource_missing":
            return tool_error(
                "not_found",
                details=f"Charge {cid!r} not found: {e}",
                cause="not_found",
                retryable=False,
            )
        logger.exception("get_charge invalid request: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=False,
        )
    except Exception as e:
        logger.exception("get_charge failed: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check Stripe API status.",
        )


async def get_payment_intent(payment_intent_id: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve a single PaymentIntent by ID (pi_xxx). Returns friendly error if given ch_xxx."""
    if not payment_intent_id or not str(payment_intent_id).strip():
        return tool_error(
            "validation_error", details="payment_intent_id is required", cause="validation", retryable=False
        )
    pid = str(payment_intent_id).strip()
    if pid.startswith("ch_"):
        return tool_error(
            "wrong_id_prefix",
            cause="validation",
            retryable=False,
            details=f"'{pid}' is a charge ID (ch_...), not a payment intent. Use stripe_get_charge for ch_ IDs.",
        )
    client = _get_client()

    def _run():
        return client.v1.payment_intents.retrieve(pid)

    try:
        pi = await asyncio.to_thread(_run)
        if pi is None:
            return tool_error("not_found", details=f"PaymentIntent {pid} not found", cause="not_found", retryable=False)
        return to_serializable(pi)
    except _stripe.InvalidRequestError as e:
        http_status = getattr(e, "http_status", None)
        code = getattr(e, "code", None)
        if http_status == 404 or code == "resource_missing":
            return tool_error(
                "not_found",
                details=f"PaymentIntent {pid!r} not found: {e}",
                cause="not_found",
                retryable=False,
            )
        logger.exception("get_payment_intent invalid request: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=False,
        )
    except Exception as e:
        logger.exception("get_payment_intent failed: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check Stripe API status.",
        )


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


async def get_balance(detail_level: Optional[str] = None) -> Dict[str, Any]:
    """Get current account balance (available and pending).

    Returns ``{"data": {balance fields}, "meta": {...}}``.
    ``data`` is the authoritative singleton.
    """
    client = _get_client()
    dl = parse_detail_level(detail_level, default="compact")

    def _run():
        return client.v1.balance.retrieve()

    try:
        balance = await asyncio.to_thread(_run)
        if balance is None:
            return tool_error(
                "stripe_api_error", details="Failed to retrieve balance", cause="upstream_error", retryable=True
            )
        raw = to_serializable(balance)
        if dl != "full" and isinstance(raw, dict):
            slim: Dict[str, Any] = {}
            for key in ("object", "available", "pending", "livemode"):
                if key in raw:
                    slim[key] = raw[key]
            return with_response_meta({"data": slim, "detail_level": dl}, tool="stripe_get_balance")
        if isinstance(raw, dict):
            return with_response_meta({"data": raw, "detail_level": dl}, tool="stripe_get_balance")
        return raw
    except Exception as e:
        logger.exception("get_balance failed: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check Stripe API status.",
        )


async def get_balance_transaction(transaction_id: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve a single balance transaction by ID (txn_xxx)."""
    if not transaction_id or not str(transaction_id).strip():
        return tool_error("validation_error", details="transaction_id is required", cause="validation", retryable=False)
    client = _get_client()

    def _run():
        return client.v1.balance_transactions.retrieve(transaction_id)

    try:
        txn = await asyncio.to_thread(_run)
        if txn is None:
            return tool_error(
                "not_found",
                details=f"Balance transaction {transaction_id} not found",
                cause="not_found",
                retryable=False,
            )
        return to_serializable(txn)
    except _stripe.InvalidRequestError as e:
        http_status = getattr(e, "http_status", None)
        code = getattr(e, "code", None)
        if http_status == 404 or code == "resource_missing":
            return tool_error(
                "not_found",
                details=f"Balance transaction {transaction_id!r} not found: {e}",
                cause="not_found",
                retryable=False,
            )
        logger.exception("get_balance_transaction invalid request: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=False,
        )
    except Exception as e:
        logger.exception("get_balance_transaction failed: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check Stripe API status.",
        )


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


async def get_refund(refund_id: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve a single refund by ID (re_xxx).

    Pre-validates that refund_id starts with ``re_`` so obviously invalid ids never reach Stripe.
    Stripe's resource_missing (404) is mapped to ``not_found`` with ``retryable=False``.
    """
    if not refund_id or not str(refund_id).strip():
        return tool_error("validation_error", details="refund_id is required", cause="validation", retryable=False)
    rid = str(refund_id).strip()
    if not rid.startswith("re_"):
        return tool_error(
            "validation_error",
            details=f"'{rid}' does not look like a Stripe refund id (expected 're_...' prefix).",
            cause="validation",
            retryable=False,
            suggested_fix="Stripe refund ids start with 're_'. Check the id source.",
        )
    client = _get_client()

    def _run():
        return client.v1.refunds.retrieve(rid)

    try:
        refund = await asyncio.to_thread(_run)
        if refund is None:
            return tool_error("not_found", details=f"Refund {rid!r} not found", cause="not_found", retryable=False)
        return to_serializable(refund)
    except _stripe.InvalidRequestError as e:
        # Permanent not-found: Stripe returns 404 / resource_missing for unknown refund IDs.
        # This is not retryable — the ID does not exist.
        http_status = getattr(e, "http_status", None)
        code = getattr(e, "code", None)
        err_msg = str(e).lower()
        if http_status == 404 or code == "resource_missing" or "no such refund" in err_msg:
            return tool_error(
                "not_found",
                details=f"Refund {rid!r} not found: {e}",
                cause="not_found",
                retryable=False,
                suggested_fix="Verify the refund id exists in your Stripe account.",
            )
        logger.exception("get_refund invalid request: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=False,
        )
    except Exception as e:
        logger.exception("get_refund failed: %s", e)
        return tool_error(
            "stripe_api_error",
            details=str(e),
            cause="upstream_error",
            retryable=True,
            suggested_fix="Retry later or check Stripe API status.",
        )
