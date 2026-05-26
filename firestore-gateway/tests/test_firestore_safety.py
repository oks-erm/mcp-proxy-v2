"""Unit tests for Firestore gateway collection policy and redaction."""

from __future__ import annotations

import os
from unittest.mock import patch

import firestore_safety as firestore_safety_mod
import pytest
from firestore_safety import (
    collection_access_error,
    collection_matches_denied_pattern,
    filter_visible_collection_ids,
    is_collection_denied,
    redact_document,
)


def test_default_denied_constant_lists_credential_collections():
    assert "tokens" in firestore_safety_mod._DEFAULT_DENIED
    assert "n8n-tokens" in firestore_safety_mod._DEFAULT_DENIED


def test_default_denied_patterns_cover_ephemeral_collections():
    """Default patterns should hide tmp, migration, webhooks, monitoring collections."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS", None)
        assert collection_matches_denied_pattern("tmp_jobs")
        assert collection_matches_denied_pattern("schema_migration")
        assert collection_matches_denied_pattern("stripe_webhooks")
        assert collection_matches_denied_pattern("monitoring_alerts")
        assert not collection_matches_denied_pattern("reservations")


def test_pattern_deny_env():
    with patch.dict(
        os.environ,
        {
            "FIRESTORE_GATEWAY_DENIED_COLLECTIONS": "",
            "FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS": "foo-*,*-bar",
        },
    ):
        assert collection_matches_denied_pattern("foo-x")
        assert collection_matches_denied_pattern("x-bar")
        assert not collection_matches_denied_pattern("other")
        err = collection_access_error("foo-secret")
        assert err is not None
        assert "PATTERNS" in err


def test_filter_visible_collection_ids_order_preserved():
    with patch.dict(
        os.environ,
        {
            "FIRESTORE_GATEWAY_DENIED_COLLECTIONS": "tokens",
            "FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS": "",
        },
    ):
        inp = ["a", "tokens", "b"]
        assert filter_visible_collection_ids(inp) == ["a", "b"]


def test_is_collection_denied_exact():
    with patch.dict(
        os.environ, {"FIRESTORE_GATEWAY_DENIED_COLLECTIONS": "x", "FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS": ""}
    ):
        assert is_collection_denied("x")
        assert not is_collection_denied("y")


@pytest.mark.parametrize(
    "key,expected_redacted",
    [
        ("access_token", True),
        ("safe_field", False),
        ("my_api_key", True),
    ],
)
def test_redact_sensitive_keys(key: str, expected_redacted: bool):
    doc = {key: "secret-value", "plain": 1}
    out = redact_document(doc)
    if expected_redacted:
        assert out[key] == "<redacted>"
    else:
        assert out[key] == "secret-value"
