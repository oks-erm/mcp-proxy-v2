"""Small Ticketmaster Discovery API client."""

from __future__ import annotations

from typing import Any

import requests

DEFAULT_BASE_URL = "https://app.ticketmaster.com/discovery/v2"
DEFAULT_TIMEOUT_SECONDS = 20


class TicketmasterClientError(RuntimeError):
    """Raised when the Ticketmaster API returns an error."""


class TicketmasterClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def configured(self) -> bool:
        return bool(self._api_key)

    def _require_configured(self) -> None:
        if not self.configured():
            raise TicketmasterClientError("Ticketmaster API key is not configured. Set TICKETMASTER_API_KEY.")

    def search_events(self, *, params: dict[str, Any]) -> dict[str, Any]:
        self._require_configured()
        response = requests.get(
            f"{self._base_url}/events.json",
            params={"apikey": self._api_key, **{k: v for k, v in params.items() if v is not None}},
            timeout=self._timeout_seconds,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TicketmasterClientError(
                f"Ticketmaster returned non-JSON response (status {response.status_code})."
            ) from exc
        if not isinstance(payload, dict):
            raise TicketmasterClientError(f"Ticketmaster returned unexpected response type: {type(payload).__name__}")
        if response.status_code >= 400:
            fault = payload.get("fault")
            if isinstance(fault, dict):
                detail = fault.get("faultstring") or fault.get("detail")
                if detail:
                    raise TicketmasterClientError(f"Ticketmaster request failed: {detail}")
            raise TicketmasterClientError("Ticketmaster request failed.")
        return payload


_client: TicketmasterClient | None = None


def configure_api(
    api_key: str | None,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    global _client
    _client = TicketmasterClient(api_key, base_url=base_url, timeout_seconds=timeout_seconds)


def get_client() -> TicketmasterClient:
    if _client is None:
        raise TicketmasterClientError("Ticketmaster client is not initialised.")
    return _client
