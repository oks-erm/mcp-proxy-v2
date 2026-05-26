"""Contract tests: resolver payloads and envelope normalization for MCP tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from util import (
    assert_error_dict_normalized,
    assert_meta_tool,
    assert_success_dict,
    load_mcp_server,
)


@pytest.fixture
def absence_mod():
    return load_mcp_server("absence-mcp")


@pytest.fixture
def guesty_mod():
    return load_mcp_server("guesty-mcp")


@pytest.fixture
def breezeway_mod():
    return load_mcp_server("breezeway-mcp")


@pytest.fixture
def zendesk_mod():
    return load_mcp_server("zendesk-mcp")


@pytest.fixture
def pipedrive_mod():
    return load_mcp_server("pipedrive-mcp")


@pytest.fixture
def quickbooks_mod():
    return load_mcp_server("quickbooks-mcp")


@pytest.fixture
def n8n_mod():
    return load_mcp_server("n8n-mcp")


@pytest.mark.asyncio
async def test_absence_list_users_compact_response_shape(absence_mod):
    fake = {
        "data": [
            {
                "id": "1",
                "firstName": "A",
                "lastName": "B",
                "name": "A B",
                "email": "a@b.c",
                "status": 1,
                "heavy_secret": "drop_me",
            }
        ],
        "totalCount": 1,
    }
    with patch.object(absence_mod, "post_json", lambda *a, **k: fake):
        r = await absence_mod.absence_list_users(skip=0, limit=50, exclude_inactive=True, detail_level="compact")
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="absence_list_users")
    assert p.get("detail_level") == "compact"
    row = p["data"][0]
    assert "heavy_secret" not in row
    assert set(row.keys()) <= {"id", "firstName", "lastName", "name", "email", "status"}


@pytest.mark.asyncio
async def test_guesty_search_listings_compact_response_shape(guesty_mod):
    svc = MagicMock()
    svc.search_listings.return_value = {
        "results": [
            {
                "_id": "1",
                "title": "Hi",
                "nickname": None,
                "address": {"city": "Lisbon", "country": "PT"},
                "isListed": True,
                "privateBlob": {"k": "v"},
            }
        ],
        "count": 1,
    }
    with patch.object(guesty_mod, "_get_service", return_value=svc):
        r = await guesty_mod.search_listings(
            limit=5, skip=0, fetch_all=False, sort="_id", compact=True, detail_level="compact"
        )
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="guesty_search_listings")
    assert isinstance(p.get("data"), list), p
    assert "results" not in p
    row = p["data"][0]
    assert "privateBlob" not in row
    assert "_id" in row


@pytest.mark.asyncio
async def test_guesty_find_listing_empty_query_normalized_error(guesty_mod):
    r = await guesty_mod.find_listing("", 10, "compact")
    assert_error_dict_normalized(r.structuredContent)


@pytest.mark.asyncio
async def test_guesty_find_listing_count_data(guesty_mod):
    svc = MagicMock()
    svc.search_listings.return_value = {
        "results": [
            {"_id": "a", "title": "Beach", "nickname": "bee", "address": {}, "isListed": True},
            {"_id": "b", "title": "Other", "nickname": None, "address": {}, "isListed": False},
        ],
        "count": 2,
    }
    with patch.object(guesty_mod, "_get_service", return_value=svc):
        r = await guesty_mod.find_listing("beach", 10, "compact")
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="guesty_find_listing")
    assert p["detail_level"] == "compact"
    assert p["count"] >= 1
    assert p["data"][0]["title"] == "Beach"
    assert "results" not in p


@pytest.mark.asyncio
async def test_guesty_find_reservation_count_data(guesty_mod):
    svc = MagicMock()
    svc.search_reservations.return_value = {
        "results": [
            {
                "_id": "r1",
                "confirmationCode": "X",
                "listingId": "L1",
                "status": "confirmed",
                "checkIn": "2026-01-01",
                "checkOut": "2026-01-05",
                "guest": {"fullName": "Jane Doe"},
            }
        ],
        "count": 1,
    }
    with patch.object(guesty_mod, "_get_service", return_value=svc):
        r = await guesty_mod.find_reservation(confirmation_code="X", limit=5)
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="guesty_find_reservation")
    assert p["count"] == 1
    assert p["data"][0]["guestName"] == "Jane Doe"


@pytest.mark.asyncio
async def test_breezeway_find_property_count_data(breezeway_mod):
    page = {
        "results": [
            {
                "id": 9,
                "name": "Beach House",
                "display": "BH",
                "city": "Nice",
                "state": None,
                "country": "FR",
                "reference_property_id": "p1",
                "reference_external_property_id": "ext-1",
                "status": "active",
            }
        ],
        "total_pages": 1,
    }
    with patch.object(breezeway_mod, "_get_client") as gc:
        client = MagicMock()
        client.list_properties_page.return_value = page
        gc.return_value = client
        r = await breezeway_mod.breezeway_find_property_by_name_or_external_id("beach", 5, 1, 100)
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="breezeway_find_property_by_name_or_external_id")
    assert p["detail_level"] == "compact"
    assert p["count"] == 1
    assert p["data"][0]["id"] == 9
    assert "wifi" not in str(p).lower()


@pytest.mark.asyncio
async def test_zendesk_get_ticket_flat_shape(zendesk_mod):
    svc = MagicMock()
    svc.get_ticket.return_value = {
        "ticket": {
            "id": 42,
            "subject": "Hi",
            "status": "open",
            "priority": "normal",
            "requester_id": 1,
            "assignee_id": None,
            "organization_id": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "tags": ["a"],
        }
    }
    with patch.object(zendesk_mod, "_get_service", return_value=svc):
        r = await zendesk_mod.zendesk_get_ticket(42, None, "summary")
    p = assert_success_dict(r.structuredContent)
    assert "ticket" not in p
    assert isinstance(p.get("data"), dict), p
    assert p["data"].get("id") == 42
    assert p["data"].get("detail_level") == "summary"


@pytest.mark.asyncio
async def test_zendesk_list_views_shape(zendesk_mod):
    svc = MagicMock()
    svc.list_views.return_value = {
        "views": [{"id": 1, "title": "Open"}],
        "count": 1,
        "next_page": "https://example.zendesk.com/api/v2/views.json?page=2",
    }
    with patch.object(zendesk_mod, "_get_service", return_value=svc):
        r = await zendesk_mod.zendesk_list_views(active_only=False, access="shared")
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="zendesk_list_views")
    assert p["data"][0]["id"] == 1
    assert p["meta"]["pagination"]["has_more"] is True


@pytest.mark.asyncio
async def test_zendesk_list_view_tickets_masked(zendesk_mod):
    svc = MagicMock()
    svc.list_view_tickets.return_value = {
        "tickets": [
            {
                "id": 9,
                "subject": "Hi",
                "status": "open",
                "created_at": "t1",
                "updated_at": "t2",
                "junk": 1,
            }
        ],
        "next_page": None,
    }
    with patch.object(zendesk_mod, "_get_service", return_value=svc):
        r = await zendesk_mod.zendesk_list_view_tickets(view_id=25, detail_level="compact")
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="zendesk_list_view_tickets")
    assert p["data"][0]["id"] == 9
    assert "junk" not in p["data"][0]


@pytest.mark.asyncio
async def test_zendesk_get_view_ticket_counts_shape(zendesk_mod):
    svc = MagicMock()
    svc.get_view_ticket_counts.return_value = {
        "view_counts": [{"view_id": 25, "value": 3, "pretty": "3", "fresh": True}],
    }
    with patch.object(zendesk_mod, "_get_service", return_value=svc):
        r = await zendesk_mod.zendesk_get_view_ticket_counts([25, 25])
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="zendesk_get_view_ticket_counts")
    assert p["count"] == 1
    assert p["data"][0]["view_id"] == 25


@pytest.mark.asyncio
async def test_zendesk_find_ticket_by_reservation_id_shape(zendesk_mod):
    svc = MagicMock()
    svc.search_custom_object_records.return_value = {"custom_object_records": []}
    svc.search_tickets.return_value = [
        {"id": 7, "subject": "S", "status": "new", "created_at": "t1", "updated_at": "t2", "junk": 1},
    ]
    with patch.object(zendesk_mod, "_get_service", return_value=svc):
        r = await zendesk_mod.zendesk_find_ticket_by_reservation_id("ABC", 10, 100, None)
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="zendesk_find_ticket_by_reservation_id")
    assert "data" in p and p["detail_level"] == "compact"
    assert p["data"][0]["id"] == 7
    assert "junk" not in p["data"][0]


@pytest.mark.asyncio
async def test_pipedrive_find_deal_count_data(pipedrive_mod):
    body = {
        "success": True,
        "data": {
            "items": [
                {"item": {"id": 1, "title": "Exact", "status": "open", "pipeline_id": 2, "stage_id": 3}},
                {"item": {"id": 2, "title": "Exact", "status": "open"}},
            ]
        },
    }
    with patch.object(pipedrive_mod, "get_v2_json", lambda *a, **k: body):
        r = await pipedrive_mod.pipedrive_find_deal_by_title_or_exact_name("exact", None, None, None, 10, None, None)
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="pipedrive_find_deal_by_title_or_exact_name")
    assert p["count"] == 2
    assert p["data"][0]["title"] == "Exact"


@pytest.mark.asyncio
async def test_quickbooks_get_bank_account_by_name_count_data(quickbooks_mod):
    acc = MagicMock()
    acc.id = "1"
    acc.name = "Operating"
    acc.account_type = "Bank"
    acc.account_sub_type = "Checking"
    acc.currency_code = "EUR"
    svc = MagicMock()
    svc.get_bank_accounts.return_value = [acc]
    with patch.object(quickbooks_mod, "_get_qb_service", return_value=svc):
        r = quickbooks_mod.get_bank_account_by_name("Operating", 5, ctx=None)
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="quickbooks_get_bank_account_by_name")
    assert p["count"] >= 1
    assert "current_balance" not in p["data"][0]


@pytest.mark.asyncio
async def test_n8n_find_workflow_by_name_count_data(n8n_mod):
    payload = {
        "data": [
            {"id": "w1", "name": "Alpha workflow", "active": True, "isArchived": False, "updatedAt": "t"},
        ],
        "nextCursor": None,
    }

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        async def get(self, path, params=None):
            assert path == "/workflows"
            return FakeResp()

    class ClientCM:
        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, *args):
            return False

    with patch.object(n8n_mod, "get_client", lambda: ClientCM()):
        r = await n8n_mod.n8n_find_workflow_by_name("alpha", 10, None, 2, True)
    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="n8n_find_workflow_by_name")
    assert p["count"] == 1
    assert p["data"][0]["name"] == "Alpha workflow"
    assert "tags" not in p["data"][0]


def test_sql_gateway_get_schema_single_content():
    mod = load_mcp_server("sql-gateway")
    r = mod.get_schema()
    p = r.structuredContent
    if isinstance(p, dict) and "error" in p:
        pytest.skip(f"schema unavailable: {p}")
    assert_success_dict(p)
    assert list(p.keys()).count("content") == 1
    body = p["content"]
    assert isinstance(body, str) and len(body) > 0
    encoded = json.dumps(p, ensure_ascii=True)
    assert encoded.count('"content"') == 1
    for k, v in p.items():
        if k == "content" or not isinstance(v, str):
            continue
        if len(v) > 500 and v == body:
            pytest.fail("schema prose duplicated in another top-level string field")
