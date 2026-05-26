"""Additive ``meta`` block for MCP tool success payloads (optional schema_version + pagination)."""

from __future__ import annotations

from typing import Any, Dict, Optional

DEFAULT_SCHEMA_VERSION = "1"


def build_pagination_meta(
    *,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    cursor: Optional[str] = None,
    has_more: Optional[bool] = None,
    next_cursor: Optional[str] = None,
    next_offset: Optional[int] = None,
    total_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a normalized ``meta.pagination`` dict for use with ``with_response_meta``.

    Only non-None fields are emitted. Standard names across all tools:

    Offset-based style
        limit, offset, has_more, next_offset, total_count (when available)

    Cursor-based style
        limit, cursor (current input cursor), has_more, next_cursor (continuation token)

    Agents should read ``meta.pagination`` exclusively for pagination state and use the
    standard field names regardless of the upstream service's native pagination vocabulary
    (``skip``, ``nextCursor``, etc.).  Native top-level pagination fields emitted by upstream
    APIs may still appear on the payload for backward compat but are not authoritative.
    """
    out: Dict[str, Any] = {}
    if limit is not None:
        out["limit"] = limit
    if offset is not None:
        out["offset"] = offset
    if cursor is not None:
        out["cursor"] = cursor
    if has_more is not None:
        out["has_more"] = has_more
    if next_cursor is not None:
        out["next_cursor"] = next_cursor
    if next_offset is not None:
        out["next_offset"] = next_offset
    if total_count is not None:
        out["total_count"] = total_count
    return out


def with_response_meta(
    payload: Dict[str, Any],
    *,
    tool: str,
    pagination: Optional[Dict[str, Any]] = None,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
    data_from: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a shallow copy of ``payload`` with ``meta: {tool, schema_version, ...}`` merged in.

    Existing top-level keys on ``payload`` are preserved; if ``payload`` already has ``meta``,
    it is replaced.

    **``data`` is the authoritative list field.**  When ``data_from`` is set, this function adds
    top-level ``data`` as the *same list object* as the native key (e.g. ``results``, ``tickets``,
    ``users``, ``documents``).  Service-native keys are preserved for backward compatibility only
    and may be removed in a future major version — agents should read ``data``.

    Use ``build_pagination_meta`` to produce the ``pagination`` dict with normalized field names
    (``offset``/``cursor``, ``has_more``, ``next_offset``/``next_cursor``, ``total_count``) so
    agents can page any tool without knowing its upstream pagination vocabulary.
    """
    out = dict(payload)
    if data_from and "data" not in out:
        v = out.get(data_from)
        if isinstance(v, list):
            out["data"] = v
    meta: Dict[str, Any] = {"tool": tool, "schema_version": schema_version}
    if pagination is not None:
        meta["pagination"] = pagination
    out["meta"] = meta
    return out
