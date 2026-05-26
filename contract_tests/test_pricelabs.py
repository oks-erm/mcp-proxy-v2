from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from util import (
    assert_error_dict_normalized,
    assert_meta_tool,
    assert_success_dict,
    load_mcp_server,
)


@pytest.fixture
def pricelabs_mod():
    return load_mcp_server("pricelabs-mcp")


@pytest.mark.asyncio
async def test_neighborhood_data_success_meta(pricelabs_mod):
    payload = {
        "status": "Success",
        "data": {
            "Listings Used": 350,
            "currency": "EUR",
            "lat": 48.2041,
            "lng": 16.3697,
            "source": "airbnb",
            "Neighborhood Data Source": "Nearby Listings",
        },
    }
    with patch.object(pricelabs_mod.pricelabs_client, "get_neighborhood_data", return_value=payload):
        r = await pricelabs_mod.pricelabs_get_neighborhood_data(pms="vrm", listing_id="abc-123")

    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="pricelabs_get_neighborhood_data")
    assert p["status"] == "Success"
    assert p["data"]["Listings Used"] == 350


@pytest.mark.asyncio
async def test_date_specific_overrides_success_count_and_meta(pricelabs_mod):
    payload = {
        "overrides": [
            {
                "date": "2026-06-01",
                "price": "144",
                "price_type": "fixed",
                "currency": "EUR",
                "min_stay": 5,
            },
            {
                "date": "2026-06-02",
                "price": "10",
                "price_type": "percent",
                "min_stay": 4,
            },
        ]
    }
    with patch.object(pricelabs_mod.pricelabs_client, "get_date_specific_overrides", return_value=payload):
        r = await pricelabs_mod.pricelabs_get_date_specific_overrides(listing_id="abc-123", pms="airbnb")

    p = assert_success_dict(r.structuredContent)
    assert_meta_tool(p, expected_tool="pricelabs_get_date_specific_overrides")
    assert p["count"] == 2
    assert p["overrides"] == payload["overrides"]


@pytest.mark.asyncio
async def test_neighborhood_data_requires_pms(pricelabs_mod):
    r = await pricelabs_mod.pricelabs_get_neighborhood_data(pms="", listing_id="abc-123")
    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "validation_error"
    assert p["cause"] == "validation"
    assert p["retryable"] is False


@pytest.mark.asyncio
async def test_date_specific_overrides_requires_listing_id(pricelabs_mod):
    r = await pricelabs_mod.pricelabs_get_date_specific_overrides(listing_id="", pms="airbnb")
    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "validation_error"
    assert p["cause"] == "validation"
    assert p["retryable"] is False


@pytest.mark.asyncio
async def test_date_specific_overrides_not_found(pricelabs_mod):
    err = pricelabs_mod.pricelabs_client.PriceLabsAPIError(
        "not_found",
        "Listing not found",
        status_code=404,
        retryable=False,
        cause="not_found",
    )
    with patch.object(pricelabs_mod.pricelabs_client, "get_date_specific_overrides", side_effect=err):
        r = await pricelabs_mod.pricelabs_get_date_specific_overrides(listing_id="missing", pms="airbnb")

    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "not_found"
    assert p["details"] == "Listing not found"
    assert p["status_code"] == 404
    assert p["cause"] == "not_found"
    assert p["retryable"] is False


@pytest.mark.asyncio
async def test_neighborhood_data_rate_limited_retryable(pricelabs_mod):
    err = pricelabs_mod.pricelabs_client.PriceLabsAPIError(
        "rate_limited",
        "Too Many Requests",
        status_code=429,
        retryable=True,
        cause="upstream_error",
    )
    with patch.object(pricelabs_mod.pricelabs_client, "get_neighborhood_data", side_effect=err):
        r = await pricelabs_mod.pricelabs_get_neighborhood_data(pms="vrm", listing_id="abc-123")

    p = assert_error_dict_normalized(r.structuredContent)
    assert p["error"] == "rate_limited"
    assert p["status_code"] == 429
    assert p["cause"] == "upstream_error"
    assert p["retryable"] is True
    assert "60 requests/minute" in p["suggested_fix"]


def test_client_sends_pricelabs_api_key_and_params(pricelabs_mod):
    response = MagicMock()
    response.ok = True
    response.status_code = 200
    response.json.return_value = {"status": "Success", "data": {}}
    client = pricelabs_mod.pricelabs_client.PriceLabsClient(api_key="secret")
    with patch.object(client._session, "get", return_value=response) as get:
        assert client.get_neighborhood_data(pms="vrm", listing_id="abc") == {"status": "Success", "data": {}}

    _, kwargs = get.call_args
    assert kwargs["headers"]["X-API-Key"] == "secret"
    assert kwargs["params"] == {"pms": "vrm", "listing_id": "abc"}
    assert kwargs["timeout"] == 300.0
