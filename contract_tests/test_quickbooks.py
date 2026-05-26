from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from util import (
    assert_error_dict,
    assert_meta_tool,
    assert_success_dict,
    load_mcp_server,
)


def test_get_bank_account_by_name_requires_query():
    mod = load_mcp_server("quickbooks-mcp")
    r = mod.get_bank_account_by_name(query="   ", limit=5, ctx=None)
    assert_error_dict(r.structuredContent)
    assert r.structuredContent["error"] == "validation_error"


def test_get_bank_credits_returns_data_alias_and_canonical_pagination():
    mod = load_mcp_server("quickbooks-mcp")
    entry = SimpleNamespace(
        id="dep-1",
        date=date(2026, 4, 30),
        amount=18538.86,
        transaction_type="deposit",
        bank_account_id="35",
        bank_account_name="Santander Central",
        description="Airbnb payout",
        counterparty_name=None,
        category="Accommodation revenue",
        doc_number="DOC-1",
        payment_ref_num=None,
        linked_txn_ids=["pay-1"],
    )
    svc = MagicMock()
    svc.get_bank_credit_transactions.return_value = [entry]

    with patch.object(mod, "_get_qb_service", return_value=svc):
        r = mod.get_bank_credits(
            start_date="2026-04-30",
            end_date="2026-04-30",
            account_id="35",
            external_transaction_id="dep-1",
            max_results=50,
            start_position=1,
            ctx=None,
        )

    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="quickbooks_get_bank_credits")
    assert p["data"] is p["transactions"]
    assert p["transactions"][0]["transaction_type"] == "deposit"
    assert p["transactions"][0]["account_id"] == "35"
    assert p["transactions"][0]["linked_txn_ids"] == ["pay-1"]
    assert p["meta"]["pagination"] == {"limit": 50, "offset": 0}
    svc.get_bank_credit_transactions.assert_called_once()


def test_get_company_context_returns_realm_metadata():
    mod = load_mcp_server("quickbooks-mcp")
    svc = MagicMock()
    svc.get_company_info.return_value = SimpleNamespace(
        realm_id="123",
        environment="production",
        company_name="HostWise",
        legal_name="HostWise Lda",
        country="PT",
        email="accounting@example.com",
    )

    with patch.object(mod, "_get_qb_service", return_value=svc):
        r = mod.get_company_context(ctx=None)

    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="quickbooks_get_company_context")
    assert p["realm_id"] == "123"
    assert p["company_name"] == "HostWise"


def test_quickbooks_client_raises_on_api_error_instead_of_empty_list():
    load_mcp_server("quickbooks-mcp")
    from quickbooks_client import QuickBooksAPIError, QuickBooksService

    svc = QuickBooksService()
    svc._initialized = True
    svc.credentials = {"access_token": "token"}
    svc.base_url = "https://quickbooks.example/v3/company/123"

    response = MagicMock()
    response.status_code = 500
    response.text = "boom"

    with patch("quickbooks_client.requests.request", return_value=response):
        try:
            svc.get_bank_accounts()
        except QuickBooksAPIError as exc:
            assert "500" in str(exc)
        else:
            raise AssertionError("expected QuickBooksAPIError")


def test_money_out_uses_purchase_records_for_quickbooks_expenses():
    load_mcp_server("quickbooks-mcp")
    from quickbooks_client import BankAccount, QuickBooksService

    svc = QuickBooksService()
    svc.get_bank_accounts = MagicMock(
        return_value=[
            BankAccount(
                id="35",
                name="Santander Central",
                account_type="Bank",
                account_sub_type=None,
                current_balance=None,
                currency_code="EUR",
            )
        ]
    )
    svc.get_transfers = MagicMock(return_value=[])

    queries = []

    def fake_query_response(query):
        queries.append(query)
        if "FROM Purchase" in query:
            return {
                "Purchase": [
                    {
                        "Id": "purchase-1",
                        "TxnDate": "2026-04-15",
                        "TotalAmt": "42.50",
                        "AccountRef": {"value": "35", "name": "Santander Central"},
                        "EntityRef": {"value": "vendor-1", "name": "Rent Vendor"},
                        "PrivateNote": "April rent",
                        "Line": [
                            {
                                "Description": "Office rent",
                                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "600", "name": "Rent"}},
                            }
                        ],
                    }
                ]
            }
        if "FROM BillPayment" in query:
            return {}
        raise AssertionError(f"unexpected query: {query}")

    svc._query_response = MagicMock(side_effect=fake_query_response)

    entries = svc.get_bank_money_out_transactions(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        from_account_id="35",
    )

    assert len(entries) == 1
    assert entries[0].id == "purchase-1"
    assert entries[0].amount == 42.5
    assert entries[0].transaction_type == "purchase"
    assert entries[0].bank_account_id == "35"
    assert entries[0].payee_name == "Rent Vendor"
    assert entries[0].category == "Rent"
    assert any("FROM Purchase" in q for q in queries)
    assert not any("FROM Expense" in q for q in queries)


