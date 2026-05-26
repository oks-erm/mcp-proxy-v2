"""absence.io API v2 client — Hawk auth, POST JSON (same contract as portal absence_tasks)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests
from requests_hawk import HawkAuth

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://app.absence.io/api/v2"
_config: Dict[str, Any] = {"hawk_id": None, "hawk_key": None, "base_url": _DEFAULT_BASE}
_SESSION = requests.Session()


def configure_api(
    hawk_id: Optional[str],
    hawk_key: Optional[str],
    base_url: Optional[str] = None,
) -> None:
    """Called from FastAPI lifespan after loading secrets."""
    _config["hawk_id"] = (hawk_id or "").strip() or None
    _config["hawk_key"] = (hawk_key or "").strip() or None
    _config["base_url"] = (base_url or _DEFAULT_BASE).rstrip("/")


def _hawk_auth() -> Optional[HawkAuth]:
    hid = _config.get("hawk_id")
    hkey = _config.get("hawk_key")
    if not hid or not hkey:
        return None
    return HawkAuth(id=hid, key=hkey, always_hash_content=False)


def post_json(path: str, body: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
    """
    POST JSON to a path under the v2 base (e.g. 'users', 'absences').
    Returns the API JSON body on success, or {\"error\": \"...\", \"status_code\": n, ...} on failure.
    """
    auth = _hawk_auth()
    if not auth:
        return {"error": "absence.io Hawk credentials not configured"}

    base = _config.get("base_url") or _DEFAULT_BASE
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Content-Type": "application/json"}

    try:
        r = _SESSION.post(url, headers=headers, json=body, auth=auth, timeout=timeout)
    except requests.RequestException as e:
        logger.exception("absence.io request failed: %s", url)
        return {"error": str(e)}

    try:
        data = r.json()
    except ValueError:
        return {
            "error": (r.text or "")[:500] or "non-JSON response",
            "status_code": r.status_code,
        }

    if not r.ok:
        msg: Any
        if isinstance(data, dict):
            msg = data.get("message") or data.get("error") or r.reason or "request failed"
        else:
            msg = r.reason or "request failed"
        out: Dict[str, Any] = {"error": msg, "status_code": r.status_code}
        if isinstance(data, dict):
            out["details"] = data
        return out

    return data


def list_users_payload(skip: int, limit: int) -> Dict[str, Any]:
    return {"skip": max(0, skip), "limit": min(max(1, limit), 100)}


def list_absences_payload(
    skip: int,
    limit: int,
    filter_dict: Optional[Dict[str, Any]] = None,
    relations: Optional[list] = None,
    sort_by: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "skip": max(0, skip),
        "limit": min(max(1, limit), 100),
    }
    if filter_dict:
        body["filter"] = filter_dict
    if relations:
        body["relations"] = relations
    if sort_by:
        body["sortBy"] = sort_by
    return body
