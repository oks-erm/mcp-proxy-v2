"""Firestore operations for MCP server configs."""

import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
from gcp_firestore import get_client
from google.cloud.firestore import DELETE_FIELD
from models import OAuth2UpstreamConfig, ServerConfig

logger = logging.getLogger(__name__)

_servers_cache_all: Optional[List[ServerConfig]] = None
_servers_cache_mono: float = 0.0


def _cache_ttl_seconds() -> float:
    raw = os.getenv("MCP_SERVERS_CACHE_TTL_SECONDS", "30")
    try:
        v = float(raw)
        return max(0.0, v)
    except ValueError:
        return 30.0


def invalidate_mcp_servers_cache() -> None:
    """Drop in-process mcp_servers list cache (call after create/update/delete)."""
    global _servers_cache_all, _servers_cache_mono
    _servers_cache_all = None
    _servers_cache_mono = 0.0


def _mcp_servers_collection():
    collection_name = os.getenv("FIRESTORE_COLLECTION", "mcp_servers")
    project_id = config.GCP_PROJECT_ID
    database = config.MCP_PROXY_DATABASE
    logger.debug(
        "Firestore mcp_servers: project=%s, database=%s, collection=%s",
        project_id,
        database,
        collection_name,
    )
    return get_client().collection(collection_name)


def _fetch_all_server_configs() -> List[ServerConfig]:
    coll = _mcp_servers_collection()
    docs = list(coll.stream())
    configs = [_doc_to_config(doc.id, doc.to_dict() or {}) for doc in docs]
    configs.sort(key=lambda c: (c.created_at or datetime.min).isoformat())
    return configs


def _get_cached_all_server_configs() -> List[ServerConfig]:
    global _servers_cache_all, _servers_cache_mono
    ttl = _cache_ttl_seconds()
    now = time.monotonic()
    if ttl > 0 and _servers_cache_all is not None and (now - _servers_cache_mono) < ttl:
        return list(_servers_cache_all)
    configs = _fetch_all_server_configs()
    _servers_cache_all = list(configs)
    _servers_cache_mono = now
    return list(configs)


def _oauth_from_data(data: Dict[str, Any]) -> Optional[OAuth2UpstreamConfig]:
    raw = data.get("oauth")
    if not raw or not isinstance(raw, dict):
        return None
    try:
        return OAuth2UpstreamConfig.model_validate(raw)
    except Exception:
        logger.warning("Invalid oauth config in server document, ignoring")
        return None


