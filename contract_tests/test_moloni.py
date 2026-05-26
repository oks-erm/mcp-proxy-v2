from __future__ import annotations

import pytest
from util import (
    assert_error_dict,
    assert_meta_tool,
    assert_offset_pagination,
    assert_success_dict,
    load_mcp_server,
)


@pytest.fixture
def moloni_mod():
    mod = load_mcp_server("moloni-mcp")
    mod.moloni_client.configure_client(
        {
            "developer_id": "d",
            "client_secret": "s",
            "username": "u",
            "password": "p",
            "company_id": 99,
        }
    )
    yield mod
    mod.moloni_client.configure_client(None)


@pytest.mark.asyncio
async def test_moloni_get_invoice_validation_non_numeric(moloni_mod):
    res = await moloni_mod.moloni_get_invoice("abc")
    payload = assert_error_dict(res.structuredContent)
    assert payload["error"] == "validation_error"


@pytest.mark.asyncio
async def test_moloni_list_invoices_upstream_error(moloni_mod):
    err_body = {"error": "nope", "error_description": "failed"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(moloni_mod.moloni_client, "api_post", lambda *a, **k: err_body)
        res = await moloni_mod.moloni_list_invoices(0, 10, None, None, None, None, None, None, None)
    payload = assert_error_dict(res.structuredContent)
    assert payload["error"] == "upstream_failed"


@pytest.mark.asyncio
async def test_moloni_list_invoices_success_meta(moloni_mod):
    ok = [{"document_id": 1, "number": 10, "your_reference": "inv-a"}]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(moloni_mod.moloni_client, "api_post", lambda *a, **k: ok)
        res = await moloni_mod.moloni_list_invoices(0, 10, None, None, None, None, None, None, None)
    payload = assert_success_dict(res.structuredContent)
    assert_meta_tool(payload, expected_tool="moloni_list_invoices")
    assert_offset_pagination(payload)
    assert payload["data"] == ok


@pytest.mark.asyncio
async def test_moloni_list_invoices_clamps_qty(moloni_mod):
    captured: list[tuple[str, dict]] = []

    def capture(endpoint: str, data: dict):
        captured.append((endpoint, dict(data)))
        return []

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(moloni_mod.moloni_client, "api_post", capture)
        await moloni_mod.moloni_list_invoices(0, 500, None, None, None, None, None, None, None)
    assert captured[0][0] == "invoices/getAll"
    assert captured[0][1]["qty"] == 50
    assert captured[0][1]["company_id"] == 99


@pytest.mark.asyncio
async def test_moloni_get_invoice_success(moloni_mod):
    doc = {"document_id": 42, "company_id": 99, "number": 7, "notes": "x"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(moloni_mod.moloni_client, "api_post", lambda *a, **k: doc)
        res = await moloni_mod.moloni_get_invoice("42")
    payload = assert_success_dict(res.structuredContent)
    assert_meta_tool(payload, expected_tool="moloni_get_invoice")
    assert payload["document_id"] == 42
