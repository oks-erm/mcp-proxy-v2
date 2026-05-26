"""MCP tools for travel and local utility lookups: weather, events, flights, and demand."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import weather_client
from amadeus_client import AmadeusClientError
from amadeus_client import get_client as get_amadeus_client
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from mcp_platform.coerce_numeric import coerce_int
from mcp_platform.detail_level import parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import build_pagination_meta, with_response_meta
from mcp_platform.transport import structured_result
from ticketmaster_client import TicketmasterClientError
from ticketmaster_client import get_client as get_ticketmaster_client

logger = logging.getLogger(__name__)

_TRAVEL_SUB_TYPES = {"AIRPORT", "CITY", "ANY"}
_TRAVEL_CLASSES = {"ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"}
_VIEW_BY = {"DATE", "DURATION", "WEEK"}
_DEMAND_TYPES = {"most_booked", "most_traveled", "busiest_period"}
_DEMAND_SORT = {"travelers": "analytics.travelers.score", "flights": "analytics.flights.score"}
_DEMAND_DIRECTION = {"ARRIVING", "DEPARTING"}

mcp = FastMCP(
    "utils",
    instructions=(
        "Travel and local utility tools. "
        "Use find_travel_location to resolve city/airport names to IATA codes before flight tools. "
        "search_flights returns live-ish Amadeus offers for a specific route/date. "
        "search_flight_date_deals returns cached cheap-date ideas for a route. "
        "get_flight_market_demand exposes Amadeus booking / traveler demand insights. "
        "search_events uses geocoded place search plus Ticketmaster Discovery. "
        "get_weather_forecast uses Open-Meteo geocoding + forecast APIs."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


def _validation_error(details: str) -> CallToolResult:
    return structured_result(
        tool_error(
            "validation_error",
            details=details,
            cause="validation",
            retryable=False,
        )
    )


def _upstream_error(details: str, *, suggested_fix: str) -> CallToolResult:
    return structured_result(
        tool_error(
            "upstream_error",
            details=details,
            cause="upstream_error",
            retryable=True,
            suggested_fix=suggested_fix,
        )
    )


def _normalize_text(value: str) -> str:
    return " ".join((value or "").strip().split())


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _iso_start_of_day(date_value: str | None) -> str | None:
    parsed = _parse_date(date_value)
    return f"{parsed}T00:00:00Z" if parsed else None


def _iso_end_of_day(date_value: str | None) -> str | None:
    parsed = _parse_date(date_value)
    return f"{parsed}T23:59:59Z" if parsed else None


def _first_location_for_query(location_query: str, country_code: str | None = None) -> dict[str, Any]:
    payload = weather_client.geocode(name=location_query, country_code=country_code, count=1)
    rows = payload.get("results")
    if not isinstance(rows, list) or not rows:
        raise weather_client.WeatherClientError(f"No location found for '{location_query}'.")
    row = rows[0]
    if not isinstance(row, dict):
        raise weather_client.WeatherClientError(f"Unexpected geocoding response for '{location_query}'.")
    return row


def _compact_location_row(row: dict[str, Any]) -> dict[str, Any]:
    address = row.get("address") or {}
    geo = row.get("geoCode") or {}
    return {
        "name": row.get("name"),
        "iata_code": row.get("iataCode"),
        "sub_type": row.get("subType"),
        "country_code": address.get("countryCode"),
        "state_code": address.get("stateCode"),
        "latitude": geo.get("latitude"),
        "longitude": geo.get("longitude"),
    }


def _compact_ticketmaster_event(row: dict[str, Any], *, detail_level: str) -> dict[str, Any]:
    dates = row.get("dates") or {}
    start = dates.get("start") or {}
    venue = {}
    embedded = row.get("_embedded")
    if isinstance(embedded, dict):
        venues = embedded.get("venues")
        if isinstance(venues, list) and venues:
            first = venues[0]
            if isinstance(first, dict):
                venue = first
    compact = {
        "id": row.get("id"),
        "name": row.get("name"),
        "url": row.get("url"),
        "status": (dates.get("status") or {}).get("code"),
        "start_date": start.get("localDate"),
        "start_time": start.get("localTime"),
        "timezone": dates.get("timezone"),
        "city": (venue.get("city") or {}).get("name"),
        "country_code": (venue.get("country") or {}).get("countryCode"),
        "venue_name": venue.get("name"),
        "classification": _primary_classification_name(row),
        "price_ranges": [
            {
                "type": price.get("type"),
                "currency": price.get("currency"),
                "min": price.get("min"),
                "max": price.get("max"),
            }
            for price in (row.get("priceRanges") or [])
            if isinstance(price, dict)
        ],
    }
    if detail_level in {"summary", "full"}:
        compact["images"] = [
            {
                "url": image.get("url"),
                "ratio": image.get("ratio"),
                "width": image.get("width"),
                "height": image.get("height"),
            }
            for image in (row.get("images") or [])
            if isinstance(image, dict)
        ][:5]
        compact["info"] = row.get("info")
        compact["please_note"] = row.get("pleaseNote")
    if detail_level == "full":
        compact["sales"] = row.get("sales")
        compact["raw_classifications"] = row.get("classifications")
        compact["venue"] = venue
    return compact


def _primary_classification_name(row: dict[str, Any]) -> str | None:
    for item in row.get("classifications") or []:
        if not isinstance(item, dict):
            continue
        if item.get("primary"):
            genre = item.get("genre") or {}
            segment = item.get("segment") or {}
            sub_genre = item.get("subGenre") or {}
            return " / ".join([x for x in [segment.get("name"), genre.get("name"), sub_genre.get("name")] if x]) or None
    return None


def _compact_flight_offer(offer: dict[str, Any], *, detail_level: str) -> dict[str, Any]:
    itineraries = offer.get("itineraries") or []
    traveler_pricings = offer.get("travelerPricings") or []
    compact = {
        "id": offer.get("id"),
        "one_way": offer.get("oneWay"),
        "instant_ticketing_required": offer.get("instantTicketingRequired"),
        "last_ticketing_date": offer.get("lastTicketingDate"),
        "number_of_bookable_seats": offer.get("numberOfBookableSeats"),
        "price": {
            "currency": (offer.get("price") or {}).get("currency"),
            "base": (offer.get("price") or {}).get("base"),
            "total": (offer.get("price") or {}).get("total"),
            "grand_total": (offer.get("price") or {}).get("grandTotal"),
        },
        "itineraries": [
            {
                "duration": itinerary.get("duration"),
                "segments": [
                    {
                        "carrier_code": segment.get("carrierCode"),
                        "number": segment.get("number"),
                        "aircraft_code": (segment.get("aircraft") or {}).get("code"),
                        "duration": segment.get("duration"),
                        "number_of_stops": segment.get("numberOfStops"),
                        "departure": {
                            "iata_code": (segment.get("departure") or {}).get("iataCode"),
                            "terminal": (segment.get("departure") or {}).get("terminal"),
                            "at": (segment.get("departure") or {}).get("at"),
                        },
                        "arrival": {
                            "iata_code": (segment.get("arrival") or {}).get("iataCode"),
                            "terminal": (segment.get("arrival") or {}).get("terminal"),
                            "at": (segment.get("arrival") or {}).get("at"),
                        },
                    }
                    for segment in itinerary.get("segments") or []
                    if isinstance(segment, dict)
                ],
            }
            for itinerary in itineraries
            if isinstance(itinerary, dict)
        ],
    }
    if detail_level in {"summary", "full"}:
        compact["traveler_pricing_summary"] = [
            {
                "traveler_id": traveler.get("travelerId"),
                "traveler_type": traveler.get("travelerType"),
                "fare_option": traveler.get("fareOption"),
                "price": traveler.get("price"),
            }
            for traveler in traveler_pricings
            if isinstance(traveler, dict)
        ]
    if detail_level == "full":
        compact["validating_airline_codes"] = offer.get("validatingAirlineCodes")
        compact["pricing_options"] = offer.get("pricingOptions")
        compact["traveler_pricings"] = traveler_pricings
    return compact


def _compact_flight_date(row: dict[str, Any]) -> dict[str, Any]:
    price = row.get("price") or {}
    links = row.get("links") or {}
    return {
        "type": row.get("type"),
        "origin": row.get("origin"),
        "destination": row.get("destination"),
        "departure_date": row.get("departureDate"),
        "return_date": row.get("returnDate"),
        "price": {
            "total": price.get("total"),
        },
        "links": {
            "flight_offers": links.get("flightOffers"),
            "flight_destinations": links.get("flightDestinations"),
        },
    }


def _compact_market_demand(row: dict[str, Any]) -> dict[str, Any]:
    analytics = row.get("analytics") or {}
    return {
        "type": row.get("type"),
        "sub_type": row.get("subType"),
        "destination": row.get("destination"),
        "period": row.get("period"),
        "analytics": {
            "travelers_score": ((analytics.get("travelers") or {}).get("score")),
            "flights_score": ((analytics.get("flights") or {}).get("score")),
        },
    }


@mcp.tool(structured_output=False)
def find_travel_location(
    query: str,
    country_code: str | None = None,
    sub_type: str = "ANY",
    limit: int = 10,
    offset: int = 0,
    detail_level: str = "compact",
) -> CallToolResult:
    """Resolve city or airport names to Amadeus travel locations and IATA codes."""
    raw_query = _normalize_text(query)
    if not raw_query:
        return _validation_error("query is required")
    raw_sub_type = (sub_type or "ANY").strip().upper()
    if raw_sub_type not in _TRAVEL_SUB_TYPES:
        return _validation_error(f"sub_type must be one of {sorted(_TRAVEL_SUB_TYPES)}")
    limit = coerce_int(limit, default=10, minimum=1, maximum=50)
    offset = coerce_int(offset, default=0, minimum=0, maximum=500)
    dl = parse_detail_level(detail_level, default="compact")
    try:
        payload = get_amadeus_client().search_locations(
            keyword=raw_query,
            country_code=(country_code or "").strip().upper() or None,
            sub_type=raw_sub_type,
            limit=limit,
            offset=offset,
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            rows = []
        data = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            compact = _compact_location_row(row)
            if dl == "full":
                compact["raw"] = row
            data.append(compact)
        meta = payload.get("meta") or {}
        links = meta.get("links") or {}
        pagination = build_pagination_meta(
            limit=limit,
            offset=offset,
            has_more=bool(links.get("next")),
            next_offset=(offset + limit) if links.get("next") else None,
        )
        return structured_result(
            with_response_meta(
                {
                    "count": len(data),
                    "data": data,
                    "detail_level": dl,
                },
                tool="utils_find_travel_location",
                pagination=pagination if pagination else None,
            )
        )
    except AmadeusClientError as exc:
        logger.exception("find_travel_location failed")
        return _upstream_error(str(exc), suggested_fix="Retry later or verify the Amadeus API credentials.")


@mcp.tool(structured_output=False)
def search_flights(
    origin_location_code: str,
    destination_location_code: str,
    departure_date: str,
    adults: int = 1,
    return_date: str | None = None,
    children: int = 0,
    infants: int = 0,
    travel_class: str | None = None,
    non_stop: bool | None = None,
    currency_code: str | None = "EUR",
    max_results: int = 10,
    detail_level: str = "compact",
) -> CallToolResult:
    """Search Amadeus flight offers for a specific route and date."""
    origin = _normalize_text(origin_location_code).upper()
    destination = _normalize_text(destination_location_code).upper()
    dep = _parse_date(departure_date)
    ret = _parse_date(return_date)
    if not origin or not destination:
        return _validation_error("origin_location_code and destination_location_code are required")
    if dep is None:
        return _validation_error("departure_date must be YYYY-MM-DD")
    if return_date and ret is None:
        return _validation_error("return_date must be YYYY-MM-DD when provided")
    adults = coerce_int(adults, default=1, minimum=1, maximum=9)
    children = coerce_int(children, default=0, minimum=0, maximum=9)
    infants = coerce_int(infants, default=0, minimum=0, maximum=9)
    max_results = coerce_int(max_results, default=10, minimum=1, maximum=20)
    raw_travel_class = (travel_class or "").strip().upper() or None
    if raw_travel_class and raw_travel_class not in _TRAVEL_CLASSES:
        return _validation_error(f"travel_class must be one of {sorted(_TRAVEL_CLASSES)}")
    dl = parse_detail_level(detail_level, default="compact")
    try:
        payload = get_amadeus_client().search_flight_offers(
            origin_location_code=origin,
            destination_location_code=destination,
            departure_date=dep,
            return_date=ret,
            adults=adults,
            children=children or None,
            infants=infants or None,
            travel_class=raw_travel_class,
            non_stop=non_stop,
            currency_code=(currency_code or "").strip().upper() or None,
            max_results=max_results,
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            rows = []
        data = [_compact_flight_offer(row, detail_level=dl) for row in rows if isinstance(row, dict)]
        dictionaries = payload.get("dictionaries") if dl in {"summary", "full"} else None
        return structured_result(
            with_response_meta(
                {
                    "results": data,
                    "search_criteria": {
                        "origin_location_code": origin,
                        "destination_location_code": destination,
                        "departure_date": dep,
                        "return_date": ret,
                        "adults": adults,
                        "children": children,
                        "infants": infants,
                        "travel_class": raw_travel_class,
                        "non_stop": non_stop,
                        "currency_code": (currency_code or "").strip().upper() or None,
                    },
                    "detail_level": dl,
                    **({"dictionaries": dictionaries} if isinstance(dictionaries, dict) else {}),
                },
                tool="utils_search_flights",
                data_from="results",
            )
        )
    except AmadeusClientError as exc:
        logger.exception("search_flights failed")
        return _upstream_error(str(exc), suggested_fix="Retry later or verify the Amadeus API credentials.")


@mcp.tool(structured_output=False)
def search_flight_date_deals(
    origin: str,
    destination: str,
    departure_date: str | None = None,
    one_way: bool | None = None,
    duration: int | None = None,
    non_stop: bool | None = None,
    max_price: int | float | None = None,
    currency_code: str | None = "EUR",
    view_by: str | None = "DATE",
    limit: int = 10,
) -> CallToolResult:
    """Search cached cheapest-date flight ideas for a route."""
    raw_origin = _normalize_text(origin).upper()
    raw_destination = _normalize_text(destination).upper()
    dep = _parse_date(departure_date)
    if not raw_origin or not raw_destination:
        return _validation_error("origin and destination are required")
    if departure_date and dep is None:
        return _validation_error("departure_date must be YYYY-MM-DD when provided")
    raw_view_by = (view_by or "DATE").strip().upper()
    if raw_view_by not in _VIEW_BY:
        return _validation_error(f"view_by must be one of {sorted(_VIEW_BY)}")
    limit = coerce_int(limit, default=10, minimum=1, maximum=50)
    if duration is not None:
        duration = coerce_int(duration, default=1, minimum=1, maximum=30)
    try:
        payload = get_amadeus_client().search_flight_dates(
            origin=raw_origin,
            destination=raw_destination,
            departure_date=dep,
            one_way=one_way,
            duration=duration,
            non_stop=non_stop,
            max_price=max_price,
            currency_code=(currency_code or "").strip().upper() or None,
            view_by=raw_view_by,
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            rows = []
        data = [_compact_flight_date(row) for row in rows[:limit] if isinstance(row, dict)]
        return structured_result(
            with_response_meta(
                {
                    "results": data,
                    "detail_level": "compact",
                    "search_criteria": {
                        "origin": raw_origin,
                        "destination": raw_destination,
                        "departure_date": dep,
                        "one_way": one_way,
                        "duration": duration,
                        "non_stop": non_stop,
                        "max_price": max_price,
                        "currency_code": (currency_code or "").strip().upper() or None,
                        "view_by": raw_view_by,
                    },
                },
                tool="utils_search_flight_date_deals",
                data_from="results",
            )
        )
    except AmadeusClientError as exc:
        logger.exception("search_flight_date_deals failed")
        return _upstream_error(str(exc), suggested_fix="Retry later or verify the Amadeus API credentials.")


@mcp.tool(structured_output=False)
def get_flight_market_demand(
    insight_type: str,
    period: str,
    origin_city_code: str | None = None,
    city_code: str | None = None,
    sort_by: str = "travelers",
    direction: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> CallToolResult:
    """Get Amadeus route demand insights: booked, traveled, or busiest period."""
    raw_insight = (insight_type or "").strip().lower()
    if raw_insight not in _DEMAND_TYPES:
        return _validation_error(f"insight_type must be one of {sorted(_DEMAND_TYPES)}")
    if raw_insight == "busiest_period":
        if not _normalize_text(city_code or ""):
            return _validation_error("city_code is required when insight_type is busiest_period")
        if not _parse_year(period):
            return _validation_error("period must be YYYY for busiest_period")
    else:
        if not _normalize_text(origin_city_code or ""):
            return _validation_error("origin_city_code is required for most_booked and most_traveled")
        if not _parse_month(period):
            return _validation_error("period must be YYYY-MM for most_booked and most_traveled")
    raw_sort = (sort_by or "travelers").strip().lower()
    if raw_sort not in _DEMAND_SORT:
        return _validation_error(f"sort_by must be one of {sorted(_DEMAND_SORT)}")
    raw_direction = (direction or "").strip().upper() or None
    if raw_direction and raw_direction not in _DEMAND_DIRECTION:
        return _validation_error(f"direction must be one of {sorted(_DEMAND_DIRECTION)}")
    limit = coerce_int(limit, default=10, minimum=1, maximum=50)
    offset = coerce_int(offset, default=0, minimum=0, maximum=500)
    try:
        payload = get_amadeus_client().get_market_demand(
            insight_type=raw_insight,
            period=period.strip(),
            origin_city_code=_normalize_text(origin_city_code or "").upper() or None,
            city_code=_normalize_text(city_code or "").upper() or None,
            limit=limit,
            offset=offset,
            sort=_DEMAND_SORT[raw_sort] if raw_insight != "busiest_period" else None,
            direction=raw_direction,
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            rows = []
        data = [_compact_market_demand(row) for row in rows if isinstance(row, dict)]
        meta = payload.get("meta") or {}
        links = meta.get("links") or {}
        pagination = build_pagination_meta(
            limit=limit if raw_insight != "busiest_period" else None,
            offset=offset if raw_insight != "busiest_period" else None,
            has_more=bool(links.get("next")) if raw_insight != "busiest_period" else None,
            next_offset=(offset + limit) if (raw_insight != "busiest_period" and links.get("next")) else None,
            total_count=meta.get("count") if isinstance(meta.get("count"), int) else None,
        )
        return structured_result(
            with_response_meta(
                {
                    "results": data,
                    "insight_type": raw_insight,
                    "period": period.strip(),
                    "detail_level": "compact",
                },
                tool="utils_get_flight_market_demand",
                pagination=pagination if pagination else None,
                data_from="results",
            )
        )
    except AmadeusClientError as exc:
        logger.exception("get_flight_market_demand failed")
        return _upstream_error(str(exc), suggested_fix="Retry later or verify the Amadeus API credentials.")


def _parse_month(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m").strftime("%Y-%m")
    except ValueError:
        return None


def _parse_year(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y").strftime("%Y")
    except ValueError:
        return None


@mcp.tool(structured_output=False)
def search_events(
    location_query: str,
    keyword: str | None = None,
    country_code: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    classification_name: str | None = None,
    radius_km: int = 50,
    size: int = 20,
    page: int = 0,
    detail_level: str = "compact",
) -> CallToolResult:
    """Search Ticketmaster events around a city or area such as Porto, Portugal."""
    raw_location = _normalize_text(location_query)
    if not raw_location:
        return _validation_error("location_query is required")
    start_at = _iso_start_of_day(start_date)
    end_at = _iso_end_of_day(end_date)
    if start_date and start_at is None:
        return _validation_error("start_date must be YYYY-MM-DD")
    if end_date and end_at is None:
        return _validation_error("end_date must be YYYY-MM-DD")
    radius_km = coerce_int(radius_km, default=50, minimum=1, maximum=500)
    size = coerce_int(size, default=20, minimum=1, maximum=100)
    page = coerce_int(page, default=0, minimum=0, maximum=100)
    dl = parse_detail_level(detail_level, default="compact")
    try:
        location = _first_location_for_query(raw_location, country_code=(country_code or "").strip().upper() or None)
        payload = get_ticketmaster_client().search_events(
            params={
                "keyword": _normalize_text(keyword or "") or None,
                "latlong": f'{location.get("latitude")},{location.get("longitude")}',
                "radius": radius_km,
                "unit": "km",
                "countryCode": (country_code or "").strip().upper() or None,
                "classificationName": _normalize_text(classification_name or "") or None,
                "startDateTime": start_at,
                "endDateTime": end_at,
                "size": size,
                "page": page,
                "sort": "eventDate,date.asc",
            }
        )
        rows = ((payload.get("_embedded") or {}).get("events")) or []
        if not isinstance(rows, list):
            rows = []
        data = [_compact_ticketmaster_event(row, detail_level=dl) for row in rows if isinstance(row, dict)]
        page_meta = payload.get("page") or {}
        total_pages = page_meta.get("totalPages")
        current_page = page_meta.get("number")
        pagination = build_pagination_meta(
            limit=page_meta.get("size") if isinstance(page_meta.get("size"), int) else size,
            offset=page * size,
            has_more=bool(
                isinstance(total_pages, int) and isinstance(current_page, int) and current_page + 1 < total_pages
            ),
            next_offset=(
                ((page + 1) * size)
                if isinstance(total_pages, int) and isinstance(current_page, int) and current_page + 1 < total_pages
                else None
            ),
            total_count=page_meta.get("totalElements") if isinstance(page_meta.get("totalElements"), int) else None,
        )
        return structured_result(
            with_response_meta(
                {
                    "results": data,
                    "resolved_location": {
                        "name": location.get("name"),
                        "country_code": location.get("country_code"),
                        "latitude": location.get("latitude"),
                        "longitude": location.get("longitude"),
                    },
                    "detail_level": dl,
                },
                tool="utils_search_events",
                pagination=pagination if pagination else None,
                data_from="results",
            )
        )
    except (TicketmasterClientError, weather_client.WeatherClientError) as exc:
        logger.exception("search_events failed")
        return _upstream_error(
            str(exc),
            suggested_fix="Retry later or verify the Ticketmaster API key and event coverage for the location.",
        )


@mcp.tool(structured_output=False)
def get_weather_forecast(
    location_query: str,
    country_code: str | None = None,
    forecast_days: int = 5,
    include_hourly: bool = False,
    detail_level: str = "compact",
) -> CallToolResult:
    """Get current weather plus a short forecast for a place like Porto, Portugal."""
    raw_location = _normalize_text(location_query)
    if not raw_location:
        return _validation_error("location_query is required")
    forecast_days = coerce_int(forecast_days, default=5, minimum=1, maximum=16)
    dl = parse_detail_level(detail_level, default="compact")
    try:
        location = _first_location_for_query(raw_location, country_code=(country_code or "").strip().upper() or None)
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is None or longitude is None:
            raise weather_client.WeatherClientError(f"Geocoding for '{raw_location}' did not return coordinates.")
        payload = weather_client.forecast(
            latitude=float(latitude),
            longitude=float(longitude),
            forecast_days=forecast_days,
            include_hourly=include_hourly,
        )
        daily_block = payload.get("daily") or {}
        daily_rows = []
        if isinstance(daily_block, dict):
            times = daily_block.get("time") or []
            for idx, day in enumerate(times):
                daily_rows.append(
                    {
                        "date": day,
                        "weather_code": _index_or_none(daily_block.get("weather_code"), idx),
                        "temperature_max": _index_or_none(daily_block.get("temperature_2m_max"), idx),
                        "temperature_min": _index_or_none(daily_block.get("temperature_2m_min"), idx),
                        "precipitation_sum": _index_or_none(daily_block.get("precipitation_sum"), idx),
                        "precipitation_probability_max": _index_or_none(
                            daily_block.get("precipitation_probability_max"), idx
                        ),
                        "wind_speed_max": _index_or_none(daily_block.get("wind_speed_10m_max"), idx),
                        "sunrise": _index_or_none(daily_block.get("sunrise"), idx),
                        "sunset": _index_or_none(daily_block.get("sunset"), idx),
                    }
                )
        result: dict[str, Any] = {
            "location": {
                "name": location.get("name"),
                "country": location.get("country"),
                "country_code": location.get("country_code"),
                "latitude": latitude,
                "longitude": longitude,
                "timezone": payload.get("timezone"),
            },
            "current": payload.get("current"),
            "daily": daily_rows,
            "detail_level": dl,
        }
        if include_hourly:
            hourly = payload.get("hourly")
            hourly_units = payload.get("hourly_units")
            if dl == "compact":
                result["hourly_preview"] = _hourly_preview(hourly)
            else:
                result["hourly"] = hourly
                result["hourly_units"] = hourly_units
        if dl == "full":
            result["current_units"] = payload.get("current_units")
            result["daily_units"] = payload.get("daily_units")
        return structured_result(with_response_meta(result, tool="utils_get_weather_forecast"))
    except weather_client.WeatherClientError as exc:
        logger.exception("get_weather_forecast failed")
        return _upstream_error(str(exc), suggested_fix="Retry later or verify the location query.")


def _index_or_none(value: Any, idx: int) -> Any:
    return value[idx] if isinstance(value, list) and idx < len(value) else None


def _hourly_preview(hourly: Any) -> list[dict[str, Any]]:
    if not isinstance(hourly, dict):
        return []
    times = hourly.get("time") or []
    preview = []
    for idx, moment in enumerate(times[:24]):
        preview.append(
            {
                "time": moment,
                "temperature_2m": _index_or_none(hourly.get("temperature_2m"), idx),
                "apparent_temperature": _index_or_none(hourly.get("apparent_temperature"), idx),
                "precipitation_probability": _index_or_none(hourly.get("precipitation_probability"), idx),
                "precipitation": _index_or_none(hourly.get("precipitation"), idx),
                "weather_code": _index_or_none(hourly.get("weather_code"), idx),
                "wind_speed_10m": _index_or_none(hourly.get("wind_speed_10m"), idx),
            }
        )
    return preview
