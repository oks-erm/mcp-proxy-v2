"""Resolve upstream MCP credentials from Secret Manager or inline header."""

import json
import logging
import os
from typing import Dict

from google.cloud import secretmanager

logger = logging.getLogger(__name__)

_project_id = os.getenv("GCP_PROJECT_ID", "it-team-hw-project")
_cache: Dict[str, Dict[str, str]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes
_cache_time: Dict[str, float] = {}


def parse_header_string(s: str) -> Dict[str, str]:
    """Parse 'Header-Name: value' format (same as mcp.json --header)."""
    s = s.strip()
    if ":" not in s:
        return {}
    name, _, value = s.partition(":")
    return {name.strip(): value.strip()} if name.strip() else {}


def get_headers(
    credentials_secret_id: str = "",
    credentials_header: str = "",
) -> Dict[str, str]:
    """
    Resolve MCP request headers from inline header or Secret Manager.

    If credentials_header is set (format "Header-Name: value" like mcp.json),
    parse and return it. If credentials_secret_id looks like "Header: value"
    (contains ": "), treat as inline header (handles wrong-field input).
    Otherwise fetch from Secret Manager.
    """
    header_str = credentials_header or ""
    # Secret IDs cannot contain ": " - if present, treat as misplaced header
    if not header_str and credentials_secret_id and ": " in credentials_secret_id:
        header_str = credentials_secret_id
    if header_str:
        logger.debug("Resolving credentials from inline header (len=%d)", len(header_str))
        return parse_header_string(header_str)

    if not credentials_secret_id:
        return {}

    import time

    now = time.time()
    if credentials_secret_id in _cache:
        if now - _cache_time.get(credentials_secret_id, 0) < _CACHE_TTL_SECONDS:
            logger.debug("Credentials cache hit for %s", credentials_secret_id)
            return _cache[credentials_secret_id].copy()

    logger.debug("Fetching credentials from Secret Manager: %s", credentials_secret_id)
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{_project_id}/secrets/{credentials_secret_id}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        payload = response.payload.data.decode("UTF-8")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("Secret must contain a JSON object")
        headers = {str(k): str(v) for k, v in data.items()}
        _cache[credentials_secret_id] = headers
        _cache_time[credentials_secret_id] = now
        return headers.copy()
    except Exception as e:
        logger.error("Failed to fetch credentials from %s: %s", credentials_secret_id, e)
        raise RuntimeError(f"Failed to fetch credentials: {e}") from e
