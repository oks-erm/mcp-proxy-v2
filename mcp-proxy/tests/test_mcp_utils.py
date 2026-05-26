"""Unit tests for mcp_utils helpers."""

from __future__ import annotations

import json

from mcp_utils import dedupe_tool_call_jsonrpc


def test_dedupe_tool_call_jsonrpc_strips_redundant_text():
    payload = {"format": "markdown", "content": "hello"}
    text = json.dumps(payload, indent=2)
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
        },
    }
    out = dedupe_tool_call_jsonrpc(rpc)
    assert out["result"]["content"] == []
    assert out["result"]["structuredContent"] == payload


def test_dedupe_tool_call_jsonrpc_strips_raw_markdown_mirror_of_schema_payload():
    """SDKs may emit the schema body as plain text while structuredContent already embeds it in JSON."""
    schema_body = "# Tables\n\nSome **markdown** guide content.\n"
    payload = {"format": "markdown", "content": schema_body, "error": None, "details": None}
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": schema_body}],
            "structuredContent": payload,
        },
    }
    out = dedupe_tool_call_jsonrpc(rpc)
    assert out["result"]["content"] == []
    assert out["result"]["structuredContent"] == payload


def test_dedupe_tool_call_jsonrpc_strips_schema_mirror_with_trailing_whitespace_mismatch():
    """Plain-text mirror may add trailing newline while structuredContent content does not."""
    content_inline = "# Guide\n\nBody."
    text_block = "# Guide\n\nBody.\n"
    payload = {"format": "markdown", "content": content_inline, "detail_level": "full"}
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": text_block}],
            "structuredContent": payload,
        },
    }
    out = dedupe_tool_call_jsonrpc(rpc)
    assert out["result"]["content"] == []


def test_dedupe_tool_call_jsonrpc_strips_raw_yaml_mirror_of_schema_payload():
    body = "tables:\n  foo:\n    columns: []\n"
    payload = {"format": "yaml", "content": body, "error": None, "details": None}
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": body}],
            "structuredContent": payload,
        },
    }
    out = dedupe_tool_call_jsonrpc(rpc)
    assert out["result"]["content"] == []


def test_dedupe_tool_call_jsonrpc_keeps_non_duplicate_text():
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": '{"hint":"read structuredContent"}'}],
            "structuredContent": {"a": 1},
        },
    }
    out = dedupe_tool_call_jsonrpc(rpc)
    assert len(out["result"]["content"]) == 1