def test_money_out_includes_bank_paid_bill_payments():
    load_mcp_server("quickbooks-mcp")
    from quickbooks_client import BankAccount, QuickBooksService

    svc = QuickBooksService()
    svc.get_bank_accounts = MagicMock(
        return_value=[
            BankAccount(
                id="35",
                name="Santander Central",
                account_type="Bank",
                account_sub_type=None,
                current_balance=None,
                currency_code="EUR",
            )
        ]
    )
    svc.get_transfers = MagicMock(return_value=[])

    def fake_query_response(query):
        if "FROM Purchase" in query:
            return {}
        if "FROM BillPayment" in query:
            return {
                "BillPayment": [
                    {
                        "Id": "billpay-1",
                        "TxnDate": "2026-04-20",
                        "TotalAmt": "125.75",
                        "DocNumber": "BP-42",
                        "PayType": "Check",
                        "VendorRef": {"value": "vendor-2", "name": "Utilities Vendor"},
                        "CheckPayment": {"BankAccountRef": {"value": "35", "name": "Santander Central"}},
                    },
                    {
                        "Id": "billpay-other-bank",
                        "TxnDate": "2026-04-21",
                        "TotalAmt": "999.00",
                        "PayType": "Check",
                        "CheckPayment": {"BankAccountRef": {"value": "99", "name": "Other Bank"}},
                    },
                ]
            }
        raise AssertionError(f"unexpected query: {query}")

    svc._query_response = MagicMock(side_effect=fake_query_response)

    entries = svc.get_bank_money_out_transactions(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        from_account_id="35",
    )

    assert len(entries) == 1
    assert entries[0].id == "billpay-1"
    assert entries[0].amount == 125.75
    assert entries[0].transaction_type == "bill_payment"
    assert entries[0].bank_account_id == "35"
    assert entries[0].private_note == "BP-42"
    assert entries[0].payee_name == "Utilities Vendor"


def test_transfers_filter_accounts_in_client_side_not_quickbooks_query():
    load_mcp_server("quickbooks-mcp")
    from quickbooks_client import QuickBooksService

    svc = QuickBooksService()
    queries = []

    def fake_query_response(query):
        queries.append(query)
        return {
            "Transfer": [
                {
                    "Id": "transfer-1",
                    "TxnDate": "2026-04-10",
                    "Amount": "15.00",
                    "FromAccountRef": {"value": "35", "name": "Santander Central"},
                    "ToAccountRef": {"value": "29", "name": "Santander PLH"},
                },
                {
                    "Id": "transfer-2",
                    "TxnDate": "2026-04-11",
                    "Amount": "30.00",
                    "FromAccountRef": {"value": "99", "name": "Other Bank"},
                    "ToAccountRef": {"value": "29", "name": "Santander PLH"},
                },
            ]
        }

    svc._query_response = MagicMock(side_effect=fake_query_response)

    entries = svc.get_transfers(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        from_account_id="35",
        to_account_id="29",
    )

    assert len(entries) == 1
    assert entries[0].id == "transfer-1"
    assert entries[0].from_account_id == "35"
    assert entries[0].to_account_id == "29"
    assert queries
    assert not any("FromAccountRef" in q or "ToAccountRef" in q for q in queries)


def test_deposits_filter_account_in_client_side_not_quickbooks_query():
    load_mcp_server("quickbooks-mcp")
    from quickbooks_client import QuickBooksService

    svc = QuickBooksService()
    queries = []

    def fake_query_response(query):
        queries.append(query)
        return {
            "Deposit": [
                {
                    "Id": "deposit-1",
                    "TxnDate": "2026-04-10",
                    "TotalAmt": "70.00",
                    "DepositToAccountRef": {"value": "35", "name": "Santander Central"},
                    "Line": [
                        {
                            "Description": "Stripe payout",
                            "DepositLineDetail": {"AccountRef": {"value": "400", "name": "OTAs"}},
                        }
                    ],
                },
                {
                    "Id": "deposit-2",
                    "TxnDate": "2026-04-11",
                    "TotalAmt": "80.00",
                    "DepositToAccountRef": {"value": "99", "name": "Other Bank"},
                    "Line": [],
                },
            ]
        }

    svc._query_response = MagicMock(side_effect=fake_query_response)

    entries = svc.get_bank_transactions(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        account_id="35",
    )

    assert len(entries) == 1
    assert entries[0].id == "deposit-1"
    assert entries[0].amount == 70.0
    assert entries[0].account_id == "35"
    assert queries
    assert not any("DepositToAccountRef" in q for q in queries)


def test_bank_credits_filter_incoming_transfers_client_side_not_quickbooks_query():
    load_mcp_server("quickbooks-mcp")
    from quickbooks_client import QuickBooksService

    svc = QuickBooksService()
    queries = []

    def fake_query_response(query):
        queries.append(query)
        if "FROM Deposit" in query or "FROM Payment" in query or "FROM SalesReceipt" in query:
            return {}
        if "FROM Transfer" in query:
            return {
                "Transfer": [
                    {
                        "Id": "transfer-in",
                        "TxnDate": "2026-04-12",
                        "Amount": "25.00",
                        "FromAccountRef": {"value": "29", "name": "Santander PLH"},
                        "ToAccountRef": {"value": "35", "name": "Santander Central"},
                    },
                    {
                        "Id": "transfer-other",
                        "TxnDate": "2026-04-13",
                        "Amount": "50.00",
                        "FromAccountRef": {"value": "29", "name": "Santander PLH"},
                        "ToAccountRef": {"value": "99", "name": "Other Bank"},
                    },
                ]
            }
        raise AssertionError(f"unexpected query: {query}")

    svc._query_response = MagicMock(side_effect=fake_query_response)

    entries = svc.get_bank_credit_transactions(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        account_id="35",
    )

    assert len(entries) == 1
    assert entries[0].id == "transfer-in"
    assert entries[0].transaction_type == "transfer_in"
    assert entries[0].bank_account_id == "35"
    assert any("FROM Transfer" in q for q in queries)
    assert not any("ToAccountRef" in q for q in queries if "FROM Transfer" in q)
    assert not any("DepositToAccountRef" in q for q in queries)
