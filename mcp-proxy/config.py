"""Configuration for MCP Proxy. Loads from env (local) or from a single JSON secret in GCP Secret Manager (production)."""

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Secret name in GCP Secret Manager holding JSON with: JWT_SECRET_KEY, GOOGLE_OAUTH_CLIENT_ID, etc.
MCP_PROXY_CONFIG_SECRET_ID = os.getenv("MCP_PROXY_CONFIG_SECRET_ID", "mcp-proxy-config")

_ENV = os.getenv("ENV", "")
_secret_config: Optional[Dict[str, Any]] = None


def _load_secret_config() -> None:
    """Load config JSON from GCP Secret Manager once. Skipped when ENV=local."""
    global _secret_config
    if _secret_config is not None:
        return
    if _ENV == "local":
        _secret_config = {}
        return
    secret_id = os.getenv("MCP_PROXY_CONFIG_SECRET_ID", MCP_PROXY_CONFIG_SECRET_ID)
    project_id = os.getenv("GCP_PROJECT_ID", "it-team-hw-project")
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        raw = resp.payload.data.decode("UTF-8")
        _secret_config = json.loads(raw)
        logger.info("Loaded config from Secret Manager: %s", secret_id)
    except Exception as e:
        logger.warning("Could not load config from Secret Manager (%s): %s", secret_id, e)
        _secret_config = {}


def _get(key: str, default: str = "") -> str:
    """Get config value: env override > secret > default."""
    _load_secret_config()
    env_val = os.getenv(key)
    if env_val is not None and env_val != "":
        return env_val
    if _secret_config and key in _secret_config and _secret_config[key] is not None:
        return str(_secret_config[key])
    return default


def _get_int(key: str, default: int = 0) -> int:
    """Get config value as int: env override > secret > default."""
    _load_secret_config()
    env_val = os.getenv(key)
    if env_val is not None and env_val != "":
        try:
            return int(env_val)
        except ValueError:
            pass
    if _secret_config and key in _secret_config and _secret_config[key] is not None:
        try:
            return int(_secret_config[key])
        except (TypeError, ValueError):
            pass
    return default


class _Config:
    """Config values: env override > GCP Secret Manager (single JSON) > defaults."""

    @property
    def GCP_PROJECT_ID(self) -> str:
        return _get("GCP_PROJECT_ID", "it-team-hw-project")

    @property
    def MCP_PROXY_DATABASE(self) -> str:
        return _get("MCP_PROXY_DATABASE", "mcp-proxy-database")

    @property
    def GOOGLE_OAUTH_CLIENT_ID(self) -> str:
        return _get("GOOGLE_OAUTH_CLIENT_ID", "")

    @property
    def JWT_SECRET_KEY(self) -> str:
        return _get("JWT_SECRET_KEY", "")

    @property
    def JWT_SECRET_ID(self) -> str:
        return os.getenv("JWT_SECRET_ID", "mcp-proxy-jwt-secret")

    @property
    def JWT_ACCESS_EXPIRE_MINUTES(self) -> int:
        return _get_int("JWT_ACCESS_EXPIRE_MINUTES", 60)

    @property
    def JWT_ALGORITHM(self) -> str:
        return "HS256"

    @property
    def ENV(self) -> str:
        return _ENV

    @property
    def MCP_PROXY_URL(self) -> str:
        return _get("MCP_PROXY_URL", "")

    @property
    def MCP_PROXY_BOOTSTRAP_ADMIN_EMAILS(self) -> str:
        """Comma-separated emails that become admin+active on login (first deploy / recovery)."""
        return _get("MCP_PROXY_BOOTSTRAP_ADMIN_EMAILS", "")

    @property
    def MCP_USER_KEY_ENCRYPTION_KEY(self) -> str:
        """Optional Fernet key (url-safe base64) for encrypting per-user MCP API keys at rest.

        If unset, a Fernet key is derived from JWT_SECRET_KEY (same security domain as session tokens).
        Set explicitly if you rotate JWT without invalidating stored user keys.
        """
        return _get("MCP_USER_KEY_ENCRYPTION_KEY", "")

    @property
    def MCP_PROXY_APP_SECRET_ENC_KEY(self) -> str:
        """Fernet key (url-safe base64) for encrypting managed app secrets in Firestore."""
        return _get("MCP_PROXY_APP_SECRET_ENC_KEY", "")


# Expose config as module attributes (lazy from secret when needed)
_config = _Config()


def __getattr__(name: str) -> Any:
    """Expose config.* from _Config so 'import config; config.GCP_PROJECT_ID' works."""
    return getattr(_config, name)
