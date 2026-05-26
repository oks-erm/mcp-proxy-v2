"""Per-service sample IDs for end-to-end (live) contract verification.

Usage in tests
--------------
    import pytest
    from fixtures import BREEZEWAY_EXTERNAL_RESERVATION_ID

    @pytest.mark.skipif(
        not BREEZEWAY_EXTERNAL_RESERVATION_ID,
        reason="no live Breezeway reservation fixture — set BREEZEWAY_EXTERNAL_RESERVATION_ID env var",
    )
    async def test_breezeway_get_reservation_live(...):
        ...

All IDs default to ``None`` when not configured. Tests that require live IDs must guard with
``pytest.mark.skipif(not FIXTURE_VAR, ...)`` so CI (which has no live credentials) skips them
cleanly.

Setting fixture IDs
-------------------
Set via environment variables at test time (do NOT commit real IDs to source control):

    export BREEZEWAY_EXTERNAL_RESERVATION_ID="your-real-reservation-id"
    pytest global/mcp/contract_tests/ -k live

Or override in a local ``.env.test`` and source it before running pytest.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Breezeway
# ---------------------------------------------------------------------------

#: A real Breezeway external reservation id — used to verify the success path of
#: ``breezeway_get_reservation_by_external_id``.
BREEZEWAY_EXTERNAL_RESERVATION_ID: str | None = os.environ.get("BREEZEWAY_EXTERNAL_RESERVATION_ID")

#: A real Breezeway numeric property id — used to verify ``breezeway_get_property_summary``.
BREEZEWAY_PROPERTY_ID: int | None = (
    int(os.environ["BREEZEWAY_PROPERTY_ID"]) if os.environ.get("BREEZEWAY_PROPERTY_ID") else None
)

# ---------------------------------------------------------------------------
# Guesty
# ---------------------------------------------------------------------------

#: A real Guesty listing title substring — used to verify ``guesty_find_listing`` end-to-end.
GUESTY_LISTING_QUERY: str | None = os.environ.get("GUESTY_LISTING_QUERY")

#: A real Guesty confirmation code — used to verify ``guesty_find_reservation``.
GUESTY_CONFIRMATION_CODE: str | None = os.environ.get("GUESTY_CONFIRMATION_CODE")

# ---------------------------------------------------------------------------
# n8n
# ---------------------------------------------------------------------------

#: A real n8n workflow name substring — used to verify ``n8n_find_workflow_by_name``.
N8N_WORKFLOW_NAME_QUERY: str | None = os.environ.get("N8N_WORKFLOW_NAME_QUERY")

# ---------------------------------------------------------------------------
# Pipedrive
# ---------------------------------------------------------------------------

#: A real Pipedrive deal title substring — used to verify ``pipedrive_find_deal_by_title_or_exact_name``.
PIPEDRIVE_DEAL_QUERY: str | None = os.environ.get("PIPEDRIVE_DEAL_QUERY")

# ---------------------------------------------------------------------------
# QuickBooks
# ---------------------------------------------------------------------------

#: A real QuickBooks bank account name substring — used to verify ``quickbooks_get_bank_account_by_name``.
QUICKBOOKS_BANK_ACCOUNT_QUERY: str | None = os.environ.get("QUICKBOOKS_BANK_ACCOUNT_QUERY")

# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

#: A real Stripe refund id (``re_...``) — used to verify ``stripe_get_refund`` success path.
STRIPE_REFUND_ID: str | None = os.environ.get("STRIPE_REFUND_ID")

#: A real Stripe charge id (``ch_...``) — used to verify ``stripe_get_charge`` success path.
STRIPE_CHARGE_ID: str | None = os.environ.get("STRIPE_CHARGE_ID")

# ---------------------------------------------------------------------------
# Zendesk
# ---------------------------------------------------------------------------

#: A real Zendesk ticket id — used to verify ``zendesk_get_ticket`` success path.
ZENDESK_TICKET_ID: int | None = int(os.environ["ZENDESK_TICKET_ID"]) if os.environ.get("ZENDESK_TICKET_ID") else None

# ---------------------------------------------------------------------------
# SQL Gateway
# ---------------------------------------------------------------------------

#: A real table name in the portal database — used to verify ``lookup_table`` end-to-end.
SQL_GATEWAY_TABLE_NAME: str | None = os.environ.get("SQL_GATEWAY_TABLE_NAME", "reservations_reservation")
