"""Shared MCP tool helpers: response envelope, detail levels, structured transport."""

from mcp_platform.detail_level import (
    DetailLevel,
    effective_detail_level,
    parse_detail_level,
)
from mcp_platform.envelope import is_tool_error, tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result

__all__ = [
    "DetailLevel",
    "effective_detail_level",
    "parse_detail_level",
    "is_tool_error",
    "tool_error",
    "structured_result",
    "build_pagination_meta",
    "with_response_meta",
]
