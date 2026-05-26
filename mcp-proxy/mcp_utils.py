"""Shared utilities for MCP proxy."""

import json
from typing import Any, Dict


def parse_mcp_response(text: str) -> dict | None:
    """
    Parse MCP response: plain JSON or SSE (event-stream with data: lines).
    NocoDB and some MCP servers return SSE format.
    """
    if not text or not text.strip():
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    continue
    return None


def _canonical_json_obj(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _schema_inline_body(sc: Any) -> str | None:
    """If structuredContent matches sql-gateway schema tool shape, return the inline body string."""
    if not isinstance(sc, dict):
        return None
    fmt = sc.get("format")
    body = sc.get("content")
    if fmt in ("markdown", "yaml") and isinstance(body, str):
        return body
    return None


def dedupe_tool_call_jsonrpc(rpc: Dict[str, Any]) -> Dict[str, Any]:
    """Drop text content blocks that only repeat structuredContent (saves tokens for MCP agents).

    The MCP Python SDK duplicates dict tool results as both structuredContent and a JSON text
    content block; some clients concatenate both. Removes redundant text blocks when they parse
    to the same object as structuredContent.
    """
    if not isinstance(rpc, dict) or rpc.get("jsonrpc") != "2.0":
        return rpc
    result = rpc.get("result")
    if not isinstance(result, dict):
        return rpc
    sc = result.get("structuredContent")
    content = result.get("content")
    if sc is None or not isinstance(content, list):
        return rpc
    want = _canonical_json_obj(sc)
    new_blocks: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            new_blocks.append(block)
            continue
        if block.get("type") != "text":
            new_blocks.append(block)
            continue
        t = block.get("text")
        if not isinstance(t, str):
            new_blocks.append(block)
            continue
        try:
            parsed = json.loads(t)
        except json.JSONDecodeError:
            # Some MCP stacks emit the markdown/yaml body again as plain text (not JSON),
            # while structuredContent holds the same prose inside {"format","content",...}.
            inline = _schema_inline_body(sc)
            if inline is not None and isinstance(t, str) and t.strip() == inline.strip():
                continue
            new_blocks.append(block)
            continue
        if _canonical_json_obj(parsed) == want:
            continue
        new_blocks.append(block)
    if new_blocks == content:
        return rpc
    out = dict(rpc)
    r2 = dict(result)
    r2["content"] = new_blocks
    out["result"] = r2
    return out
