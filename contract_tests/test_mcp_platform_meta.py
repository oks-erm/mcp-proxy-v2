"""Unit tests for ``mcp_platform.meta.with_response_meta`` (canonical copy under ``global/mcp/mcp_platform``)."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_meta_module():
    path = Path(__file__).resolve().parent.parent / "mcp_platform" / "meta.py"
    spec = importlib.util.spec_from_file_location("_mcp_platform_meta_under_test", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


meta = _load_meta_module()
with_response_meta = meta.with_response_meta


def test_with_response_meta_adds_tool_and_schema_version():
    src = {"results": [{"a": 1}], "count": 1}
    out = with_response_meta(src, tool="t_example")
    assert out is not src
    assert out["results"] == [{"a": 1}]
    assert out["meta"] == {"tool": "t_example", "schema_version": meta.DEFAULT_SCHEMA_VERSION}


def test_with_response_meta_data_from_aliases_list():
    results = [{"x": 1}]
    src = {"results": results, "count": 1}
    out = with_response_meta(src, tool="t_list", data_from="results")
    assert out["data"] is results
    assert out["results"] is results


def test_with_response_meta_data_from_skips_when_data_present():
    results = [{"x": 1}]
    existing = [{"y": 2}]
    src = {"results": results, "data": existing, "count": 1}
    out = with_response_meta(src, tool="t", data_from="results")
    assert out["data"] is existing


def test_with_response_meta_data_from_skips_non_list_source():
    src = {"results": "not-a-list", "count": 0}
    out = with_response_meta(src, tool="t", data_from="results")
    assert "data" not in out


def test_with_response_meta_replaces_existing_meta():
    src = {"results": [], "meta": {"stale": True}}
    out = with_response_meta(src, tool="t_new", pagination={"cursor": "abc"})
    assert "stale" not in out["meta"]
    assert out["meta"]["tool"] == "t_new"
    assert out["meta"]["pagination"] == {"cursor": "abc"}


def test_with_response_meta_none_data_from_is_noop_for_data():
    src = {"results": [{"k": 1}]}
    out = with_response_meta(src, tool="t", data_from=None)
    assert "data" not in out