def _doc_to_config(doc_id: str, data: Dict[str, Any]) -> ServerConfig:
    """Convert Firestore document to ServerConfig."""
    ua = data.get("upstream_auth") or "headers"
    if ua not in ("headers", "cloud_run_iam", "oauth2"):
        ua = "headers"
    oauth = _oauth_from_data(data)
    if ua == "oauth2" and oauth is None:
        ua = "headers"
    return ServerConfig(
        id=doc_id,
        url=data.get("url", ""),
        credentials_secret_id=data.get("credentials_secret_id", ""),
        credentials_header=data.get("credentials_header", ""),
        upstream_auth=ua,
        oauth=oauth,
        enabled=data.get("enabled", True),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def verify_firestore_connection() -> int:
    """
    Verify Firestore connection at startup. Logs connection target and server count.
    Returns the number of server configs found.
    Raises on connection or permission errors.
    """
    project_id = config.GCP_PROJECT_ID
    database = config.MCP_PROXY_DATABASE
    collection_name = os.getenv("FIRESTORE_COLLECTION", "mcp_servers")
    path = f"projects/{project_id}/databases/{database}/documents/{collection_name}"
    logger.info(
        "Firestore connection check: project=%s, database=%s, collection=%s (path=%s)",
        project_id,
        database,
        collection_name,
        path,
    )
    try:
        invalidate_mcp_servers_cache()
        configs = list_servers(enabled_only=False)
        logger.info("Firestore connection OK: found %d server config(s)", len(configs))
        return len(configs)
    except Exception as e:
        logger.error(
            "Firestore connection FAILED (project=%s, database=%s): %s",
            project_id,
            database,
            e,
        )
        raise


def list_servers(enabled_only: bool = False) -> List[ServerConfig]:
    """List all MCP server configs (cached with TTL; invalidated on writes)."""
    logger.debug("list_servers enabled_only=%s", enabled_only)
    all_configs = _get_cached_all_server_configs()
    if enabled_only:
        return [c for c in all_configs if c.enabled]
    return list(all_configs)


def get_server(server_id: str) -> Optional[ServerConfig]:
    """Get a single MCP server config by ID."""
    coll = _mcp_servers_collection()
    doc = coll.document(server_id).get()
    if not doc.exists:
        return None
    return _doc_to_config(doc.id, doc.to_dict() or {})


def get_cloud_run_deployer_server() -> Optional[ServerConfig]:
    """Resolve the deployer upstream whether the Firestore id is ``cloud_run_deployer`` or ``cloud-run-deployer``."""
    for doc_id in ("cloud_run_deployer", "cloud-run-deployer"):
        cfg = get_server(doc_id)
        if cfg and cfg.enabled:
            return cfg
    return None


def create_server(
    server_id: str,
    url: str,
    credentials_secret_id: str = "",
    credentials_header: str = "",
    enabled: bool = True,
    upstream_auth: str = "headers",
    oauth: Optional[OAuth2UpstreamConfig] = None,
) -> ServerConfig:
    """Create a new MCP server config."""
    coll = _mcp_servers_collection()
    doc_ref = coll.document(server_id)
    now = datetime.utcnow()
    data: Dict[str, Any] = {
        "url": url,
        "credentials_secret_id": credentials_secret_id,
        "credentials_header": credentials_header,
        "upstream_auth": upstream_auth,
        "enabled": enabled,
        "created_at": now,
        "updated_at": now,
    }
    if oauth is not None:
        data["oauth"] = oauth.model_dump(exclude_none=True)
    doc_ref.set(data)
    invalidate_mcp_servers_cache()
    logger.debug("Created MCP server config: %s url=%s", server_id, url)
    logger.info("Created MCP server config: %s", server_id)
    return _doc_to_config(server_id, data)


def update_server(
    server_id: str,
    url: Optional[str] = None,
    credentials_secret_id: Optional[str] = None,
    credentials_header: Optional[str] = None,
    enabled: Optional[bool] = None,
    upstream_auth: Optional[str] = None,
    reset_credentials: Optional[bool] = None,
    oauth: Optional[OAuth2UpstreamConfig] = None,
    reset_oauth: Optional[bool] = None,
) -> Optional[ServerConfig]:
    """Update an existing MCP server config (partial update)."""
    coll = _mcp_servers_collection()
    doc_ref = coll.document(server_id)
    doc = doc_ref.get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    updates: Dict[str, Any] = {"updated_at": datetime.utcnow()}
    if url is not None:
        updates["url"] = url
    if upstream_auth is not None:
        updates["upstream_auth"] = upstream_auth
    if reset_oauth and oauth is None:
        updates["oauth"] = DELETE_FIELD
    elif oauth is not None:
        updates["oauth"] = oauth.model_dump(exclude_none=True)
    if reset_credentials:
        updates["credentials_secret_id"] = ""
        updates["credentials_header"] = ""
    if credentials_secret_id is not None:
        updates["credentials_secret_id"] = credentials_secret_id
        updates["credentials_header"] = ""
    if credentials_header is not None:
        updates["credentials_header"] = credentials_header
        updates["credentials_secret_id"] = ""
    if enabled is not None:
        updates["enabled"] = enabled
    doc_ref.update(updates)
    data.update({k: v for k, v in updates.items() if v is not DELETE_FIELD})
    if updates.get("oauth") is DELETE_FIELD:
        data.pop("oauth", None)
    elif "oauth" in updates and updates["oauth"] is not DELETE_FIELD:
        data["oauth"] = updates["oauth"]
    invalidate_mcp_servers_cache()
    logger.info("Updated MCP server config: %s", server_id)
    return _doc_to_config(server_id, data)


def delete_server(server_id: str) -> bool:
    """Delete an MCP server config. Returns True if it existed."""
    coll = _mcp_servers_collection()
    doc_ref = coll.document(server_id)
    doc = doc_ref.get()
    if not doc.exists:
        return False
    doc_ref.delete()
    invalidate_mcp_servers_cache()
    logger.info("Deleted MCP server config: %s", server_id)
    return True
