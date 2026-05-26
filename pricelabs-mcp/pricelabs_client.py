"""PriceLabs Customer API client for the MCP server."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.pricelabs.co"
DEFAULT_TIMEOUT_SECONDS = 300.0


class PriceLabsAPIError(RuntimeError):
    """Structured exception for PriceLabs HTTP and transport failures."""

    def __init__(
        self,
        code: str,
        details: str,
        *,
        status_code: Optional[int] = None,
        retryable: bool = False,
        cause: str = "upstream_error",
        payload: Any = None,
    ) -> None:
        super().__init__(details)
        self.code = code
        self.details = details
        self.status_code = status_code
        self.retryable = retryable
        self.cause = cause
        self.payload = payload


def _error_details(status_code: int, body: Any) -> str:
    if isinstance(body, dict):
        message = body.get("error") or body.get("message") or body.get("detail")
        if message:
            return str(message)
    if isinstance(body, str) and body.strip():
        return body.strip()[:1000]
    return f"PriceLabs API returned HTTP {status_code}"


class PriceLabsClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ValueError("PriceLabs API key is required")
        self.api_key = key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_seconds = float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
        self._session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        return {"Accept": "application/json", "X-API-Key": self.api_key}

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self._session.get(
                url,
                headers=self._headers(),
                params=params or {},
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise PriceLabsAPIError(
                "timeout",
                str(exc),
                retryable=True,
                cause="timeout",
            ) from exc
        except requests.RequestException as exc:
            raise PriceLabsAPIError(
                "upstream_failed",
                str(exc),
                retryable=True,
                cause="upstream_error",
            ) from exc

        try:
            body: Any = response.json()
        except ValueError:
            body = response.text

        if response.ok:
            if not isinstance(body, dict):
                raise PriceLabsAPIError(
                    "upstream_failed",
                    f"Unexpected PriceLabs response shape: {type(body).__name__}",
                    status_code=response.status_code,
                    retryable=True,
                    cause="upstream_error",
                    payload=body,
                )
            return body

        status = response.status_code
        details = _error_details(status, body)
        if status == 400:
            raise PriceLabsAPIError(
                "validation_error",
                details,
                status_code=status,
                retryable=False,
                cause="validation",
                payload=body,
            )
        if status == 404:
            raise PriceLabsAPIError(
                "not_found",
                details,
                status_code=status,
                retryable=False,
                cause="not_found",
                payload=body,
            )
        if status == 429:
            raise PriceLabsAPIError(
                "rate_limited",
                details,
                status_code=status,
                retryable=True,
                cause="upstream_error",
                payload=body,
            )
        if status in (401, 403):
            raise PriceLabsAPIError(
                "permission_denied",
                details,
                status_code=status,
                retryable=False,
                cause="permission",
                payload=body,
            )
        if status >= 500:
            raise PriceLabsAPIError(
                "upstream_failed",
                details,
                status_code=status,
                retryable=True,
                cause="upstream_error",
                payload=body,
            )
        raise PriceLabsAPIError(
            "upstream_failed",
            details,
            status_code=status,
            retryable=True,
            cause="upstream_error",
            payload=body,
        )

    def get_neighborhood_data(self, *, pms: str, listing_id: str) -> Dict[str, Any]:
        return self.get("v1/neighborhood_data", params={"pms": pms, "listing_id": listing_id})

    def get_date_specific_overrides(self, *, listing_id: str, pms: str) -> Dict[str, Any]:
        safe_listing_id = quote(str(listing_id), safe="")
        return self.get(f"v1/listings/{safe_listing_id}/overrides", params={"pms": pms})


_client: Optional[PriceLabsClient] = None


def configure_client(
    api_key: Optional[str],
    *,
    base_url: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> None:
    """Replace global client. Passing an empty key disables outbound calls."""
    global _client
    if not api_key or not api_key.strip():
        _client = None
        return
    _client = PriceLabsClient(
        api_key=api_key,
        base_url=base_url or os.getenv("PRICELABS_API_BASE", DEFAULT_BASE_URL),
        timeout_seconds=timeout_seconds or float(os.getenv("PRICELABS_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
    )


def get_client() -> PriceLabsClient:
    if _client is None:
        raise RuntimeError(
            "PriceLabs client is not configured; set PRICELABS_API_KEY locally or configure pricelabs-api-key secret"
        )
    return _client


def get_neighborhood_data(*, pms: str, listing_id: str) -> Dict[str, Any]:
    """Module-level function for MCP tools and tests."""
    return get_client().get_neighborhood_data(pms=pms, listing_id=listing_id)


def get_date_specific_overrides(*, listing_id: str, pms: str) -> Dict[str, Any]:
    """Module-level function for MCP tools and tests."""
    return get_client().get_date_specific_overrides(listing_id=listing_id, pms=pms)
