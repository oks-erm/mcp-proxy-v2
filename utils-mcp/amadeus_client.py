"""Small Amadeus REST client for airport lookup, flight search, and demand insights."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_BASE_URL = "https://test.api.amadeus.com"
DEFAULT_TIMEOUT_SECONDS = 20


class AmadeusClientError(RuntimeError):
    """Raised when the Amadeus API returns an error."""


@dataclass
class _TokenState:
    access_token: str | None = None
    expires_at_monotonic: float = 0.0


class AmadeusClient:
    """Minimal OAuth2-enabled Amadeus client."""

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._token = _TokenState()
        self._token_lock = threading.Lock()

    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _require_configured(self) -> None:
        if not self.configured():
            raise AmadeusClientError(
                "Amadeus credentials are not configured. Set AMADEUS_API_KEY and AMADEUS_API_SECRET."
            )

    def _ensure_access_token(self) -> str:
        self._require_configured()
        now = time.monotonic()
        if self._token.access_token and now < self._token.expires_at_monotonic:
            return self._token.access_token
        with self._token_lock:
            now = time.monotonic()
            if self._token.access_token and now < self._token.expires_at_monotonic:
                return self._token.access_token
            response = requests.post(
                f"{self._base_url}/v1/security/oauth2/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=self._timeout_seconds,
            )
            payload = _parse_json_or_raise(response)
            if response.status_code >= 400:
                raise AmadeusClientError(_format_error("Amadeus token request failed", payload))
            access_token = str(payload.get("access_token") or "").strip()
            expires_in = int(payload.get("expires_in") or 0)
            if not access_token:
                raise AmadeusClientError("Amadeus token response did not include access_token.")
            # Refresh a bit early to avoid edge expirations.
            self._token = _TokenState(
                access_token=access_token,
                expires_at_monotonic=time.monotonic() + max(expires_in - 60, 60),
            )
            return access_token

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._ensure_access_token()
        response = requests.get(
            f"{self._base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params={k: v for k, v in (params or {}).items() if v is not None},
            timeout=self._timeout_seconds,
        )
        payload = _parse_json_or_raise(response)
        if response.status_code >= 400:
            raise AmadeusClientError(_format_error("Amadeus request failed", payload))
        return payload

    def search_locations(
        self,
        *,
        keyword: str,
        country_code: str | None = None,
        sub_type: str = "ANY",
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._get(
            "/v1/reference-data/locations",
            params={
                "keyword": keyword,
                "subType": sub_type,
                "page[limit]": limit,
                "page[offset]": offset,
                "countryCode": country_code,
            },
        )

    def search_flight_offers(
        self,
        *,
        origin_location_code: str,
        destination_location_code: str,
        departure_date: str,
        adults: int,
        return_date: str | None = None,
        children: int | None = None,
        infants: int | None = None,
        travel_class: str | None = None,
        non_stop: bool | None = None,
        currency_code: str | None = None,
        max_results: int = 10,
    ) -> dict[str, Any]:
        return self._get(
            "/v2/shopping/flight-offers",
            params={
                "originLocationCode": origin_location_code,
                "destinationLocationCode": destination_location_code,
                "departureDate": departure_date,
                "returnDate": return_date,
                "adults": adults,
                "children": children,
                "infants": infants,
                "travelClass": travel_class,
                "nonStop": _bool_query(non_stop),
                "currencyCode": currency_code,
                "max": max_results,
            },
        )

    def search_flight_dates(
        self,
        *,
        origin: str,
        destination: str,
        departure_date: str | None = None,
        one_way: bool | None = None,
        duration: int | None = None,
        non_stop: bool | None = None,
        max_price: int | float | None = None,
        currency_code: str | None = None,
        view_by: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/v1/shopping/flight-dates",
            params={
                "origin": origin,
                "destination": destination,
                "departureDate": departure_date,
                "oneWay": _bool_query(one_way),
                "duration": duration,
                "nonStop": _bool_query(non_stop),
                "maxPrice": max_price,
                "currencyCode": currency_code,
                "viewBy": view_by,
            },
        )

    def get_market_demand(
        self,
        *,
        insight_type: str,
        period: str,
        origin_city_code: str | None = None,
        city_code: str | None = None,
        limit: int = 10,
        offset: int = 0,
        sort: str | None = None,
        direction: str | None = None,
    ) -> dict[str, Any]:
        if insight_type == "most_booked":
            path = "/v1/travel/analytics/air-traffic/booked"
            params = {
                "originCityCode": origin_city_code,
                "period": period,
                "page[limit]": limit,
                "page[offset]": offset,
                "sort": sort,
            }
        elif insight_type == "most_traveled":
            path = "/v1/travel/analytics/air-traffic/traveled"
            params = {
                "originCityCode": origin_city_code,
                "period": period,
                "page[limit]": limit,
                "page[offset]": offset,
                "sort": sort,
            }
        elif insight_type == "busiest_period":
            path = "/v1/travel/analytics/air-traffic/busiest-period"
            params = {
                "cityCode": city_code,
                "period": period,
                "direction": direction,
            }
        else:
            raise AmadeusClientError(f"Unsupported insight_type: {insight_type}")
        return self._get(path, params=params)


def _bool_query(value: bool | None) -> str | None:
    if value is None:
        return None
    return "true" if value else "false"


def _parse_json_or_raise(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AmadeusClientError(f"Amadeus returned non-JSON response (status {response.status_code}).") from exc
    if not isinstance(payload, dict):
        raise AmadeusClientError(f"Amadeus returned unexpected response type: {type(payload).__name__}")
    return payload


def _format_error(prefix: str, payload: dict[str, Any]) -> str:
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            detail = first.get("detail") or first.get("title") or first.get("code")
            if detail:
                return f"{prefix}: {detail}"
    detail = payload.get("error_description") or payload.get("detail") or payload.get("error")
    if detail:
        return f"{prefix}: {detail}"
    return prefix


_client: AmadeusClient | None = None


def configure_api(
    client_id: str | None,
    client_secret: str | None,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    global _client
    _client = AmadeusClient(
        client_id,
        client_secret,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def get_client() -> AmadeusClient:
    if _client is None:
        raise AmadeusClientError("Amadeus client is not initialised.")
    return _client
