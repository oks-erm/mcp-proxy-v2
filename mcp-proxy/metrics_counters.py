"""Thread-safe in-process counters for MCP proxy (per Cloud Run instance / process)."""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any, Dict

_lock = threading.Lock()
_boot_id: str = uuid.uuid4().hex[:12]

_COUNT_KEYS = (
    "mcp_denied_missing_key",
    "mcp_denied_invalid_user",
    "mcp_requests_authorized",
    "mcp_errors",
)
_counters: Dict[str, int] = {k: 0 for k in _COUNT_KEYS}
_by_method: Dict[str, int] = {}

_KNOWN_METHODS = frozenset(
    {
        "initialize",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "prompts/list",
        "prompts/get",
    }
)


def normalize_method_name(method: str) -> str:
    m = (method or "").strip()
    if not m:
        return "empty"
    if m.startswith("notifications/"):
        return "notifications"
    if m in _KNOWN_METHODS:
        return m
    return "other"


def increment_denied_missing_key() -> None:
    with _lock:
        _counters["mcp_denied_missing_key"] += 1


def increment_denied_invalid_user() -> None:
    with _lock:
        _counters["mcp_denied_invalid_user"] += 1


def increment_requests_authorized() -> None:
    with _lock:
        _counters["mcp_requests_authorized"] += 1


def increment_errors() -> None:
    with _lock:
        _counters["mcp_errors"] += 1


def increment_method(method: str) -> None:
    key = normalize_method_name(method)
    with _lock:
        _by_method[key] = _by_method.get(key, 0) + 1


def snapshot() -> Dict[str, Any]:
    with _lock:
        return {
            "process_id": os.getpid(),
            "boot_id": _boot_id,
            "counters": dict(_counters),
            "by_method": dict(_by_method),
        }
