from __future__ import annotations

import pytest
from util import assert_error_dict, assert_success_dict, load_mcp_server


def test_lookup_table_validation_empty():
    mod = load_mcp_server("sql-gateway")
    r = mod.lookup_table("   ")
    assert_error_dict(r.structuredContent)


def test_run_query_rejects_write_sql():
    mod = load_mcp_server("sql-gateway")
    r = mod.run_query("INSERT INTO t VALUES (1)", page=1, page_size=10, count_rows=False, ctx=None)
    assert_error_dict(r.structuredContent)
    assert r.structuredContent["error"] == "sql_not_allowed"


def test_lookup_table_happy_path_smoke():
    mod = load_mcp_server("sql-gateway")
    r = mod.lookup_table("reservations_reservation", detail_level="compact")
    p = r.structuredContent
    if "error" in p:
        pytest.skip(f"schema unavailable in CI env: {p}")
    assert_success_dict(p)
    assert p.get("table_name")
    assert p.get("detail_level") == "compact"
