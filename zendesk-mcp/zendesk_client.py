"""Zendesk API client for MCP: read-only custom objects, views, ticket search, audits, users, reservations."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

import requests
from google.cloud import secretmanager

logger = logging.getLogger(__name__)

# Sentinel: Zendesk returned HTTP 404 (distinct from transport failure returning None).
ZENDESK_NOT_FOUND = object()

PROJECT_ID = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
ZENDESK_SECRET_ID = os.getenv("ZENDESK_SECRET_ID") or os.getenv("ZENDESK_CREDENTIALS_SECRET_ID", "zendesk-credentials")


def _get_secret_payload(secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")


# Zendesk allows up to 20 view ids per count_many request; bulk endpoint is rate-limited (e.g. 6 req/min).
VIEW_COUNT_MANY_MAX_IDS = 20
VIEW_COUNT_MANY_BATCH_SLEEP_S = 10
MAX_VIEW_IDS_FOR_COUNT_MANY = 200


class ZendeskService:
    """Zendesk API client (read-only MCP surface): custom object reads, views, tickets, audits, users, reservations."""

    def __init__(self) -> None:
        env = os.getenv("ENV", "")
        if env == "local":
            self.email = os.getenv("ZD_EMAIL", "")
            self.api_token = os.getenv("ZD_TOKEN", "")
            self.subdomain = os.getenv("ZD_SUBDOMAIN", "")
        else:
            raw = _get_secret_payload(ZENDESK_SECRET_ID)
            data = json.loads(raw) if raw.strip().startswith("{") else {}
            if not isinstance(data, dict):
                raise ValueError("zendesk-credentials secret must be JSON")
            self.email = str(data.get("zd_email") or "")
            self.api_token = str(data.get("zd_token") or "")
            self.subdomain = str(data.get("zd_subdomain") or "")

        if not all([self.email, self.api_token, self.subdomain]):
            raise ValueError("Zendesk credentials incomplete (need email, token, subdomain)")

        self.base_url = f"https://{self.subdomain}.zendesk.com/api/v2"
        self.session = requests.Session()
        self.session.auth = (f"{self.email}/token", self.api_token)

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            response = self.session.request(method=method, url=url, params=params, json=json_data, timeout=60)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning("Zendesk rate limit; sleeping %s s", retry_after)
                time.sleep(retry_after)
                return self._request(method, endpoint, params, json_data)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"ok": True}
            return response.json()
        except requests.exceptions.HTTPError as e:
            resp = e.response
            if resp is not None and resp.status_code == 404:
                return ZENDESK_NOT_FOUND
            logger.error("Zendesk %s %s HTTP error: %s", method, endpoint, e)
            return None
        except requests.exceptions.RequestException as e:
            logger.error("Zendesk %s %s failed: %s", method, endpoint, e)
            return None

    def _zendesk_origin_prefix(self) -> str:
        return f"https://{self.subdomain}.zendesk.com"

    def _get_json_url(self, url: str) -> Any:
        """GET a full Zendesk API URL (e.g. ``next_page``). Refuses other origins."""
        prefix = self._zendesk_origin_prefix()
        if not isinstance(url, str) or not url.startswith(prefix):
            logger.error("Refusing non-Zendesk URL for _get_json_url")
            return None
        try:
            response = self.session.get(url, timeout=60)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning("Zendesk rate limit; sleeping %s s", retry_after)
                time.sleep(retry_after)
                return self._get_json_url(url)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"ok": True}
            return response.json()
        except requests.exceptions.HTTPError as e:
            resp = e.response
            if resp is not None and resp.status_code == 404:
                return ZENDESK_NOT_FOUND
            logger.error("Zendesk GET URL HTTP error: %s", e)
            return None
        except requests.exceptions.RequestException as e:
            logger.error("Zendesk GET URL failed: %s", e)
            return None

    def list_views(
        self,
        *,
        next_url: Optional[str] = None,
        active_only: bool = True,
        access: Optional[str] = None,
        per_page: Optional[int] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        page_after: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> Any:
        """List views (``GET /views.json`` or active sidebar set ``GET /views/active.json``).

        Pass ``next_url`` from a prior response's ``next_page`` to follow pagination.
        """
        if next_url:
            return self._get_json_url(next_url.strip())
        params: Dict[str, Any] = {}
        if access:
            params["access"] = access
        if sort_by is not None:
            params["sort_by"] = sort_by
        if sort_order is not None:
            params["sort_order"] = sort_order
        if active_only:
            return self._request("GET", "views/active.json", params=params or None)
        if per_page is not None:
            params["per_page"] = per_page
        if page is not None:
            params["page"] = page
        if page_size is not None:
            params["page[size]"] = page_size
        if page_after:
            params["page[after]"] = page_after
        return self._request("GET", "views.json", params=params or None)

    def list_view_tickets(
        self,
        view_id: int,
        *,
        next_url: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        per_page: Optional[int] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        page_after: Optional[str] = None,
    ) -> Any:
        """List tickets for a view (``GET /views/{id}/tickets``). Use ``next_url`` for ``next_page`` pagination."""
        if next_url:
            return self._get_json_url(next_url.strip())
        params: Dict[str, Any] = {}
        if sort_by is not None:
            params["sort_by"] = sort_by
        if sort_order is not None:
            params["sort_order"] = sort_order
        if per_page is not None:
            params["per_page"] = per_page
        if page is not None:
            params["page"] = page
        if page_size is not None:
            params["page[size]"] = page_size
        if page_after:
            params["page[after]"] = page_after
        return self._request("GET", f"views/{int(view_id)}/tickets.json", params=params or None)

    def get_view_ticket_counts(self, view_ids: List[int]) -> Any:
        """Return merged ``view_counts`` from ``GET /views/count_many`` (batches of up to 20 ids)."""
        seen: set[int] = set()
        ordered: List[int] = []
        for raw in view_ids:
            try:
                vid = int(raw)
            except (TypeError, ValueError):
                continue
            if vid <= 0 or vid in seen:
                continue
            seen.add(vid)
            ordered.append(vid)
        if not ordered:
            return {"view_counts": []}
        merged: List[Dict[str, Any]] = []
        for i in range(0, len(ordered), VIEW_COUNT_MANY_MAX_IDS):
            batch = ordered[i : i + VIEW_COUNT_MANY_MAX_IDS]
            ids_param = ",".join(str(x) for x in batch)
            data = self._request("GET", "views/count_many", params={"ids": ids_param})
            if data is ZENDESK_NOT_FOUND:
                return ZENDESK_NOT_FOUND
            if data is None:
                return None
            if not isinstance(data, dict):
                return None
            vc = data.get("view_counts")
            if isinstance(vc, list):
                merged.extend(vc)
            if i + VIEW_COUNT_MANY_MAX_IDS < len(ordered):
                time.sleep(VIEW_COUNT_MANY_BATCH_SLEEP_S)
        return {"view_counts": merged}

    def get_custom_object_records(self, custom_object_key: str, **params: Any) -> Any:
        return self._request("GET", f"custom_objects/{custom_object_key}/records", params=params or None)

    def get_custom_object_record(self, custom_object_key: str, record_id: str) -> Any:
        return self._request("GET", f"custom_objects/{custom_object_key}/records/{record_id}")

    def search_custom_object_records(self, custom_object_key: str, query: str = "", **params: Any) -> Any:
        p = dict(params)
        p["query"] = query
        return self._request("GET", f"custom_objects/{custom_object_key}/records/search", params=p)

    def _fetch_tickets_page(
        self, url: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3
    ) -> Optional[Dict[str, Any]]:
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=30)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.info("Rate limit hit. Waiting %s seconds...", retry_after)
                    time.sleep(retry_after)
                    return self._fetch_tickets_page(url, params, max_retries)
                response.raise_for_status()
                return response.json()
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError,
            ) as e:
                if attempt < max_retries - 1:
                    wait_time = 2**attempt
                    logger.warning(
                        "Network error fetching tickets (attempt %s/%s): %s",
                        attempt + 1,
                        max_retries,
                        e,
                    )
                    time.sleep(wait_time)
                else:
                    logger.error("Error fetching tickets after %s attempts: %s", max_retries, e)
                    return None
            except requests.exceptions.RequestException as e:
                logger.error("HTTP error fetching tickets: %s", e)
                return None
        return None

    def search_tickets(
        self,
        query_string: str,
        limit: int = 1000,
        batch_size: int = 100,
        max_retries: int = 3,
    ) -> List[Dict[str, Any]]:
        base_url = f"{self.base_url}/search.json"
        params = {
            "query": query_string,
            "sort_by": "created_at",
            "sort_order": "asc",
            "per_page": min(batch_size, 100),
        }
        all_tickets: List[Dict[str, Any]] = []
        current_url = base_url
        page = 1
        total_count: Optional[int] = None

        while current_url and len(all_tickets) < limit:
            logger.info("Fetching page %s...", page)
            response_data = self._fetch_tickets_page(current_url, params if page == 1 else None, max_retries)
            if not response_data:
                logger.warning("Error fetching data. Stopping.")
                break
            if page == 1:
                total_count = response_data.get("count", 0)
                logger.info("Total tickets found: %s", total_count)
                if total_count == 0:
                    break
            tickets = response_data.get("results", [])
            if tickets:
                all_tickets.extend(tickets)
                logger.info("Retrieved %s of %s tickets...", len(all_tickets), total_count)
            else:
                break
            current_url = response_data.get("next_page")
            if current_url:
                page += 1
                time.sleep(2)
        return all_tickets[:limit]

    def get_ticket(
        self,
        ticket_id: int,
        include: Optional[str] = None,
    ) -> Any:
        """GET /api/v2/tickets/{id}.json — see Zendesk Show Ticket API."""
        params: Dict[str, Any] = {}
        if include:
            params["include"] = include
        return self._request("GET", f"tickets/{int(ticket_id)}.json", params=params or None)

    def get_ticket_audits(
        self,
        ticket_ids: Union[int, List[int]],
        max_retries: int = 3,
    ) -> List[Dict[str, Any]]:
        if isinstance(ticket_ids, int):
            ticket_ids = [ticket_ids]
        all_audits: List[Dict[str, Any]] = []
        for i, ticket_id in enumerate(ticket_ids, 1):
            url = f"{self.base_url}/tickets/{ticket_id}/audits.json"
            for attempt in range(max_retries):
                try:
                    response = self.session.get(url, timeout=30)
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        logger.info("Rate limited! Waiting %s seconds...", retry_after)
                        time.sleep(retry_after)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    logger.info(
                        "[%s/%s] Ticket %s has %s audits",
                        i,
                        len(ticket_ids),
                        ticket_id,
                        len(data.get("audits", [])),
                    )
                    all_audits.extend(data.get("audits", []))
                    break
                except (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.SSLError,
                ) as e:
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            "Network error for ticket %s (attempt %s/%s): %s",
                            ticket_id,
                            attempt + 1,
                            max_retries,
                            e,
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(
                            "Error fetching audits for ticket %s after %s attempts: %s",
                            ticket_id,
                            max_retries,
                            e,
                        )
                        break
                except Exception as e:
                    logger.error("Error fetching audits for ticket %s: %s", ticket_id, e)
                    break
            time.sleep(0.5)
        return all_audits

    def get_users(
        self,
        user_ids: List[int],
        max_retries: int = 3,
    ) -> List[Dict[str, Any]]:
        chunk_size = 100
        all_users: List[Dict[str, Any]] = []
        for i in range(0, len(user_ids), chunk_size):
            chunk = user_ids[i : i + chunk_size]
            ids_param = ",".join(str(uid) for uid in chunk)
            url = f"{self.base_url}/users/show_many"
            params = {"ids": ids_param}
            for attempt in range(max_retries):
                try:
                    response = self.session.get(url, params=params, timeout=30)
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        logger.info("Rate limited! Waiting %s seconds...", retry_after)
                        time.sleep(retry_after)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    users = data.get("users", [])
                    all_users.extend(users)
                    logger.info("Retrieved %s users (total: %s)", len(users), len(all_users))
                    break
                except (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.SSLError,
                ) as e:
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            "Network error for users chunk (attempt %s/%s): %s",
                            attempt + 1,
                            max_retries,
                            e,
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(
                            "Error fetching users chunk after %s attempts: %s",
                            max_retries,
                            e,
                        )
                        break
                except Exception as e:
                    logger.error("Error fetching users chunk: %s", e)
                    break
            time.sleep(1)
        return all_users

    def fetch_reservation_record(
        self,
        reservation_id: str,
        max_retries: int = 3,
    ) -> Any:
        url = f"{self.base_url}/custom_objects/reservations_data/records/search"
        params = {"query": reservation_id}
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=30)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.info("Rate limited on reservation lookup. Sleeping %s seconds...", retry_after)
                    time.sleep(retry_after)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.exceptions.HTTPError as e:
                resp = e.response
                if resp is not None and resp.status_code == 404:
                    return ZENDESK_NOT_FOUND
                logger.error("HTTP error fetching reservation record %s: %s", reservation_id, e)
                return None
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError,
            ) as e:
                if attempt < max_retries - 1:
                    wait_time = 2**attempt
                    logger.warning(
                        "Network error fetching reservation %s (attempt %s/%s): %s",
                        reservation_id,
                        attempt + 1,
                        max_retries,
                        e,
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(
                        "Error fetching reservation %s after %s attempts: %s",
                        reservation_id,
                        max_retries,
                        e,
                    )
                    return None
            except requests.exceptions.RequestException as e:
                logger.error("Request error fetching reservation record %s: %s", reservation_id, e)
                return None
            except Exception as e:
                logger.error("Unexpected error fetching reservation record %s: %s", reservation_id, e)
                return None
        return None
