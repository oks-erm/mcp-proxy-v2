"""Coerce JSON/tool arguments that may arrive as strings into ints (pagination params)."""

from __future__ import annotations

from typing import Any, Optional


def coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> int:
    """Parse ``value`` as int; invalid/missing uses ``default``, then clamp to ``minimum`` / ``maximum``."""
    v: int
    if value is None:
        v = default
    elif isinstance(value, bool):
        v = default
    elif isinstance(value, int):
        v = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            v = default
        else:
            try:
                v = int(s)
            except ValueError:
                v = default
    else:
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = default
    if v < minimum:
        v = minimum
    if maximum is not None and v > maximum:
        v = maximum
    return v


def coerce_optional_int(
    value: Any,
    *,
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> Optional[int]:
    """Like ``coerce_int`` but returns ``None`` when ``value`` is None or blank string."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    v = coerce_int(value, default=0, minimum=minimum, maximum=maximum)
    return v
