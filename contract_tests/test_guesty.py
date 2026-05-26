from __future__ import annotations

import pytest
from util import assert_error_dict, load_mcp_server


@pytest.fixture
def guesty_mod():
    return load_mcp_server("guesty-mcp")


@pytest.mark.asyncio
async def test_find_listing_requires_query(guesty_mod):
    r = await guesty_mod.find_listing("", 5, "compact")
    assert_error_dict(r.structuredContent)
    assert r.structuredContent["error"] == "validation_error"
