from __future__ import annotations

import importlib
import sys
from unittest.mock import patch

import pytest
import stripe as _stripe
from util import (
    assert_error_dict,
    assert_error_dict_normalized,
    assert_success_dict,
    load_mcp_server,
)


@pytest.fixture
def stripe_mod():
    return load_mcp_server("stripe-mcp")


@pytest.mark.asyncio
async def test_stripe_get_balance_mocked(stripe_mod):
    async def fake_balance(*, detail_level=None):
        return {
            "object": "balance",
            "available": [{"amount": 100, "currency": "eur"}],
            "pending": [],
            "livemode": False,
            "detail_level": detail_level or "compact",
        }

    with patch.object(stripe_mod, "get_balance", side_effect=fake_balance):
        res = await stripe_mod.stripe_get_balance("compact")
    assert_success_dict(res.structuredContent)
    assert res.structuredContent.get("object") == "balance"


@pytest.mark.asyncio
async def test_stripe_get_refund_missing_id_validation(stripe_mod):
    """Empty refund_id must return validation_error with retryable=False."""
    res = await stripe_mod.stripe_get_refund(refund_id=None)
    p = assert_error_dict(res.structuredContent)
    assert p["error"] == "validation_error"
    assert p.get("retryable") is False


@pytest.mark.asyncio
async def test_stripe_get_refund_fake_id_not_found(stripe_mod):
    """A nonexistent refund ID must yield not_found (cause=not_found, retryable=False), not stripe_api_error."""
    fake_exc = _stripe.InvalidRequestError(
        message="No such refund: 're_fake_does_not_exist'",
        param=None,
        code="resource_missing",
        http_status=404,
        http_body=None,
        json_body=None,
        headers=None,
    )

    # get_refund lives in stripe_ops (imported into mcp_server). Patch _get_client on the
    # stripe_ops module that was loaded as a side-effect of load_mcp_server("stripe-mcp").
    # Setting side_effect to an exception *instance* makes mock raise it directly — no
    # wrapper function needed, so no arity mismatch.
    stripe_ops = sys.modules.get("stripe_ops") or importlib.import_module("stripe_ops")
    with patch.object(stripe_ops, "_get_client") as mock_client:
        mock_client.return_value.v1.refunds.retrieve.side_effect = fake_exc
        res = await stripe_mod.stripe_get_refund(refund_id="re_fake_does_not_exist")

    p = assert_error_dict_normalized(res.structuredContent)
    assert p["error"] == "not_found", f"expected not_found, got {p['error']!r}"
    assert p["cause"] == "not_found"
    assert p["retryable"] is False
