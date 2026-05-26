from __future__ import annotations

import pytest
from util import assert_error_dict, load_mcp_server


@pytest.fixture
def n8n_mod():
    return load_mcp_server("n8n-mcp")


@pytest.mark.asyncio
async def test_n8n_find_workflow_validation_empty_query(n8n_mod):
    res = await n8n_mod.n8n_find_workflow_by_name("", 10, None, 3, True)
    payload = assert_error_dict(res.structuredContent)
    assert payload["error"] == "validation_error"


@pytest.mark.asyncio
async def test_n8n_create_workflow_requires_summary(n8n_mod):
    res = await n8n_mod.n8n_create_workflow(name="Demo", nodes=[], connections={})
    payload = assert_error_dict(res.structuredContent)
    assert payload["error"] == "validation_error"
    assert payload["details"] == "summary is required"


@pytest.mark.asyncio
async def test_n8n_delete_workflow_requires_workflow_id(n8n_mod):
    res = await n8n_mod.n8n_delete_workflow()
    payload = assert_error_dict(res.structuredContent)
    assert payload["error"] == "validation_error"
    assert payload["details"] == "workflow_id is required"
