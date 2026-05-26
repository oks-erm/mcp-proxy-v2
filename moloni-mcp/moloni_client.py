"""Moloni API client (read-only POST endpoints used by the portal integration)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.moloni.pt/v1"
MAX_RETRIES = 3
RETRY_DELAY = 2.0
REQUEST_WAIT = 0.5
TOKEN_EXPIRY_BUFFER = 300


class MoloniClient:
    """Minimal Moloni v1 client: password grant, refresh, and JSON POST with access_token query param."""

    def __init__(self, creds: Dict[str, Any]) -> None:
        self.developer_id = creds.get("developer_id")
        self.client_secret = creds.get("client_secret")
        self.username = creds.get("username")
        self.password = creds.get("password")
        self.company_id = creds.get("company_id")
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expiry = 0.0
        self._session = requests.Session()

    def _authenticate(self) -> None:
        url = f"{BASE_URL}/grant/"
        params = {
            "grant_type": "password",
            "client_id": self.developer_id,
            "client_secret": self.client_secret,
            "username": self.username,
            "password": self.password,
        }
        r = self._session.get(url, params=params, timeout=60.0)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Moloni auth failed: {data.get('error')} {data.get('error_description', '')}")
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token")
        self.token_expiry = time.time() + float(data.get("expires_in", 3600))

    def _refresh(self) -> None:
        if not self.refresh_token:
            self._authenticate()
            return
        url = f"{BASE_URL}/grant/"
        params = {
            "grant_type": "refresh_token",
            "client_id": self.developer_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }
        r = self._session.get(url, params=params, timeout=60.0)
        try:
            data = r.json()
        except ValueError:
            self._authenticate()
            return
        if isinstance(data, dict) and data.get("error"):
            self._authenticate()
            return
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.token_expiry = time.time() + float(data.get("expires_in", 3600))

    def _token_expired(self) -> bool:
        return time.time() + TOKEN_EXPIRY_BUFFER >= self.token_expiry

    def ensure_token(self) -> None:
        if not self.access_token:
            self._authenticate()
        elif self._token_expired():
            self._refresh()

    def api_post(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Any:
        """POST JSON body to ``endpoint`` (e.g. ``invoices/getAll``). Returns parsed JSON (dict or list)."""
        self.ensure_token()
        payload = dict(data or {})
        url = f"{BASE_URL}/{endpoint.strip('/')}/"
        params = {
            "access_token": self.access_token,
            "json": "true",
            "human_errors": "true",
        }
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if attempt > 1:
                    time.sleep(REQUEST_WAIT)
                if self._token_expired():
                    self._refresh()
                params["access_token"] = self.access_token

                r = self._session.post(
                    url,
                    params=params,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=120.0,
                )
                try:
                    json_data = r.json()
                except ValueError:
                    return {
                        "error": "invalid_response",
                        "error_description": (r.text[:500] if r.text else f"HTTP {r.status_code}"),
                    }

                if r.status_code >= 400:
                    return (
                        json_data
                        if isinstance(json_data, dict)
                        else {"error": "http_error", "error_description": str(json_data)}
                    )

                if isinstance(json_data, list) and any(
                    isinstance(err, dict) and err.get("code") == "4 number" for err in json_data
                ):
                    raise requests.RequestException("4 number error")

                if isinstance(json_data, dict) and json_data.get("error"):
                    err = str(json_data.get("error", ""))
                    if "401" in err or "403" in err:
                        self._refresh()
                        params["access_token"] = self.access_token
                        raise requests.RequestException(err)
                    return json_data

                return json_data
            except requests.RequestException as e:
                last_exc = e
                logger.warning("Moloni request attempt %s failed: %s", attempt, e)
                if attempt == MAX_RETRIES:
                    break
                time.sleep(RETRY_DELAY)

        return {
            "error": "request_failed",
            "error_description": str(last_exc) if last_exc else "unknown",
        }


_client: Optional[MoloniClient] = None


def configure_client(creds: Optional[Dict[str, Any]]) -> None:
    """Replace global client (``None`` disables outbound calls until reconfigured)."""
    global _client
    if creds is None:
        _client = None
        return
    _client = MoloniClient(creds)


def get_client() -> MoloniClient:
    if _client is None:
        raise RuntimeError("Moloni client not configured")
    return _client


def api_post(endpoint: str, data: Optional[Dict[str, Any]] = None) -> Any:
    """Module-level POST for tests (monkeypatch this symbol)."""
    return get_client().api_post(endpoint, data)


def unwrap_single_item(raw: Any) -> Any:
    response = raw
    while isinstance(response, list) and len(response) == 1:
        response = response[0]
    return response
