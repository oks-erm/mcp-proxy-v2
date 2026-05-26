"""Breezeway API client — JWT from Firestore tokens/breezeway (same as portal connector)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional

import requests
from google.cloud import firestore
from requests import Response
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "data-warehouse-firestore")
TOKEN_COLLECTION = os.getenv("BREEZEWAY_TOKEN_COLLECTION", "tokens")
TOKEN_DOC_ID = os.getenv("BREEZEWAY_TOKEN_DOC_ID", "breezeway")


def _load_token_from_firestore() -> Optional[str]:
    db = firestore.Client(project=PROJECT_ID, database=FIRESTORE_DATABASE)
    doc = db.collection(TOKEN_COLLECTION).document(TOKEN_DOC_ID).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    tok = data.get("token")
    return str(tok) if tok else None


class BreezewayMcpClient:
    def __init__(self) -> None:
        self.base_url = os.getenv(
            "BREEZEWAY_API_BASE",
            "https://api.breezeway.io/public/inventory/v1",
        )
        self.company_id = os.getenv("BREEZEWAY_COMPANY_ID", "8617")
        env = os.getenv("ENV", "")
        if env == "local":
            self._token = os.getenv("BREEZEWAY_TOKEN", "")
        else:
            self._token = _load_token_from_firestore() or ""
        if not self._token:
            raise ValueError(
                "Breezeway JWT missing: set BREEZEWAY_TOKEN for local or ensure Firestore "
                f"{TOKEN_COLLECTION}/{TOKEN_DOC_ID} has field 'token'"
            )
        self._session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._session.mount("https://", adapter)

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"JWT {self._token}",
        }

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        return self._session.get(url, headers=self._headers(), params=params or {}, timeout=30)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
    ) -> Response:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        return self._session.request(
            method=method.upper(),
            url=url,
            headers=self._headers(),
            params=params or {},
            json=json,
            timeout=30,
        )

    @staticmethod
    def _parse_response_body(response: Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def list_properties_page(self, page: int = 1, limit: int = 100) -> Dict[str, Any]:
        limit = min(max(limit, 1), 100)
        r = self._get("property", params={"limit": limit, "page": page})
        r.raise_for_status()
        return r.json()

    def get_property(self, property_id: int) -> Dict[str, Any]:
        """GET /property/{id} — documented as Retrieve property in Breezeway inventory API."""
        r = self._get(f"property/{int(property_id)}")
        r.raise_for_status()
        return r.json()

    def list_all_users(self) -> List[Dict[str, Any]]:
        r = self._get("people")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError(f"Unexpected /people response: {type(data)}")
        return data

    def get_reservation_by_external_id(
        self, external_reservation_id: str, allow_multiple: bool = False
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if allow_multiple:
            params["allow_multiple"] = "true"
        r = self._get(f"reservation/external-id/{external_reservation_id}", params=params)
        r.raise_for_status()
        return r.json()

    def list_tasks_page(
        self,
        *,
        page: int = 1,
        limit: int = 100,
        home_id: Optional[int] = None,
        reference_property_id: Optional[str] = None,
        scheduled_date: Optional[str] = None,
        created_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        assignee_ids: Optional[List[int]] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "limit": min(max(limit, 1), 100),
            "page": max(page, 1),
        }
        if home_id is not None:
            params["home_id"] = int(home_id)
        if reference_property_id:
            params["reference_property_id"] = reference_property_id
        if scheduled_date:
            params["scheduled_date"] = scheduled_date
        if created_at:
            params["created_at"] = created_at
        if finished_at:
            params["finished_at"] = finished_at
        if updated_at:
            params["updated_at"] = updated_at
        if assignee_ids:
            params["assignee_ids"] = ",".join(str(int(value)) for value in assignee_ids)
        if sort_by:
            params["sort_by"] = sort_by
        if sort_order:
            params["sort_order"] = sort_order
        r = self._get("task/", params=params)
        r.raise_for_status()
        return r.json()

    def get_task_comments(self, task_id: int) -> List[Dict[str, Any]]:
        r = self._get(f"task/{int(task_id)}/comments")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError(f"Unexpected task comments response: {type(data)}")
        return data

    def update_task(self, task_id: int, updates: Dict[str, Any]) -> Any:
        if not isinstance(updates, dict) or not updates:
            raise ValueError("updates must be a non-empty object")
        r = self._request("PATCH", f"task/{int(task_id)}", json=updates)
        r.raise_for_status()
        return self._parse_response_body(r)

    def move_task(self, task_id: int, action: str) -> Any:
        action_norm = (action or "").strip().lower()
        if action_norm not in {"close", "approve", "reopen"}:
            raise ValueError("action must be one of: close, approve, reopen")
        r = self._request("POST", f"task/{int(task_id)}/{action_norm}")
        r.raise_for_status()
        return self._parse_response_body(r)

    def iter_properties(self, page_size: int = 100) -> Iterator[List[Dict[str, Any]]]:
        page = 1
        total_pages: Optional[int] = None
        while True:
            body = self.list_properties_page(page=page, limit=page_size)
            if page == 1:
                total_pages = body.get("total_pages", 0)
            rows = body.get("results", [])
            if not rows:
                break
            yield rows
            if total_pages is not None and page >= total_pages:
                break
            page += 1
            time.sleep(0.05)
