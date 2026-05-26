"""Standard tool payload envelope for errors (success payloads are plain domain dicts)."""

from __future__ import annotations

from typing import Any, Literal, Optional

# Optional machine-readable cause for agents (orthogonal to short ``error`` code).
ErrorCause = Literal["not_found", "upstream_error", "validation", "permission", "timeout", "unknown"]


def tool_error(
    code: str,
    details: str,
    *,
    retryable: Optional[bool] = None,
    suggested_fix: Optional[str] = None,
    cause: Optional[ErrorCause] = None,
    **extra: Any,
) -> dict[str, Any]:
    """Structured tool error: ``error`` (short code) + ``details`` (human-readable).

    Optional: ``retryable``, ``suggested_fix``, ``cause`` for agent orchestration.
    Additional kwargs are merged when non-None (e.g. legacy ``status_code``, ``timeout_seconds``).
    """
    out: dict[str, Any] = {"error": code, "details": details}
    if retryable is not None:
        out["retryable"] = retryable
    if suggested_fix is not None:
        out["suggested_fix"] = suggested_fix
    if cause is not None:
        out["cause"] = cause
    for k, v in extra.items():
        if v is not None:
            out[k] = v
    return out


def is_tool_error(payload: Any) -> bool:
    """True if payload looks like our standard error object."""
    return isinstance(payload, dict) and "error" in payload and "details" in payload
