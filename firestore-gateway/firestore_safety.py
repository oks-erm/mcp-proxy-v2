"""Collection deny-list and recursive field redaction for Firestore gateway (MCP + REST)."""

from __future__ import annotations

import fnmatch
import os
from typing import Any, FrozenSet, Optional, Tuple

_REDACTED = "<redacted>"

# Default: known credential-adjacent collection names (override via FIRESTORE_GATEWAY_DENIED_COLLECTIONS).
_DEFAULT_DENIED = "tokens,n8n-tokens"

# Exact key names (lowercase) to redact when seen at any depth.
_SENSITIVE_KEY_NAMES: FrozenSet[str] = frozenset(
    {
        "token",
        "password",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "refresh_token",
        "access_token",
        "private_key",
        "client_secret",
        "jwt",
        "bearer",
        "credentials_json",
    }
)

# Key suffixes (lowercase) — e.g. breezeway_token, oauth_secret.
_SENSITIVE_KEY_SUFFIXES: tuple[str, ...] = (
    "_token",
    "_secret",
    "_password",
    "_api_key",
    "password",
    "secret",
    "token",
)


def denied_collections() -> FrozenSet[str]:
    """Collections that cannot be read via this gateway (comma-separated env)."""
    raw = os.getenv("FIRESTORE_GATEWAY_DENIED_COLLECTIONS", _DEFAULT_DENIED)
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    return frozenset(parts)


_DEFAULT_DENIED_PATTERNS = "tmp_*,*_migration,*_webhooks,monitoring_*"


def denied_collection_patterns() -> Tuple[str, ...]:
    """Shell-style glob patterns (fnmatch) per collection id, comma-separated env.

    Defaults to ``tmp_*,*_migration,*_webhooks,monitoring_*`` to hide ephemeral/internal collections
    from discovery. Override with ``FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS``.
    """
    raw = os.getenv("FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS", _DEFAULT_DENIED_PATTERNS)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def collection_matches_denied_pattern(collection: str) -> bool:
    return any(fnmatch.fnmatchcase(collection, pat) for pat in denied_collection_patterns())


def is_collection_denied(collection: str) -> bool:
    """True if this collection cannot be read (exact deny list or pattern)."""
    if collection in denied_collections():
        return True
    return collection_matches_denied_pattern(collection)


def filter_visible_collection_ids(collection_ids: list[str]) -> list[str]:
    """Preserve order; omit ids denied for reads (for list_collections discovery)."""
    return [c for c in collection_ids if not is_collection_denied(c)]


def collection_access_error(collection: str) -> Optional[str]:
    """If collection is denied, return an error message; otherwise None."""
    if collection in denied_collections():
        return (
            f"Access to collection '{collection}' is not allowed through this gateway "
            "(configured in FIRESTORE_GATEWAY_DENIED_COLLECTIONS)."
        )
    if collection_matches_denied_pattern(collection):
        return (
            f"Access to collection '{collection}' is not allowed through this gateway "
            "(matches FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS)."
        )
    return None


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    if k in _SENSITIVE_KEY_NAMES:
        return True
    return any(k.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


def redact_document(obj: Any) -> Any:
    """Recursively redact sensitive keys in dict/list structures; leaves scalars as-is."""
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            sk = str(k)
            if _is_sensitive_key(sk):
                out[k] = _REDACTED
            else:
                out[k] = redact_document(v)
        return out
    if isinstance(obj, list):
        return [redact_document(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact_document(item) for item in obj)
    return obj


def redact_documents(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [redact_document(d) for d in docs]


def json_sanitize(obj: Any) -> Any:
    """Recursively coerce Firestore / Python values into JSON-serializable structures for MCP structuredContent."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {str(k): json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_sanitize(v) for v in obj]
    if isinstance(obj, tuple):
        return [json_sanitize(v) for v in obj]
    iso = getattr(obj, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except (TypeError, ValueError):
            pass
    mod = getattr(type(obj), "__module__", "") or ""
    name = type(obj).__name__
    if mod.startswith("google.cloud.firestore") and name in ("GeoPoint", "DocumentReference"):
        return str(obj)
    return str(obj)
