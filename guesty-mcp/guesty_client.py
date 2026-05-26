"""Guesty API client for MCP server. Self-contained; no shared package dependency."""

import json
import logging
import os
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import requests
from google.cloud import firestore, secretmanager
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Secret name for Guesty API credentials (client_id, client_secret)
GUESTY_SECRET_ID = os.getenv("GUESTY_SECRET_ID", "guesty-api-key")
FIRESTORE_COLLECTION = "tokens"
FIRESTORE_DOC_ID = "guesty"


class WebhookEvent(str, Enum):
    """Available webhook events from Guesty API."""

    GUEST_CREATED = "guest.created"
    GUEST_DELETED = "guest.deleted"
    GUEST_UPDATED = "guest.updated"
    LISTING_NEW = "listing.new"
    LISTING_UPDATED = "listing.updated"
    LISTING_REMOVED = "listing.removed"
    LISTING_CALENDAR_UPDATED = "listing.calendar.updated"
    PAYMENTS_FAILED = "payments.failed"
    RESERVATION_MESSAGE_RECEIVED = "reservation.messageReceived"
    RESERVATION_NEW = "reservation.new"
    RESERVATION_UPDATED = "reservation.updated"
    RESERVATION_MESSAGE_SENT = "reservation.messageSent"
    PAYMENTS_METHOD_RECEIVED = "payments.method.received"
    PAYMENTS_RECEIVED = "payments.received"
    PAYMENTS_REFUNDED = "payments.refunded"
    PAYMENTS_OVERDUE = "payments.overdue"
    PAYMENTS_OVERCHARGED = "payments.overcharged"
    PAYMENTS_OVERCHARGE_EXPECTED = "payments.overcharge.expected"
    TASK_CREATED = "task.created"
    TASK_DELETED = "task.deleted"
    TASK_UPDATED = "task.updated"
    RESERVATION_UPDATE_SHORTLIST = "reservation_update_shortlist"


