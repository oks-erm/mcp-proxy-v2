"""Emit MCP tool results as structuredContent only (no duplicate JSON text blocks)."""

from __future__ import annotations

from typing import Any, Callable, Optional

from mcp.types import CallToolResult

OptionalSanitize = Optional[Callable[[Any], Any]]


def structured_result(payload: dict[str, Any], *, content_sanitize: OptionalSanitize = None) -> CallToolResult:
    """
    Return tool output as structuredContent with empty content list.

    Pass ``content_sanitize`` (e.g. Firestore json_sanitize) to normalize/redact the payload dict.
    """
    body: Any = payload
    if content_sanitize is not None:
        body = content_sanitize(payload)
    return CallToolResult(content=[], structuredContent=body)
