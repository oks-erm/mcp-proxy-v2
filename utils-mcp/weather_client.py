"""Helpers for Open-Meteo geocoding and forecast APIs."""

from __future__ import annotations

from typing import Any

import requests

GEOCODING_BASE_URL = "https://geocoding-api.open-meteo.com/v1"
FORECAST_BASE_URL = "https://api.open-meteo.com/v1"
DEFAULT_TIMEOUT_SECONDS = 20


class WeatherClientError(RuntimeError):
    """Raised when Open-Meteo responds with an error."""


def geocode(
    *,
    name: str,
    country_code: str | None = None,
    count: int = 10,
    language: str = "en",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    response = requests.get(
        f"{GEOCODING_BASE_URL}/search",
        params={
            "name": name,
            "count": count,
            "language": language,
            "format": "json",
            "countryCode": country_code,
        },
        timeout=timeout_seconds,
    )
    return _parse_json_response(response, "Open-Meteo geocoding")


def forecast(
    *,
    latitude: float,
    longitude: float,
    forecast_days: int,
    include_hourly: bool,
    timezone: str = "auto",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone,
        "forecast_days": forecast_days,
        "current": ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
                "wind_direction_10m",
                "is_day",
            ]
        ),
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "wind_speed_10m_max",
                "sunrise",
                "sunset",
            ]
        ),
    }
    if include_hourly:
        params["hourly"] = ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "precipitation_probability",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
            ]
        )
    response = requests.get(
        f"{FORECAST_BASE_URL}/forecast",
        params=params,
        timeout=timeout_seconds,
    )
    return _parse_json_response(response, "Open-Meteo forecast")


def _parse_json_response(response: requests.Response, service_name: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise WeatherClientError(f"{service_name} returned non-JSON response (status {response.status_code}).") from exc
    if not isinstance(payload, dict):
        raise WeatherClientError(f"{service_name} returned unexpected response type: {type(payload).__name__}")
    if response.status_code >= 400:
        reason = payload.get("reason") or payload.get("error") or "request failed"
        raise WeatherClientError(f"{service_name} request failed: {reason}")
    return payload
