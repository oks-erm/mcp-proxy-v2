"""Process-wide Firestore client for mcp-proxy-database (avoid per-call Client() construction)."""

from __future__ import annotations

from typing import Optional

import config
from google.cloud import firestore

_client: Optional[firestore.Client] = None


def get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(
            project=config.GCP_PROJECT_ID,
            database=config.MCP_PROXY_DATABASE,
        )
    return _client


def reset_client_for_tests() -> None:
    """Clear cached client (tests only)."""
    global _client
    _client = None
