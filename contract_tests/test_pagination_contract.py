"""Contract tests for normalized meta.pagination across MCP tools.

Each test mocks the upstream API call and asserts that:
  - meta.pagination contains only standard keys (limit, offset, cursor, has_more,
    next_cursor, next_offset, total_count)
  - the style-specific field set is correct (offset-based vs cursor-based)
  - has_more / next_offset / next_cursor are derived correctly
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from util import (
    assert_cursor_pagination,
    assert_offset_pagination,
    assert_pagination_shape,
    assert_success_dict,
    load_mcp_server,
)


@pytest.fixture(scope="module")
def absence_mod():
    return load_mcp_server("absence-mcp")


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
def zendesk_mod():
    return load_mcp_server("zendesk-mcp")


# ---------------------------------------------------------------------------
# Absence — offset-based with totalCount → has_more + next_offset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absence_list_users_pagination_shape(absence_mod):
    fake = {
        "data": [{"id": "1", "firstName": "A", "lastName": "B", "name": "A B", "email": "a@b.c", "status": 1}],
        "totalCount": 200,
    }
    with patch.object(absence_mod, "post_json", lambda *a, **k: fake):
        r = await absence_mod.absence_list_users(skip=0, limit=1, exclude_inactive=False)
    p = assert_success_dict(r.structuredContent)
    assert_offset_pagination(p)
    pag = p["meta"]["pagination"]
    assert pag["limit"] == 1
    assert pag["offset"] == 0
    assert pag["has_more"] is True
    assert pag["next_offset"] == 1
    assert pag["total_count"] == 200


@pytest.mark.asyncio
async def test_absence_list_users_pagination_no_more(absence_mod):
    fake = {
        "data": [{"id": "1", "firstName": "A", "lastName": "B", "name": "A B", "email": "a@b.c", "status": 1}],
        "totalCount": 1,
    }
    with patch.object(absence_mod, "post_json", lambda *a, **k: fake):
        r = await absence_mod.absence_list_users(skip=0, limit=50, exclude_inactive=False)
    p = assert_success_dict(r.structuredContent)
    pag = p["meta"]["pagination"]
    assert pag["has_more"] is False
    assert "next_offset" not in pag


@pytest.mark.asyncio
async def test_absence_list_users_pagination_without_total_count(absence_mod):
    """When API does not return totalCount, has_more must be absent (not None)."""
    fake = {
        "data": [{"id": "1", "firstName": "A", "lastName": "B", "name": "A B", "email": "a@b.c", "status": 1}],
    }
    with patch.object(absence_mod, "post_json", lambda *a, **k: fake):
        r = await absence_mod.absence_list_users(skip=0, limit=50, exclude_inactive=False)
    p = assert_success_dict(r.structuredContent)
    assert_offset_pagination(p)
    pag = p["meta"]["pagination"]
    assert "has_more" not in pag
    assert "total_count" not in pag


# ---------------------------------------------------------------------------
# Breezeway — offset-based with full pagination state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breezeway_list_users_pagination_shape(breezeway_mod):
    users = [{"id": 1, "first_name": "X", "last_name": "Y", "email": "x@y.com", "role": "admin"}]
    with patch.object(breezeway_mod, "_get_client") as gc:
        client = MagicMock()
        client.list_all_users.return_value = users * 3  # 3 total, page size 2
        gc.return_value = client
        r = await breezeway_mod.breezeway_list_users(detail_level="compact", limit=2, offset=0)
    p = assert_success_dict(r.structuredContent)
    assert_offset_pagination(p)
    assert isinstance(p.get("data"), list), p
    assert "users" not in p
    pag = p["meta"]["pagination"]
    assert pag["limit"] == 2
    assert pag["offset"] == 0
    assert pag["has_more"] is True
    assert pag["next_offset"] == 2
    assert pag["total_count"] == 3


# ---------------------------------------------------------------------------
# Guesty — offset-based with total from Guesty API count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guesty_get_listings_pagination_offset_shape(guesty_mod):
    svc = MagicMock()
    svc.get_listings.return_value = {
        "results": [{"_id": "a", "title": "T", "isListed": True, "address": {}}],
        "count": 100,
    }
    with patch.object(guesty_mod, "_get_service", return_value=svc):
        r = await guesty_mod.get_listings(limit=1, skip=0, detail_level="compact")
    p = assert_success_dict(r.structuredContent)
    assert_offset_pagination(p)
    assert isinstance(p.get("data"), list), p
    assert "results" not in p
    pag = p["meta"]["pagination"]
    assert pag["limit"] == 1
    assert pag["offset"] == 0
    assert pag["has_more"] is True
    assert pag["next_offset"] == 1
    assert pag["total_count"] == 100


@pytest.mark.asyncio
async def test_guesty_search_listings_pagination_fetch_all(guesty_mod):
    svc = MagicMock()
    svc.search_listings.return_value = {
        "results": [{"_id": "a", "title": "T", "isListed": True, "address": {}}],
        "count": 1,
    }
    with patch.object(guesty_mod, "_get_service", return_value=svc):
        r = await guesty_mod.search_listings(limit=25, skip=0, fetch_all=True, compact=True)
    p = assert_success_dict(r.structuredContent)
    assert_offset_pagination(p)
    pag = p["meta"]["pagination"]
    # fetch_all=True → exhausted, has_more must be False
    assert pag.get("has_more") is False


# ---------------------------------------------------------------------------
# n8n — cursor-based, has_more + next_cursor from payload.nextCursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_n8n_get_workflows_cursor_has_more(n8n_mod):
    payload = {
        "data": [{"id": "w1", "name": "Wf1", "active": True, "isArchived": False, "updatedAt": "t"}],
        "nextCursor": "cursor-xyz",
    }

    class FakeResp:
        def raise_for_status(self):
            pass

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
        r = await n8n_mod.n8n_get_workflows(limit=1, cursor=None)
    p = assert_success_dict(r.structuredContent)
    assert_cursor_pagination(p)
    pag = p["meta"]["pagination"]
    assert pag["limit"] == 1
    assert pag["has_more"] is True
    assert pag["next_cursor"] == "cursor-xyz"
    assert "offset" not in pag


@pytest.mark.asyncio
async def test_n8n_get_workflows_cursor_no_more(n8n_mod):
    payload = {
        "data": [{"id": "w1", "name": "Wf1", "active": True, "isArchived": False, "updatedAt": "t"}],
        "nextCursor": None,
    }

    class FakeResp:
        def raise_for_status(self):
            pass

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
        r = await n8n_mod.n8n_get_workflows(limit=50, cursor=None)
    p = assert_success_dict(r.structuredContent)
    assert_cursor_pagination(p)
    pag = p["meta"]["pagination"]
    assert pag.get("has_more") is False
    assert "next_cursor" not in pag


# ---------------------------------------------------------------------------
# Zendesk — internally exhausted, only limit in meta.pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zendesk_search_tickets_pagination_limit_only(zendesk_mod):
    svc = MagicMock()
    svc.search_tickets.return_value = [{"id": 1, "subject": "S", "status": "new", "created_at": "t", "updated_at": "t"}]
    with patch.object(zendesk_mod, "_get_service", return_value=svc):
        r = await zendesk_mod.zendesk_search_tickets("type:ticket", limit=100, batch_size=50)
    p = assert_success_dict(r.structuredContent)
    assert_pagination_shape(p)
    pag = p["meta"]["pagination"]
    assert pag.get("limit") == 100
    assert "has_more" not in pag
    assert "next_cursor" not in pag
    assert "next_offset" not in pag
    assert "offset" not in pag
    assert "cursor" not in pag
    assert "batch_size" not in pag  # internal field must not leak
