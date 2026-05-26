"""Runtime limits for Firestore gateway queries."""

import os

_ENV_MAX = "FIRESTORE_GATEWAY_MAX_QUERY_LIMIT"
_DEFAULT_MAX = 100


def max_query_limit() -> int:
    """Upper bound on documents returned per request (REST `/query` and MCP `query_documents`)."""
    raw = os.getenv(_ENV_MAX, str(_DEFAULT_MAX))
    try:
        n = int(raw)
    except ValueError:
        n = _DEFAULT_MAX
    return max(1, n)
