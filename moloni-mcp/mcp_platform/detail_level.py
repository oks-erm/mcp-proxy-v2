"""Shared detail_level convention: compact | summary | full."""

from __future__ import annotations

from typing import Literal, Optional

DetailLevel = Literal["compact", "summary", "full"]

_VALID = frozenset({"compact", "summary", "full"})


def parse_detail_level(value: Optional[str], *, default: DetailLevel = "compact") -> DetailLevel:
    """Parse and validate detail_level; invalid values fall back to default."""
    if not value:
        return default
    v = str(value).strip().lower()
    if v in _VALID:
        return v  # type: ignore[return-value]
    return default


def effective_detail_level(
    detail_level: Optional[str],
    *,
    default: DetailLevel = "compact",
    compact: Optional[bool] = None,
) -> DetailLevel:
    """
    Resolve detail_level with optional legacy ``compact`` boolean (True->compact, False->full).
    ``detail_level`` wins when provided and valid.
    """
    if compact is not None and (not detail_level or str(detail_level).strip().lower() not in _VALID):
        return "compact" if compact else "full"
    return parse_detail_level(detail_level, default=default)
