"""Shared fixtures: local env, no Firestore on startup, TestClient, JWT helper."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

os.environ.setdefault("ENV", "local")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-integration-32bytes!!")
os.environ.setdefault("GCP_PROJECT_ID", "test-gcp-project")
# Avoid real Cloud Run Admin API calls on GET /admin/apps and GET /me/apps in tests.
os.environ.setdefault("MCP_PROXY_MANAGED_APPS_SKIP_CLOUD_RUN_SYNC", "1")

from auth.jwt_handler import create_access_token  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from users.schemas import UserInDB  # noqa: E402


def make_access_token(*, user_id: str, role: str, email: str) -> str:
    return create_access_token(user_id=user_id, role=role, email=email)


def sample_user(
    *,
    user_id: str = "user-1",
    role: str = "user",
    email: str = "user@example.com",
    status: str = "active",
) -> UserInDB:
    now = datetime.now(timezone.utc)
    return UserInDB(
        id=user_id,
        google_id=f"g-{user_id}",
        email=email,
        role=role,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(autouse=True)
def _clear_mcp_sessions():
    import mcp_proxy

    mcp_proxy._sessions.clear()
    yield
    mcp_proxy._sessions.clear()


@pytest.fixture(autouse=True)
def _mock_mcp_list_servers_by_default():
    """Avoid real Firestore during MCP handlers (initialize always calls list_servers)."""
    from unittest.mock import patch

    with patch("mcp_proxy.list_servers", return_value=[]):
        yield


@pytest.fixture
def client():
    import main

    with patch.object(main, "verify_servers_connection", return_value=0):
        with TestClient(main.app) as tc:
            yield tc


def _set_csrf_cookie(client: TestClient) -> None:
    client.cookies.set("mcp_csrf", "test-csrf-token", path="/")


@pytest.fixture
def auth_headers_admin(client):
    _set_csrf_cookie(client)
    token = make_access_token(user_id="admin-1", role="admin", email="admin@example.com")
    return {"Authorization": f"Bearer {token}", "X-CSRF-Token": "test-csrf-token"}


@pytest.fixture
def auth_headers_power_user(client):
    _set_csrf_cookie(client)
    token = make_access_token(user_id="pu-1", role="power_user", email="pu@example.com")
    return {"Authorization": f"Bearer {token}", "X-CSRF-Token": "test-csrf-token"}


@pytest.fixture
def auth_headers_user(client):
    _set_csrf_cookie(client)
    token = make_access_token(user_id="user-1", role="user", email="user@example.com")
    return {"Authorization": f"Bearer {token}", "X-CSRF-Token": "test-csrf-token"}
