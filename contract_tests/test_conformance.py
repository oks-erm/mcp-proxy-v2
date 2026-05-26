"""MCP conformance test suite.

Checks per resolver tool:
  1. Arg conformance: ``query=`` works as a named kwarg — agents can call by param name without
     knowing positional order (this is the production agent call pattern).
  2. Meta shape: ``meta.tool`` equals the proxied tool name exposed to agents (e.g.
     ``guesty_find_listing``, not the raw function name ``find_listing``).
  3. Data shape: resolver success payloads have ``count`` (int) and ``data`` (list) at the top level.
  4. Error classification: validation errors have ``retryable=False`` + ``cause="validation"``;
     not-found has ``retryable=False`` + ``cause="not_found"``;
     upstream errors have ``retryable=True`` + ``cause="upstream_error"``.
  5. Pagination contract: paginated tools emit only canonical ``meta.pagination`` keys.

All tests mock the upstream API call so they run offline in CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from util import (
    assert_error_dict_normalized,
    assert_meta_tool,
    assert_offset_pagination,
    assert_pagination_shape,
    assert_success_dict,
    load_mcp_server,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def breezeway_mod():
    return load_mcp_server("breezeway-mcp")


@pytest.fixture(scope="module")
def guesty_mod():
    return load_mcp_server("guesty-mcp")


@pytest.fixture(scope="module")
def n8n_mod():
    return load_mcp_server("n8n-mcp")


@pytest.fixture(scope="module")
def pipedrive_mod():
    return load_mcp_server("pipedrive-mcp")


@pytest.fixture(scope="module")
def quickbooks_mod():
    return load_mcp_server("quickbooks-mcp")


@pytest.fixture(scope="module")
def utils_mod():
    return load_mcp_server("utils-mcp")


# ---------------------------------------------------------------------------
# 1 + 2 + 3: Arg conformance, meta.tool, and data shape for resolver tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breezeway_find_property_arg_conformance_and_meta(breezeway_mod):
    """Calling with query= named kwarg (agent pattern) must work and meta.tool must match proxy name."""
    page = {
        "results": [
            {
                "id": 1,
                "name": "Test Property",
                "display": "TP",
                "city": "Lisbon",
                "state": None,
                "country": "PT",
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
        # Call with NAMED kwarg — this is how agents call it based on documented parameter names
        r = await breezeway_mod.breezeway_find_property_by_name_or_external_id(query="Test", limit=5)

    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="breezeway_find_property_by_name_or_external_id")
    assert isinstance(p.get("count"), int), p
    assert isinstance(p.get("data"), list), p
    assert p["detail_level"] == "compact"


@pytest.mark.asyncio
async def test_guesty_find_listing_arg_conformance_and_meta(guesty_mod):
    """find_listing: query= named kwarg must work; meta.tool must be guesty_find_listing (proxy name)."""
    svc = MagicMock()
    svc.search_listings.return_value = {
        "results": [{"_id": "abc", "title": "Ocean View", "nickname": "OV", "address": {}, "isListed": True}],
        "count": 1,
    }
    with patch.object(guesty_mod, "_get_service", return_value=svc):
        r = await guesty_mod.find_listing(query="Ocean View")

    p = assert_success_dict(r.structuredContent)
    # The proxied name seen by agents is guesty_find_listing (not find_listing)
    assert_meta_tool(p, expected_tool="guesty_find_listing")
    assert isinstance(p.get("count"), int), p
    assert isinstance(p.get("data"), list), p
    assert p["detail_level"] == "compact"


@pytest.mark.asyncio
async def test_n8n_find_workflow_arg_conformance_and_meta(n8n_mod):
    """n8n_find_workflow_by_name: query= named kwarg must work; meta.tool must match."""
    payload = {
        "data": [{"id": "w1", "name": "My Workflow", "active": True, "isArchived": False, "updatedAt": "t"}],
        "nextCursor": None,
    }

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        async def get(self, path, params=None):
            return FakeResp()

    class ClientCM:
        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, *args):
            return False

    with patch.object(n8n_mod, "get_client", lambda: ClientCM()):
        r = await n8n_mod.n8n_find_workflow_by_name(query="My Workflow")

    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="n8n_find_workflow_by_name")
    assert isinstance(p.get("count"), int), p
    assert isinstance(p.get("data"), list), p
    assert p["detail_level"] == "compact"


@pytest.mark.asyncio
async def test_pipedrive_find_deal_arg_conformance_and_meta(pipedrive_mod):
    """pipedrive_find_deal_by_title_or_exact_name: query= named kwarg must work; meta.tool must match."""
    body = {
        "success": True,
        "data": {"items": [{"item": {"id": 42, "title": "Deal Alpha", "status": "open"}}]},
    }
    with patch.object(pipedrive_mod, "get_v2_json", lambda *a, **k: body):
        r = await pipedrive_mod.pipedrive_find_deal_by_title_or_exact_name(query="Deal Alpha")

    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="pipedrive_find_deal_by_title_or_exact_name")
    assert isinstance(p.get("count"), int), p
    assert isinstance(p.get("data"), list), p
    assert p["detail_level"] == "compact"


def test_quickbooks_get_bank_account_arg_conformance_and_meta(quickbooks_mod):
    """get_bank_account_by_name: query= named kwarg must work; meta.tool must be quickbooks_get_bank_account_by_name."""
    acc = MagicMock()
    acc.id = "10"
    acc.name = "Operations"
    acc.account_type = "Bank"
    acc.account_sub_type = "Checking"
    acc.currency_code = "EUR"
    svc = MagicMock()
    svc.get_bank_accounts.return_value = [acc]
    with patch.object(quickbooks_mod, "_get_qb_service", return_value=svc):
        r = quickbooks_mod.get_bank_account_by_name(query="Operations")

    p = assert_success_dict(r.structuredContent)
    # Via mcp-proxy this tool is exposed as quickbooks_get_bank_account_by_name
    assert_meta_tool(p, expected_tool="quickbooks_get_bank_account_by_name")
    assert isinstance(p.get("count"), int), p
    assert isinstance(p.get("data"), list), p
    assert p["detail_level"] == "compact"


def test_utils_find_travel_location_arg_conformance_and_meta(utils_mod):
    payload = {
        "data": [
            {
                "name": "Porto",
                "iataCode": "OPO",
                "subType": "CITY",
                "address": {"countryCode": "PT", "stateCode": "PT-13"},
                "geoCode": {"latitude": 41.14961, "longitude": -8.61099},
            }
        ],
        "meta": {"links": {}},
    }
    client = MagicMock()
    client.search_locations.return_value = payload
    with patch.object(utils_mod, "get_amadeus_client", return_value=client):
        r = utils_mod.find_travel_location(query="Porto", limit=5)

    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="utils_find_travel_location")
    assert isinstance(p.get("count"), int), p
    assert isinstance(p.get("data"), list), p
    assert p["detail_level"] == "compact"


# ---------------------------------------------------------------------------
# 4: Error classification — validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_error_classification_breezeway(breezeway_mod):
    """Empty query must yield validation_error with cause=validation, retryable=False."""
    r = await breezeway_mod.breezeway_find_property_by_name_or_external_id(query="")
    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "validation_error"
    assert p["cause"] == "validation"
    assert p["retryable"] is False


@pytest.mark.asyncio
async def test_validation_error_classification_guesty(guesty_mod):
    r = await guesty_mod.find_listing(query="")
    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "validation_error"
    assert p["cause"] == "validation"
    assert p["retryable"] is False


@pytest.mark.asyncio
async def test_validation_error_classification_n8n(n8n_mod):
    r = await n8n_mod.n8n_find_workflow_by_name(query="")
    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "validation_error"
    assert p["cause"] == "validation"
    assert p["retryable"] is False


@pytest.mark.asyncio
async def test_validation_error_classification_pipedrive(pipedrive_mod):
    # pipedrive rejects queries that are too short for the search API
    r = await pipedrive_mod.pipedrive_find_deal_by_title_or_exact_name(query="x")
    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "validation_error"
    assert p["cause"] == "validation"
    assert p["retryable"] is False


def test_validation_error_classification_quickbooks(quickbooks_mod):
    r = quickbooks_mod.get_bank_account_by_name(query="", ctx=None)
    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "validation_error"
    assert p["cause"] == "validation"
    assert p["retryable"] is False


def test_validation_error_classification_utils(utils_mod):
    r = utils_mod.find_travel_location(query="")
    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "validation_error"
    assert p["cause"] == "validation"
    assert p["retryable"] is False


# ---------------------------------------------------------------------------
# 4: Error classification — upstream errors must be retryable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upstream_error_is_retryable_breezeway(breezeway_mod):
    with patch.object(breezeway_mod, "_get_client") as gc:
        client = MagicMock()
        client.list_properties_page.side_effect = RuntimeError("upstream down")
        gc.return_value = client
        r = await breezeway_mod.breezeway_find_property_by_name_or_external_id(query="SomeProp")

    p = assert_error_dict_normalized(r.structuredContent)
    assert p["cause"] == "upstream_error"
    assert p["retryable"] is True


@pytest.mark.asyncio
async def test_upstream_error_is_retryable_guesty(guesty_mod):
    svc = MagicMock()
    svc.search_listings.return_value = None
    with patch.object(guesty_mod, "_get_service", return_value=svc):
        r = await guesty_mod.find_listing(query="SomeListing")

    p = assert_error_dict_normalized(r.structuredContent)
    assert p["cause"] == "upstream_error"
    assert p["retryable"] is True


# ---------------------------------------------------------------------------
# 5: Pagination contract — canonical meta.pagination keys only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breezeway_list_users_pagination_contract(breezeway_mod):
    """breezeway_list_users must emit canonical offset pagination in meta.pagination."""
    users = [{"id": 1, "first_name": "A", "last_name": "B", "email": "a@b.com", "role": "admin"}]
    with patch.object(breezeway_mod, "_get_client") as gc:
        client = MagicMock()
        client.list_all_users.return_value = users * 5
        gc.return_value = client
        r = await breezeway_mod.breezeway_list_users(limit=2, offset=0)

    p = assert_success_dict(r.structuredContent)
    assert_offset_pagination(p)
    pag = p["meta"]["pagination"]
    assert pag["limit"] == 2
    assert pag["offset"] == 0
    assert pag["has_more"] is True
    assert pag["next_offset"] == 2
    assert pag["total_count"] == 5
    # Must NOT contain non-standard keys
    from util import _VALID_PAGINATION_KEYS

    assert set(pag.keys()) <= _VALID_PAGINATION_KEYS


@pytest.mark.asyncio
async def test_n8n_get_workflows_pagination_contract(n8n_mod):
    """n8n_get_workflows must emit canonical cursor pagination in meta.pagination."""
    payload = {
        "data": [{"id": "w1", "name": "Wf", "active": True, "isArchived": False, "updatedAt": "t"}],
        "nextCursor": "tok-xyz",
    }

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        async def get(self, path, params=None):
            return FakeResp()

    class ClientCM:
        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, *a):
            return False

    with patch.object(n8n_mod, "get_client", lambda: ClientCM()):
        r = await n8n_mod.n8n_get_workflows(limit=1)

    p = assert_success_dict(r.structuredContent)
    assert_pagination_shape(p)
    pag = p["meta"]["pagination"]
    assert pag.get("limit") == 1
    assert pag.get("has_more") is True
    assert pag.get("next_cursor") == "tok-xyz"
    assert "offset" not in pag
    assert "page" not in pag  # legacy key must not leak into canonical meta.pagination
