"""Load deployable MCP ``mcp_server`` modules in isolation (unique sys.modules name)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

MCP_ROOT = Path(__file__).resolve().parent.parent


def load_mcp_server(subdir: str) -> ModuleType:
    """Import ``global/mcp/{subdir}/mcp_server.py`` with service directory on ``sys.path``."""
    path = MCP_ROOT / subdir / "mcp_server.py"
    if not path.is_file():
        raise FileNotFoundError(path)
    name = f"contract_{subdir.replace('-', '_')}"
    service_root = str(path.parent)
    if service_root not in sys.path:
        sys.path.insert(0, service_root)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    # register before exec so relative circular imports in server resolve
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def assert_success_dict(payload: object) -> dict:
    assert isinstance(payload, dict), f"expected dict structuredContent, got {type(payload)}"
    assert "error" not in payload, payload
    return payload


def assert_error_dict(payload: object) -> dict:
    assert isinstance(payload, dict), payload
    assert "error" in payload and "details" in payload, payload
    assert isinstance(payload["error"], str) and isinstance(payload["details"], str), payload
    return payload


def assert_error_dict_normalized(payload: object) -> dict:
    """Like ``assert_error_dict`` but require ``cause`` and ``retryable`` (tool_error-normalized)."""
    p = assert_error_dict(payload)
    assert "cause" in p and isinstance(p["cause"], str), p
    assert "retryable" in p and isinstance(p["retryable"], bool), p
    return p


def assert_meta_tool(payload: dict, *, expected_tool: str) -> dict:
    assert "meta" in payload, payload
    meta = payload["meta"]
    assert isinstance(meta, dict), meta
    assert meta.get("tool") == expected_tool, meta
    assert "schema_version" in meta and isinstance(meta["schema_version"], str), meta
    return payload


def assert_data_aliases_list(payload: dict, native_key: str) -> None:
    """Top-level ``data`` must be the same list object as ``native_key`` (``data_from`` contract)."""
    assert native_key in payload and "data" in payload, payload
    assert isinstance(payload[native_key], list), payload
    assert payload["data"] is payload[native_key]


# ---------------------------------------------------------------------------
# Pagination contract helpers
# ---------------------------------------------------------------------------

_VALID_PAGINATION_KEYS = frozenset(
    {"limit", "offset", "cursor", "has_more", "next_cursor", "next_offset", "total_count"}
)


def assert_pagination_shape(payload: dict) -> dict:
    """``meta.pagination`` must exist and use only normalized field names."""
    assert "meta" in payload, payload
    pag = payload["meta"].get("pagination")
    assert pag is not None, f"meta.pagination missing in {payload['meta']}"
    assert isinstance(pag, dict), pag
    unknown = set(pag.keys()) - _VALID_PAGINATION_KEYS
    assert not unknown, f"meta.pagination has non-standard keys {unknown}: {pag}"
    if "limit" in pag:
        assert isinstance(pag["limit"], int), pag
    if "offset" in pag:
        assert isinstance(pag["offset"], int) and pag["offset"] >= 0, pag
    if "has_more" in pag:
        assert isinstance(pag["has_more"], bool), pag
    if "total_count" in pag:
        assert isinstance(pag["total_count"], int), pag
    return payload


def assert_offset_pagination(payload: dict) -> dict:
    """Require offset-style pagination: ``limit``, ``offset``; no cursor fields."""
    assert_pagination_shape(payload)
    pag = payload["meta"]["pagination"]
    assert "limit" in pag, pag
    assert "offset" in pag, pag
    assert "cursor" not in pag, f"cursor must not appear in offset pagination: {pag}"
    assert "next_cursor" not in pag, pag
    return payload


def assert_cursor_pagination(payload: dict) -> dict:
    """Require cursor-style pagination: ``limit``, optional ``has_more`` / ``next_cursor``; no offset."""
    assert_pagination_shape(payload)
    pag = payload["meta"]["pagination"]
    assert "limit" in pag, pag
    assert "offset" not in pag, f"offset must not appear in cursor pagination: {pag}"
    assert "next_offset" not in pag, pag
    return payload
