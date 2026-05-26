from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from util import assert_error_dict, assert_success_dict, load_mcp_server


@pytest.fixture
def zendesk_mod():
    return load_mcp_server("zendesk-mcp")


@pytest.mark.asyncio
async def test_zendesk_search_tickets_compact_strips_heavy_description(zendesk_mod):
    big = "x" * 9000
    svc = MagicMock()
    svc.search_tickets = MagicMock(return_value=[{"id": 1, "subject": "hi", "description": big}])
    with patch.object(zendesk_mod, "_get_service", return_value=svc):
        res = await zendesk_mod.zendesk_search_tickets("type:ticket", 10, 100, None)
    payload = assert_success_dict(res.structuredContent)
    assert payload.get("detail_level") == "compact"
    assert "tickets" not in payload
    t0 = payload["data"][0]
    assert "description" not in t0


@pytest.mark.asyncio
async def test_zendesk_find_ticket_validation_error(zendesk_mod):
    res = await zendesk_mod.zendesk_find_ticket_by_reservation_id("", limit=1)
    payload = assert_error_dict(res.structuredContent)
    assert payload["error"] == "validation_error"


@pytest.mark.asyncio
async def test_zendesk_upstream_exception(zendesk_mod):
    svc = MagicMock()
    svc.search_tickets = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(zendesk_mod, "_get_service", return_value=svc):
        res = await zendesk_mod.zendesk_search_tickets("q", 1, 10, None)
    payload = assert_error_dict(res.structuredContent)
    assert payload["error"] == "upstream_failed"
