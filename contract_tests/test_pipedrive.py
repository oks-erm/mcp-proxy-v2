from __future__ import annotations

import pytest
from util import assert_error_dict, assert_success_dict, load_mcp_server


@pytest.fixture
def pipedrive_mod():
    return load_mcp_server("pipedrive-mcp")


@pytest.mark.asyncio
async def test_pipedrive_search_deals_validation_short_term(pipedrive_mod):
    res = await pipedrive_mod.pipedrive_search_deals("x", None, None, None, None, None, None, 10, None, None)
    payload = assert_error_dict(res.structuredContent)
    assert payload["error"] == "validation_error"


@pytest.mark.asyncio
async def test_pipedrive_upstream_error_shape(pipedrive_mod):
    err_body = {"success": False, "error": "nope", "status_code": 500}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipedrive_mod, "get_json", lambda *a, **k: err_body)
        res = await pipedrive_mod.pipedrive_list_leads(0, 5, None)
    payload = assert_error_dict(res.structuredContent)
    assert payload["error"] == "upstream_failed"
    assert payload["status_code"] == 500


@pytest.mark.asyncio
async def test_pipedrive_list_leads_success_adds_detail(pipedrive_mod):
    ok = {"success": True, "data": [{"id": 1, "note": "secret"}]}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipedrive_mod, "get_json", lambda *a, **k: ok)
        res = await pipedrive_mod.pipedrive_list_leads(0, 5, "compact")
    payload = assert_success_dict(res.structuredContent)
    assert payload["detail_level"] == "compact"
    assert "note" not in payload["data"][0]
