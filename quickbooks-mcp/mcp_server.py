"""QuickBooks MCP server: tools for bank accounts, transactions in/out, and balances."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.types import CallToolResult
from mcp_platform.coerce_numeric import coerce_int
from mcp_platform.detail_level import parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result

logger = logging.getLogger(__name__)

# Set by main.py after QuickBooks service is created so stateless MCP requests can see it
# (from main import qb_service can be None in stateless lifespan context).
_qb_service: Optional[object] = None


def set_qb_service(service: object) -> None:
    global _qb_service
    _qb_service = service


@dataclass
class AppContext:
    """Application context holding the QuickBooks service."""

    qb_service: object  # QuickBooksService from shared


def _get_qb_service(ctx: Context[ServerSession, AppContext]):
    return ctx.request_context.lifespan_context.qb_service


@asynccontextmanager
async def mcp_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    # Use module-level ref set by main (stateless requests may not see main.qb_service).
    service = _qb_service
    if service is None:
        try:
            import main as main_mod

            service = getattr(main_mod, "qb_service", None)
        except Exception:
            pass
    if service is None:
        raise RuntimeError("QuickBooks service not initialised — MCP lifespan started before app startup?")
    yield AppContext(qb_service=service)


mcp = FastMCP(
    "QuickBooks MCP",
    instructions=(
        "You have access to QuickBooks bank data. "
        "Use list_bank_accounts for bank-type accounts and IDs (use IDs with other bank tools). "
        "get_bank_transactions_in returns Deposit (money in) only; TxnDate filtering applies only when both "
        "start_date and end_date are provided (YYYY-MM-DD). "
        "get_bank_credits is the broader reconciliation endpoint for incoming credits across Deposit, incoming "
        "Transfer, Payment, and SalesReceipt. "
        "get_bank_transactions_out merges transfers, purchases/expenses, and bill payments paid from bank accounts, "
        "then paginates the combined list. "
        "get_bank_balances returns current balance per bank account. "
        "get_company_context identifies the connected QuickBooks realm/company for context checks. "
        "get_bank_account_by_name resolves bank accounts from human names (Account.Name) returning count/data rows "
        "before using account-id-specific tools."
    ),
    stateless_http=True,
    json_response=True,
    lifespan=mcp_lifespan,
    host="0.0.0.0",
)


def _parse_date(s: Optional[str]) -> Optional[date]:
    """Parse YYYY-MM-DD string to date, or return None."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_bank_name(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


