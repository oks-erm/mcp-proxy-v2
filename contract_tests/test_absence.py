from __future__ import annotations

import pytest
from util import assert_error_dict, load_mcp_server


@pytest.fixture
def absence_mod():
    return load_mcp_server("absence-mcp")


@pytest.mark.asyncio
async def test_absence_find_user_validation(absence_mod):
    r = await absence_mod.absence_find_user_by_name("", True, 5)
    assert_error_dict(r.structuredContent)
    assert r.structuredContent["error"] == "validation_error"