class GuestyToken(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    scope: str
    expires_at: Optional[datetime] = None


def _get_guesty_credentials() -> Dict[str, Any]:
    """Load Guesty client_id and client_secret from GCP Secret Manager."""
    project_id = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{GUESTY_SECRET_ID}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    payload = response.payload.data.decode("UTF-8")
    return json.loads(payload, strict=False)


def _get_firestore_doc(collection: str, document_id: str) -> Optional[Dict[str, Any]]:
    """Load a document from Firestore."""
    project_id = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
    database = os.getenv("GOOGLE_CLOUD_DATABASE", "data-warehouse-firestore")
    db = firestore.Client(project=project_id, database=database)
    doc_ref = db.collection(collection).document(document_id)
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    return None


def _set_firestore_doc(collection: str, document_id: str, data: Dict[str, Any]) -> None:
    """Save a document to Firestore."""
    project_id = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project"))
    database = os.getenv("GOOGLE_CLOUD_DATABASE", "data-warehouse-firestore")
    db = firestore.Client(project=project_id, database=database)
    doc_ref = db.collection(collection).document(document_id)
    doc_ref.set(data)


class GuestyService:
    def __init__(self):
        self.base_url = "https://open-api.guesty.com"
        self.token_endpoint = f"{self.base_url}/oauth2/token"
        self.api_endpoint = f"{self.base_url}/v1"
        self._initialized = False
        self.client_id = None
        self.client_secret = None
        self.current_token = None

    def _ensure_initialized(self):
        if self._initialized:
            return
        creds = _get_guesty_credentials()
        self.client_id = creds.get("client_id")
        self.client_secret = creds.get("client_secret")
        self._load_or_generate_token()
        self._initialized = True

    def _load_credentials_from_firestore(self) -> Optional[Dict[str, Any]]:
        try:
            return _get_firestore_doc(FIRESTORE_COLLECTION, FIRESTORE_DOC_ID)
        except Exception as e:
            logger.warning("Failed to load Guesty credentials from Firestore: %s", e)
            return None

    def _save_credentials_to_firestore(self, token_data: Dict[str, Any]):
        try:
            _set_firestore_doc(FIRESTORE_COLLECTION, FIRESTORE_DOC_ID, token_data)
            logger.info("Guesty credentials saved to Firestore")
        except Exception as e:
            logger.error("Failed to save Guesty credentials to Firestore: %s", e)

    def _generate_access_token(self) -> Optional[GuestyToken]:
        logger.info("Generating new Guesty access token...")
        try:
            headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
            data = {
                "grant_type": "client_credentials",
                "scope": "open-api",
                "client_secret": self.client_secret,
                "client_id": self.client_id,
            }
            response = requests.post(self.token_endpoint, headers=headers, data=data)
            if response.status_code == 200:
                token_response = response.json()
                expires_at = datetime.now() + timedelta(seconds=token_response["expires_in"])
                token = GuestyToken(
                    access_token=token_response["access_token"],
                    token_type=token_response["token_type"],
                    expires_in=token_response["expires_in"],
                    scope=token_response["scope"],
                    expires_at=expires_at,
                )
                token_data = {
                    "access_token": token.access_token,
                    "token_type": token.token_type,
                    "expires_in": token.expires_in,
                    "scope": token.scope,
                    "expires_at": expires_at.timestamp(),
                    "client_id": self.client_id,
                }
                self._save_credentials_to_firestore(token_data)
                logger.info("Access token generated successfully. Expires at: %s", expires_at)
                return token
            logger.error(
                "Failed to generate access token. Status: %s, Response: %s", response.status_code, response.text
            )
            return None
        except Exception as e:
            logger.error("Error generating access token: %s", e)
            return None

    def _load_or_generate_token(self):
        credentials = self._load_credentials_from_firestore()
        if credentials and "access_token" in credentials:
            expires_at_timestamp = credentials.get("expires_at", 0)
            expires_at = datetime.fromtimestamp(expires_at_timestamp)
            buffer_time = datetime.now() + timedelta(minutes=60)
            if expires_at > buffer_time:
                logger.info("Using existing valid Guesty access token")
                self.current_token = GuestyToken(
                    access_token=credentials["access_token"],
                    token_type=credentials["token_type"],
                    expires_in=credentials["expires_in"],
                    scope=credentials["scope"],
                    expires_at=expires_at,
                )
                return
        logger.info("No valid token found, generating new one...")
        self.current_token = self._generate_access_token()
        if not self.current_token:
            raise Exception("Failed to generate Guesty access token")

    def _refresh_token_if_needed(self):
        if not self.current_token:
            self.current_token = self._generate_access_token()
            return
        buffer_time = datetime.now() + timedelta(minutes=60)
        if self.current_token.expires_at and self.current_token.expires_at <= buffer_time:
            logger.info("Token expires soon, refreshing...")
            self.current_token = self._generate_access_token()

    def _make_authenticated_request(
        self, endpoint: str, method: str = "GET", params: Optional[Dict] = None, data: Optional[Dict] = None
    ) -> Optional[Dict[str, Any]]:
        self._ensure_initialized()
        url = f"{self.api_endpoint}/{endpoint}"
        self._refresh_token_if_needed()
        if not self.current_token:
            logger.error("No valid access token available")
            return None
        headers = {"accept": "application/json", "Authorization": f"Bearer {self.current_token.access_token}"}
        try:
            response = requests.request(method, url, headers=headers, params=params, json=data)
            if response.status_code == 403:
                logger.warning("Received 403, token may be expired. Refreshing token...")
                self.current_token = self._generate_access_token()
                if self.current_token:
                    headers["Authorization"] = f"Bearer {self.current_token.access_token}"
                    response = requests.request(method, url, headers=headers, params=params, json=data)
                else:
                    return None
            if response.status_code in [200, 201]:
                return response.json()
            if response.status_code == 204:
                return {"success": True, "status": "no_content"}
            logger.error("Guesty API error %s: %s", response.status_code, response.text)
            return None
        except Exception as e:
            logger.error("Request failed: %s", e)
            return None

    def get_reservations(self, **kwargs) -> Optional[Dict[str, Any]]:
        return self._make_authenticated_request("reservations", params=kwargs)

    def search_reservations(
        self,
        filters: Optional[List[Dict[str, Any]]] = None,
        fields: Optional[str] = None,
        sort: str = "_id",
        limit: int = 25,
        skip: int = 0,
        fetch_all: bool = False,
    ) -> Optional[Dict[str, Any]]:
        params = {"sort": sort, "limit": min(limit, 100), "skip": skip}
        if fields:
            params["fields"] = fields
        if filters:
            params["filters"] = json.dumps(filters)
        if not fetch_all:
            return self._make_authenticated_request("reservations", params=params)
        aggregated_results: List[Dict[str, Any]] = []
        total_count: Optional[int] = None
        page_limit = params["limit"]
        current_skip = params["skip"]
        saw_successful_page = False
        while True:
            params["skip"] = current_skip
            page = self._make_authenticated_request("reservations", params=params)
            if not page:
                if not saw_successful_page:
                    return None
                break
            saw_successful_page = True
            page_results = page.get("results", [])
            if total_count is None:
                total_count = page.get("count")
            aggregated_results.extend(page_results)
            if len(page_results) < page_limit or (total_count is not None and current_skip + page_limit >= total_count):
                break
            current_skip += page_limit
        return {
            "results": aggregated_results,
            "count": total_count if total_count is not None else len(aggregated_results),
        }

    def get_listings(self, **kwargs) -> Optional[Dict[str, Any]]:
        return self._make_authenticated_request("listings", params=kwargs)

    def get_listing(self, listing_id: str) -> Optional[Dict[str, Any]]:
        """GET /v1/listings/{id} — single listing document."""
        lid = (listing_id or "").strip()
        if not lid:
            return None
        return self._make_authenticated_request(f"listings/{lid}")

    def search_listings(
        self, limit: int = 25, skip: int = 0, fetch_all: bool = False, sort: str = "_id", **kwargs
    ) -> Optional[Dict[str, Any]]:
        params = {"limit": min(limit, 100), "skip": skip, "sort": sort, **kwargs}
        if not fetch_all:
            return self._make_authenticated_request("listings", params=params)
        aggregated_results = []
        total_count = None
        page_limit = params["limit"]
        current_skip = params["skip"]
        saw_successful_page = False
        while True:
            params["skip"] = current_skip
            page = self._make_authenticated_request("listings", params=params)
            if not page:
                if not saw_successful_page:
                    return None
                break
            saw_successful_page = True
            page_results = page.get("results", [])
            if total_count is None:
                total_count = page.get("count")
            aggregated_results.extend(page_results)
            if len(page_results) < page_limit or (total_count is not None and current_skip + page_limit >= total_count):
                break
            current_skip += page_limit
        return {
            "results": aggregated_results,
            "count": total_count if total_count is not None else len(aggregated_results),
        }

    def search_reviews(
        self,
        limit: int = 25,
        skip: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        fetch_all: bool = False,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        params = {"limit": min(limit, 100), "skip": skip, **kwargs}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if not fetch_all:
            response = self._make_authenticated_request("reviews", params=params)
            if response and "data" in response:
                return {"results": response["data"], "count": len(response["data"])}
            return response
        aggregated_results = []
        page_limit = params["limit"]
        current_skip = params["skip"]
        saw_successful_page = False
        while True:
            params["skip"] = current_skip
            page = self._make_authenticated_request("reviews", params=params)
            if not page:
                if not saw_successful_page:
                    return None
                break
            saw_successful_page = True
            page_results = page.get("data", [])
            aggregated_results.extend(page_results)
            if len(page_results) < page_limit:
                break
            current_skip += page_limit
        return {"results": aggregated_results, "count": len(aggregated_results)}

    def search_owners(
        self, limit: int = 25, skip: int = 0, fetch_all: bool = False, **kwargs
    ) -> Optional[Dict[str, Any]]:
        params = {"limit": min(limit, 100), "skip": skip, **kwargs}
        if not fetch_all:
            return self._make_authenticated_request("owners", params=params)
        aggregated_results = []
        seen_owner_ids = set()
        page_limit = params["limit"]
        current_skip = params["skip"]
        while True:
            params["skip"] = current_skip
            page = self._make_authenticated_request("owners", params=params)
            if not page:
                break
            page_results = page if isinstance(page, list) else page.get("results", [])
            for owner in page_results:
                oid = owner.get("_id")
                if oid and oid not in seen_owner_ids:
                    seen_owner_ids.add(oid)
                    aggregated_results.append(owner)
            if not page_results or len(page_results) < page_limit:
                break
            current_skip += page_limit
        if not aggregated_results:
            return None
        return {"results": aggregated_results, "count": len(aggregated_results)}

    def search_guests(
        self, limit: int = 25, skip: int = 0, fetch_all: bool = False, **kwargs
    ) -> Optional[Dict[str, Any]]:
        if "columns" not in kwargs:
            kwargs["columns"] = (
                "id firstName lastName fullName tags notes goodToKnowNotes "
                "guestEmail guestOtherEmails guestPhone guestOtherPhones guestHometown "
                "allergies interests dietaryPreferences preferredLanguage birthday gender "
                "pronouns maritalStatus kids passportNumber identityNumber nationality "
                "reservationMetadata lastCommunicationDate address marketingConsent marketingConsentDate "
                "returningGuest"
            )
        params = {"limit": min(limit, 100), "skip": skip, **kwargs}
        if not fetch_all:
            return self._make_authenticated_request("guests-crud", params=params)
        aggregated_results = []
        seen_guest_ids = set()
        page_limit = params["limit"]
        current_skip = params["skip"]
        total_count = None
        while True:
            params["skip"] = current_skip
            page = self._make_authenticated_request("guests-crud", params=params)
            if not page:
                break
            page_results = page.get("results", [])
            if isinstance(page_results, dict):
                page_results = list(page_results.values()) if page_results else []
            if total_count is None:
                total_count = page.get("total")
            for guest in page_results:
                gid = guest.get("_id")
                if gid and gid not in seen_guest_ids:
                    seen_guest_ids.add(gid)
                    aggregated_results.append(guest)
            if not page_results or len(page_results) < page_limit:
                break
            if total_count is not None and len(aggregated_results) >= total_count:
                break
            current_skip += page_limit
        if not aggregated_results:
            return None
        return {"results": aggregated_results, "count": len(aggregated_results)}

    def get_guests(self, **kwargs) -> Optional[Dict[str, Any]]:
        return self._make_authenticated_request("guests", params=kwargs)

    def get_guest(self, guest_id: str) -> Optional[Dict[str, Any]]:
        return self._make_authenticated_request(f"guests/{guest_id}")

    def get_webhooks(self, **kwargs) -> Optional[Dict[str, Any]]:
        return self._make_authenticated_request("webhooks", params=kwargs)

    def get_webhook(self, webhook_id: str) -> Optional[Dict[str, Any]]:
        return self._make_authenticated_request(f"webhooks/{webhook_id}")

    def create_webhook(self, events: List[WebhookEvent], url: str) -> Optional[Dict[str, Any]]:
        data = {"events": [e.value for e in events], "url": url}
        return self._make_authenticated_request("webhooks", method="POST", data=data)

    def update_webhook(self, webhook_id: str, events: List[WebhookEvent], url: str) -> Optional[Dict[str, Any]]:
        data = {"events": [e.value for e in events], "url": url}
        return self._make_authenticated_request(f"webhooks/{webhook_id}", method="PUT", data=data)

    def delete_webhook(self, webhook_id: str) -> Optional[Dict[str, Any]]:
        return self._make_authenticated_request(f"webhooks/{webhook_id}", method="DELETE")

    def get_webhook_secret(self) -> Optional[Dict[str, Any]]:
        return self._make_authenticated_request("webhooks-v2/secret")

    def update_reservation_custom_fields(
        self, reservation_id: str, custom_fields: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        data = {"customFields": custom_fields}
        return self._make_authenticated_request(f"reservations/{reservation_id}/custom-fields", method="PUT", data=data)

    def update_listing_custom_fields(
        self, listing_id: str, custom_fields: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        data = {"customFields": custom_fields}
        return self._make_authenticated_request(f"listings/{listing_id}/custom-fields", method="PUT", data=data)

    def get_property_logs(self, listing_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        return self._make_authenticated_request(f"property-logs/{listing_id}", params=kwargs or None)

    def get_listing_calendar(
        self,
        listing_id: str,
        start_date: str,
        end_date: str,
        include_allotment: bool = True,
        ignore_inactive_child_allotment: bool = False,
        ignore_unlisted_child_allotment: bool = False,
    ) -> Optional[Dict[str, Any]]:
        params = {
            "startDate": start_date,
            "endDate": end_date,
            "includeAllotment": str(include_allotment).lower(),
        }
        if ignore_inactive_child_allotment:
            params["ignoreInactiveChildAllotment"] = "true"
        if ignore_unlisted_child_allotment:
            params["ignoreUnlistedChildAllotment"] = "true"
        return self._make_authenticated_request(
            f"availability-pricing/api/calendar/listings/{listing_id}", params=params
        )
