"""Pipedrive REST API client (read-only GET): v1 resources and v2 search."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.pipedrive.com/v1"
_config: Dict[str, Any] = {"token": None, "base_url": _DEFAULT_BASE}
_SESSION = requests.Session()


def configure_api(token: Optional[str], base_url: Optional[str] = None) -> None:
    """Called from FastAPI lifespan after loading secrets."""
    _config["token"] = (token or "").strip() or None
    _config["base_url"] = (base_url or _DEFAULT_BASE).rstrip("/")


def _headers() -> Dict[str, str]:
    token = _config.get("token")
    if not token:
        return {}
    return {"x-api-token": token}


def _api_root() -> str:
    """
    Host root for API v2 paths. Strips trailing /api/v1 or /v1 from the configured v1 base
    (e.g. https://api.pipedrive.com/v1 -> https://api.pipedrive.com).
    """
    base = (_config.get("base_url") or _DEFAULT_BASE).rstrip("/")
    if base.endswith("/api/v1"):
        return base[: -len("/api/v1")]
    if base.endswith("/v1"):
        return base[: -len("/v1")]
    return base


def _http_get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Dict[str, Any]:
    token = _config.get("token")
    if not token:
        return {"success": False, "error": "Pipedrive API token not configured"}

    q = {k: v for k, v in (params or {}).items() if v is not None}

    try:
        r = _SESSION.get(url, headers=_headers(), params=q, timeout=timeout)
    except requests.RequestException as e:
        logger.exception("Pipedrive request failed: %s", url)
        return {"success": False, "error": str(e)}

    try:
        data = r.json()
    except ValueError:
        return {
            "success": False,
            "error": r.text[:500] if r.text else "non-JSON response",
            "status_code": r.status_code,
        }

    if not r.ok:
        err = data if isinstance(data, dict) else {}
        msg = err.get("error") or err.get("error_info") or r.reason or "request failed"
        return {"success": False, "error": msg, "status_code": r.status_code, **({"details": data} if data else {})}

    return data


def get_json(path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Dict[str, Any]:
    """
    GET path relative to v1 base (e.g. 'deals'). Query params with None values are omitted.
    Returns Pipedrive JSON body or {"success": False, "error": "...", "status_code": n}.
    """
    base = _config.get("base_url") or _DEFAULT_BASE
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    return _http_get_json(url, params, timeout)


def get_v2_json(path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Dict[str, Any]:
    """
    GET under /api/v2/{path} using the same API root and x-api-token as v1.
    """
    root = _api_root()
    url = f"{root}/api/v2/{path.lstrip('/')}"
    return _http_get_json(url, params, timeout)
