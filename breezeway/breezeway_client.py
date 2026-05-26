"""Stateless Breezeway API client backed by Firestore JWTs."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from shared.shared.firestore import FirestoreService  # noqa: E402

logger = logging.getLogger(__name__)


class BreezewayClient:
    """Lightweight Breezeway wrapper that mirrors the portal connector."""

    def __init__(self) -> None:
        self.base_url = "https://api.breezeway.io/public/inventory/v1"
        self.company_id = "8617"
        self.firestore_service = FirestoreService()
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._token: Optional[str] = None
        self._headers: Dict[str, str] = {"Content-Type": "application/json"}
        self._refresh_token()

    def _refresh_token(self) -> None:
        document = self.firestore_service.get_document("tokens", "breezeway")
        token = document.get("token") if document else None
        if not token:
            raise RuntimeError("Breezeway token missing from Firestore (tokens/breezeway)")
        self._token = token
        self._headers["Authorization"] = f"JWT {token}"

    def _make_request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        method: str = "GET",
        json: Any = None,
    ) -> requests.Response:
        if not self._token:
            self._refresh_token()
        try:
            if method == "GET":
                response = self.session.get(url, headers=self._headers, params=params, timeout=20)
            elif method == "POST":
                response = self.session.post(url, headers=self._headers, params=params, json=json, timeout=20)
            elif method == "DELETE":
                response = self.session.delete(url, headers=self._headers, params=params, timeout=20)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            response.raise_for_status()
            return response
        except requests.HTTPError as http_err:
            logger.error("Breezeway HTTP error %s %s: %s", method, url, http_err)
            raise
        except Exception as exc:
            logger.error("Breezeway request failed %s %s: %s", method, url, exc)
            raise

    def get_properties_page(self, page: int = 1, limit: int = 100) -> Dict[str, Any]:
        """Fetch a single page of Breezeway properties."""
        params = {
            "limit": max(1, min(limit, 100)),
            "page": max(1, page),
        }
        response = self._make_request(f"{self.base_url}/property", params=params)
        data = response.json()
        return {
            "base_url": self.base_url,
            "properties": data.get("results", []),
            "page": params["page"],
            "limit": params["limit"],
            "total_pages": data.get("total_pages", 0),
            "total_results": data.get("total_results", 0),
        }

    def get_tasks_page(
        self,
        page: int = 1,
        limit: int = 100,
        reference_property_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch a single page of Breezeway tasks, optionally scoped to a property."""
        params: Dict[str, Any] = {
            "limit": max(1, min(limit, 100)),
            "page": max(1, page),
        }
        if reference_property_id:
            params["reference_property_id"] = reference_property_id
        response = self._make_request(f"{self.base_url}/task/", params=params)
        data = response.json()
        return {
            "tasks": data.get("results", []),
            "page": params["page"],
            "limit": params["limit"],
            "total_pages": data.get("total_pages", 0),
            "total_results": data.get("total_results", 0),
        }

    def get_users(self) -> List[Dict[str, Any]]:
        """Fetch all Breezeway people."""
        response = self._make_request(f"{self.base_url}/people")
        users = response.json()
        if not isinstance(users, list):
            raise RuntimeError("Unexpected Breezeway users response")
        return users

    def get_reservation(self, reservation_id: str, allow_multiple: bool = False) -> Dict[str, Any]:
        """Fetch a reservation by external ID."""
        params: Dict[str, Any] = {}
        if allow_multiple:
            params["allow_multiple"] = "true"
        response = self._make_request(f"{self.base_url}/reservation/external-id/{reservation_id}", params=params)
        return response.json()