@mcp.tool(structured_output=False)
def get_bank_account_by_name(
    query: str,
    limit: int = 10,
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """Resolve QuickBooks bank accounts from human account names (read/search).

    IMPORTANT: The search parameter is named ``query`` — do not pass ``name`` or ``account_name``.
    Via mcp-proxy this tool is exposed as ``quickbooks_get_bank_account_by_name``.

    Use when:
        You know the bank account label in QBO and need stable ids — use before balance or transaction tools.

    Args:
        query: Required account name text (``Account.Name``). Exact case-insensitive full name matches are listed
            before substring matches.
        limit: Max matches to return (default 10).

    Returns:
        ``{"count", "data", "detail_level": "compact"}`` with id, name, account_type, account_sub_type, currency_code
        (no balance fields — keep payload small). Empty ``data`` is success when nothing matches.

    Notes:
        Use before calling account-ID-based bank tools. Ambiguous names return multiple ``data`` rows — never auto-pick.

    Errors:
        ``{"error": "validation_error", "details": ...}`` when ``query`` is empty.
        ``{"error": "quickbooks_api_error", "details": ...}`` on QBO failures.

    Example:
        ``get_bank_account_by_name(query="Operating", limit=5)``
    """
    raw = (query or "").strip()
    if not raw:
        return structured_result(
            tool_error("validation_error", details="query is required", cause="validation", retryable=False)
        )
    cap = max(1, min(limit, 50))
    service = _get_qb_service(ctx)
    try:
        accounts = service.get_bank_accounts()
    except Exception as e:
        logger.exception("get_bank_account_by_name failed")
        return structured_result(
            tool_error(
                "quickbooks_api_error",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify QuickBooks API credentials.",
            )
        )

    rows = [
        {
            "id": a.id,
            "name": a.name,
            "account_type": a.account_type,
            "account_sub_type": a.account_sub_type,
            "currency_code": a.currency_code,
        }
        for a in accounts
    ]
    target = _normalize_bank_name(raw)
    exact_hits = [r for r in rows if _normalize_bank_name(r.get("name") or "") == target]
    needle = target
    partial = [r for r in rows if needle in _normalize_bank_name(r.get("name") or "") and r not in exact_hits]
    merged = (exact_hits + partial)[:cap]
    return structured_result(
        with_response_meta(
            {"count": len(merged), "data": merged, "detail_level": "compact"},
            tool="quickbooks_get_bank_account_by_name",
        )
    )


@mcp.tool(structured_output=False)
def list_bank_accounts(
    detail_level: str = "compact",
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """List QuickBooks bank accounts.

    **Use when:**
        You need valid bank account ids before balances, money-in, or money-out tools.

    **Args:**
        detail_level: ``compact`` (default) lists stable fields per account; echoed in the response.

    **Returns:**
        ``{"data": [...], "detail_level", "meta": {...}}``.
        ``data`` is the authoritative list field.

    **Notes:**
        Only AccountType Bank is returned. Balances are CurrentBalance snapshots, not historical as-of dates.

    **Errors:**
        ``{"error", "details"}`` on QuickBooks API or client failure.

    **Example:**
        ``list_bank_accounts(detail_level="compact")``
    """
    dl = parse_detail_level(detail_level, default="compact")
    service = _get_qb_service(ctx)
    try:
        accounts = service.get_bank_accounts()
        rows = [
            {
                "id": a.id,
                "name": a.name,
                "account_type": a.account_type,
                "account_sub_type": a.account_sub_type,
                "current_balance": a.current_balance,
                "currency_code": a.currency_code,
            }
            for a in accounts
        ]
        return structured_result(
            with_response_meta({"data": rows, "detail_level": dl}, tool="quickbooks_list_bank_accounts")
        )
    except Exception as e:
        logger.exception("list_bank_accounts failed")
        return structured_result(
            tool_error(
                "quickbooks_api_error",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify QuickBooks API credentials.",
            )
        )


@mcp.tool(structured_output=False)
def get_bank_transactions_in(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[str] = None,
    max_results: int = 100,
    start_position: int = 1,
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """List money-in transactions for QuickBooks bank accounts (Deposit entity only).

    **Use when:**
        You need deposits and other incoming bank transactions.

    **Args:**
        start_date: Optional YYYY-MM-DD; filtering applies only when both ``start_date`` and ``end_date`` parse.
        end_date: Inclusive end when paired with ``start_date``.
        account_id: Optional bank account id (DepositToAccountRef).
        max_results: Max rows per QuickBooks page.
        start_position: 1-based STARTPOSITION offset.

    **Returns:**
        ``{"transactions": [...], "detail_level": "compact"}``; empty list is success.

    **Notes:**
        Money-in only; use ``get_bank_transactions_out`` for outgoing activity. Description may be a placeholder.

    **Errors:**
        ``{"error", "details"}`` on failure.

    **Example:**
        ``get_bank_transactions_in(start_date=\"2024-01-01\", end_date=\"2024-01-31\", max_results=50)``
    """
    service = _get_qb_service(ctx)
    max_results = coerce_int(max_results, default=100, minimum=1, maximum=1000)
    start_position = coerce_int(start_position, default=1, minimum=1)
    start_d = _parse_date(start_date) if start_date else None
    end_d = _parse_date(end_date) if end_date else None
    try:
        entries = service.get_bank_transactions(
            start_date=start_d,
            end_date=end_d,
            account_id=account_id,
            max_results=max_results,
            start_position=start_position,
        )
        return structured_result(
            with_response_meta(
                {
                    "transactions": [
                        {
                            "id": e.id,
                            "date": e.date.isoformat(),
                            "amount": e.amount,
                            "account_id": e.account_id,
                            "account_name": e.account_name,
                            "description": e.description,
                            "category": e.category,
                        }
                        for e in entries
                    ],
                    "detail_level": "compact",
                },
                tool="quickbooks_get_bank_transactions_in",
                pagination=build_pagination_meta(limit=max_results, offset=start_position - 1),
                data_from="transactions",
            )
        )
    except Exception as e:
        logger.exception("get_bank_transactions_in failed")
        return structured_result(
            tool_error(
                "quickbooks_api_error",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify QuickBooks API credentials.",
            )
        )


@mcp.tool(structured_output=False)
def get_bank_transactions_out(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    from_account_id: Optional[str] = None,
    to_account_id: Optional[str] = None,
    max_results: int = 100,
    start_position: int = 1,
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """List money-out transactions for QuickBooks bank accounts.

    **Use when:**
        You need outgoing bank activity: transfers, purchases/expenses, and bill payments from bank accounts.

    **Args:**
        start_date / end_date: Optional inclusive YYYY-MM-DD range; both must parse for date filtering.
        from_account_id: Source bank id; if not a current Bank account, may yield no rows.
        to_account_id: Destination bank id for Transfer filtering only.
        max_results: Cap after merge/sort.
        start_position: 1-based slice into the merged list.

    **Returns:**
        ``{"transactions": [...], "detail_level": "compact"}``; rows include ``transaction_type`` and account aliases.

    **Notes:**
        Merges transfers with Purchase records and BillPayment records from bank, sorts by (date, id) descending.
        QuickBooks UI expenses/checks are exposed by the API as Purchase records.

    **Errors:**
        ``{"error", "details"}`` on failure.

    **Example:**
        ``get_bank_transactions_out(from_account_id=\"35\", max_results=25)``
    """
    service = _get_qb_service(ctx)
    max_results = coerce_int(max_results, default=100, minimum=1, maximum=1000)
    start_position = coerce_int(start_position, default=1, minimum=1)
    start_d = _parse_date(start_date) if start_date else None
    end_d = _parse_date(end_date) if end_date else None
    try:
        entries = service.get_bank_money_out_transactions(
            start_date=start_d,
            end_date=end_d,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            max_results=max_results,
            start_position=start_position,
        )
        return structured_result(
            with_response_meta(
                {
                    "transactions": [
                        {
                            "id": e.id,
                            "date": e.date.isoformat(),
                            "amount": e.amount,
                            "transaction_type": e.transaction_type,
                            "bank_account_id": e.bank_account_id,
                            "bank_account_name": e.bank_account_name,
                            "from_account_id": e.bank_account_id,
                            "from_account_name": e.bank_account_name,
                            "to_account_id": e.to_account_id,
                            "to_account_name": e.to_account_name,
                            "private_note": e.private_note,
                            "payee_name": e.payee_name,
                            "category": e.category,
                        }
                        for e in entries
                    ],
                    "detail_level": "compact",
                },
                tool="quickbooks_get_bank_transactions_out",
                pagination=build_pagination_meta(limit=max_results, offset=start_position - 1),
                data_from="transactions",
            )
        )
    except Exception as e:
        logger.exception("get_bank_transactions_out failed")
        return structured_result(
            tool_error(
                "quickbooks_api_error",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify QuickBooks API credentials.",
            )
        )


@mcp.tool(structured_output=False)
def get_bank_credits(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_id: Optional[str] = None,
    external_transaction_id: Optional[str] = None,
    max_results: int = 100,
    start_position: int = 1,
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """List incoming QuickBooks credit candidates for bank reconciliation.

    **Use when:**
        You need a broader money-in surface than ``get_bank_transactions_in`` for reconciliation gaps.

    **Args:**
        start_date / end_date: Optional inclusive YYYY-MM-DD range; both must parse for date filtering.
        account_id: Optional receiving account id. Applies to Deposit/Payment/SalesReceipt
            ``DepositToAccountRef`` and incoming Transfer ``ToAccountRef``.
        external_transaction_id: Optional exact match against exposed transaction identifiers such as
            ``Id``, ``DocNumber``, or ``PaymentRefNum`` after retrieval. Use with a date/account window
            when matching non-Id references.
        max_results: Cap after merge/sort.
        start_position: 1-based slice into the merged list.

    **Returns:**
        ``{"transactions": [...], "detail_level": "compact"}``; rows include ``transaction_type``.

    **Notes:**
        This uses public QuickBooks Online Accounting API entities. It is not a raw bank-feed/FITID API.

    **Example:**
        ``get_bank_credits(start_date=\"2026-04-30\", end_date=\"2026-04-30\", account_id=\"35\")``
    """
    service = _get_qb_service(ctx)
    max_results = coerce_int(max_results, default=100, minimum=1, maximum=1000)
    start_position = coerce_int(start_position, default=1, minimum=1)
    start_d = _parse_date(start_date) if start_date else None
    end_d = _parse_date(end_date) if end_date else None
    try:
        entries = service.get_bank_credit_transactions(
            start_date=start_d,
            end_date=end_d,
            account_id=account_id,
            external_transaction_id=external_transaction_id,
            max_results=max_results,
            start_position=start_position,
        )
        return structured_result(
            with_response_meta(
                {
                    "transactions": [
                        {
                            "id": e.id,
                            "external_transaction_id": e.id,
                            "date": e.date.isoformat(),
                            "amount": e.amount,
                            "transaction_type": e.transaction_type,
                            "bank_account_id": e.bank_account_id,
                            "bank_account_name": e.bank_account_name,
                            "account_id": e.bank_account_id,
                            "account_name": e.bank_account_name,
                            "description": e.description,
                            "counterparty_name": e.counterparty_name,
                            "category": e.category,
                            "doc_number": e.doc_number,
                            "payment_ref_num": e.payment_ref_num,
                            "linked_txn_ids": e.linked_txn_ids,
                        }
                        for e in entries
                    ],
                    "detail_level": "compact",
                },
                tool="quickbooks_get_bank_credits",
                pagination=build_pagination_meta(limit=max_results, offset=start_position - 1),
                data_from="transactions",
            )
        )
    except Exception as e:
        logger.exception("get_bank_credits failed")
        return structured_result(
            tool_error(
                "quickbooks_api_error",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later, verify QuickBooks API credentials, or check the connected company context.",
            )
        )


@mcp.tool(structured_output=False)
def get_bank_balances(ctx: Context[ServerSession, AppContext] = None) -> CallToolResult:
    """Get current balances for QuickBooks bank accounts.

    **Use when:**
        You need a quick balance snapshot across bank accounts.

    **Args:**
        (none) — uses lifespan context for the QuickBooks client.

    **Returns:**
        ``{"data": [{account_id, account_name, balance}], "detail_level": "compact", "meta": {...}}``.

    **Notes:**
        Balances are CurrentBalance, not historical as-of. Use ``list_bank_accounts`` for type/currency metadata.

    **Errors:**
        ``{"error", "details"}`` on failure.

    **Example:**
        ``get_bank_balances()``
    """
    service = _get_qb_service(ctx)
    try:
        accounts = service.get_bank_accounts()
        return structured_result(
            with_response_meta(
                {
                    "data": [
                        {
                            "account_id": a.id,
                            "account_name": a.name,
                            "balance": a.current_balance,
                        }
                        for a in accounts
                    ],
                    "detail_level": "compact",
                },
                tool="quickbooks_get_bank_balances",
            )
        )
    except Exception as e:
        logger.exception("get_bank_balances failed")
        return structured_result(
            tool_error(
                "quickbooks_api_error",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify QuickBooks API credentials.",
            )
        )


@mcp.tool(structured_output=False)
def get_company_context(ctx: Context[ServerSession, AppContext] = None) -> CallToolResult:
    """Return the connected QuickBooks company context for diagnostics.

    **Use when:**
        Bank/account queries unexpectedly return no rows and you need to verify the active realm/company.

    **Returns:**
        ``{"realm_id", "environment", "company_name", "legal_name", "country", "email", "detail_level"}``.

    **Example:**
        ``get_company_context()``
    """
    service = _get_qb_service(ctx)
    try:
        info = service.get_company_info()
        return structured_result(
            with_response_meta(
                {
                    "realm_id": info.realm_id,
                    "environment": info.environment,
                    "company_name": info.company_name,
                    "legal_name": info.legal_name,
                    "country": info.country,
                    "email": info.email,
                    "detail_level": "compact",
                },
                tool="quickbooks_get_company_context",
            )
        )
    except Exception as e:
        logger.exception("get_company_context failed")
        return structured_result(
            tool_error(
                "quickbooks_api_error",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify QuickBooks API credentials and realm_id.",
            )
        )


# ---------------------------------------------------------------------------
# Machine-readable tool metadata
# ---------------------------------------------------------------------------

TOOL_METADATA: dict = {
    "list_bank_accounts": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "proxy_name": "quickbooks_list_bank_accounts",
    },
    "get_bank_transactions_in": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
        "proxy_name": "quickbooks_get_bank_transactions_in",
    },
    "get_bank_transactions_out": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
        "proxy_name": "quickbooks_get_bank_transactions_out",
    },
    "get_bank_credits": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": True,
        "pagination_mode": "offset",
        "requires_real_fixture": False,
        "proxy_name": "quickbooks_get_bank_credits",
    },
    "get_bank_balances": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "proxy_name": "quickbooks_get_bank_balances",
    },
    "get_company_context": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "proxy_name": "quickbooks_get_company_context",
    },
    "get_bank_account_by_name": {
        "read_only": True,
        "mutation": False,
        "idempotent": True,
        "supports_pagination": False,
        "requires_real_fixture": False,
        "resolver": True,
        "primary_param": "query",
        "proxy_name": "quickbooks_get_bank_account_by_name",
    },
}
