"""Upstream auth: Cloud Run audience and OIDC header resolution."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from cloud_run_upstream import clear_id_token_cache, cloud_run_audience_from_url
from models import OAuth2UpstreamConfig, ServerConfig
from upstream_headers import resolve_upstream_headers


def test_cloud_run_audience_from_url():
    assert (
        cloud_run_audience_from_url("https://svc-abc123-ew.a.run.app/mcp-server") == "https://svc-abc123-ew.a.run.app/"
    )


def test_cloud_run_audience_invalid():
    with pytest.raises(ValueError):
        cloud_run_audience_from_url("not-a-url")


def test_resolve_upstream_headers_cloud_run_iam():
    clear_id_token_cache()
    cfg = ServerConfig(
        id="z",
        url="https://zendesk-mcp-test.example.run.app/mcp-server",
        upstream_auth="cloud_run_iam",
    )

    async def run():
        return await resolve_upstream_headers(cfg)

    with patch(
        "upstream_headers.fetch_id_token_for_audience",
        return_value="fake-id-token",
    ) as mock_fetch:
        headers = asyncio.run(run())
    mock_fetch.assert_called_once_with("https://zendesk-mcp-test.example.run.app/")
    assert headers == {"Authorization": "Bearer fake-id-token"}


def test_resolve_upstream_headers_cloud_run_iam_merges_extra_headers():
    """Private Cloud Run (Bearer) plus app-level headers e.g. X-API-Key."""
    clear_id_token_cache()
    cfg = ServerConfig(
        id="z",
        url="https://zendesk-mcp-test.example.run.app/mcp-server/mcp",
        credentials_header="X-API-Key: secret-key",
        upstream_auth="cloud_run_iam",
    )

    async def run():
        return await resolve_upstream_headers(cfg)

    with patch(
        "upstream_headers.fetch_id_token_for_audience",
        return_value="fake-id-token",
    ):
        headers = asyncio.run(run())
    assert headers == {
        "Authorization": "Bearer fake-id-token",
        "X-API-Key": "secret-key",
    }


def test_resolve_upstream_headers_cloud_run_iam_skips_authorization_from_extra():
    """Extra creds must not override the IAM Bearer token."""
    clear_id_token_cache()
    cfg = ServerConfig(
        id="z",
        url="https://zendesk-mcp-test.example.run.app/mcp",
        credentials_header="Authorization: Bearer user-token",
        upstream_auth="cloud_run_iam",
    )

    async def run():
        return await resolve_upstream_headers(cfg)

    with patch(
        "upstream_headers.fetch_id_token_for_audience",
        return_value="fake-id-token",
    ):
        headers = asyncio.run(run())
    assert headers == {"Authorization": "Bearer fake-id-token"}


def test_resolve_upstream_headers_secret_mode():
    cfg = ServerConfig(
        id="x",
        url="https://example.com/mcp",
        credentials_header="X-API-Key: abc",
        upstream_auth="headers",
    )

    async def run():
        return await resolve_upstream_headers(cfg)

    headers = asyncio.run(run())
    assert headers == {"X-API-Key": "abc"}


def test_resolve_upstream_headers_oauth2_requires_user_id():
    cfg = ServerConfig(
        id="x",
        url="https://example.com/mcp",
        upstream_auth="oauth2",
        oauth=OAuth2UpstreamConfig(
            authorization_endpoint="https://idp.example/oauth/authorize",
            token_endpoint="https://idp.example/oauth/token",
            client_id="cid",
        ),
    )

    async def run():
        return await resolve_upstream_headers(cfg)

    with pytest.raises(ValueError, match="user_id"):
        asyncio.run(run())


def test_resolve_upstream_headers_oauth2_merges_extra_and_bearer():
    cfg = ServerConfig(
        id="x",
        url="https://example.com/mcp",
        upstream_auth="oauth2",
        credentials_header="X-App: extra",
        oauth=OAuth2UpstreamConfig(
            authorization_endpoint="https://idp.example/oauth/authorize",
            token_endpoint="https://idp.example/oauth/token",
            client_id="cid",
        ),
    )

    async def run():
        return await resolve_upstream_headers(cfg, user_id="user-1")

    with patch(
        "upstream_headers.ensure_upstream_oauth_access_token",
        return_value=("access-token-xyz", "Bearer"),
    ):
        headers = asyncio.run(run())
    assert headers == {"Authorization": "Bearer access-token-xyz", "X-App": "extra"}
