"""Bootstrap admin emails (MCP_PROXY_BOOTSTRAP_ADMIN_EMAILS)."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from models import ServerConfig
from users.schemas import AccessRequestInDB, UserInDB
from users.service import create_ad_hoc_user, ensure_user_and_request


def test_bootstrap_creates_admin_active():
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    created = UserInDB(
        id="u1",
        email="boss@example.com",
        google_id="sub-1",
        role="admin",
        status="active",
        created_at=now,
        updated_at=now,
    )
    srv = ServerConfig(id="srv-a", url="https://example.com/mcp", enabled=True)
    with patch("users.service._bootstrap_admin_emails", return_value={"boss@example.com"}):
        with patch("users.service.store.get_user_by_google_id", return_value=None):
            with patch("users.service.store.get_user_by_email", return_value=None):
                with patch("users.service.store.create_user", return_value=created) as cu:
                    with patch("users.service.store.create_access_request") as car:
                        with patch("users.service.list_servers", return_value=[srv]):
                            with patch("users.service.set_user_permissions") as sup:
                                u, req, is_new = ensure_user_and_request("boss@example.com", "sub-1")
    assert is_new is True
    assert req is None
    assert u.role == "admin"
    cu.assert_called_once()
    assert cu.call_args.kwargs["role"] == "admin"
    assert cu.call_args.kwargs["status"] == "active"
    car.assert_not_called()
    sup.assert_called_once()
    assert sup.call_args[0][0] == "u1"
    saved = sup.call_args[0][1]
    assert len(saved) == 1 and saved[0].server_id == "srv-a" and saved[0].read and saved[0].write


def test_bootstrap_upgrades_pending_user():
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    pending = UserInDB(
        id="u1",
        email="boss@example.com",
        google_id="sub-1",
        role="user",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    upgraded = UserInDB(
        id="u1",
        email="boss@example.com",
        google_id="sub-1",
        role="admin",
        status="active",
        created_at=now,
        updated_at=now,
    )
    srv = ServerConfig(id="srv-b", url="https://b.example/mcp", enabled=True)
    with patch("users.service._bootstrap_admin_emails", return_value={"boss@example.com"}):
        with patch("users.service.store.get_user_by_google_id", return_value=pending):
            with patch("users.service.store.update_user", return_value=upgraded) as upd:
                with patch("users.service.store.get_access_requests_by_user", return_value=[]):
                    with patch("users.service.list_servers", return_value=[srv]):
                        with patch("users.service.set_user_permissions") as sup:
                            u, req, is_new = ensure_user_and_request("boss@example.com", "sub-1")
    assert is_new is False
    assert req is None
    assert u.status == "active" and u.role == "admin"
    upd.assert_called_once_with("u1", role="admin", status="active")
    sup.assert_called_once()
    saved = sup.call_args[0][1]
    assert len(saved) == 1 and saved[0].server_id == "srv-b" and saved[0].write


def test_bootstrap_upgrades_pending_user_closes_access_requests():
    now = datetime.now(timezone.utc)
    pending = UserInDB(
        id="u1",
        email="boss@example.com",
        google_id="sub-1",
        role="user",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    upgraded = UserInDB(
        id="u1",
        email="boss@example.com",
        google_id="sub-1",
        role="admin",
        status="active",
        created_at=now,
        updated_at=now,
    )
    ar = AccessRequestInDB(
        id="ar1",
        user_id="u1",
        email="boss@example.com",
        requested_at=now,
        status="pending",
    )
    srv = ServerConfig(id="srv-b", url="https://b.example/mcp", enabled=True)
    with patch("users.service._bootstrap_admin_emails", return_value={"boss@example.com"}):
        with patch("users.service.store.get_user_by_google_id", return_value=pending):
            with patch("users.service.store.update_user", return_value=upgraded):
                with patch("users.service.store.get_access_requests_by_user", return_value=[ar]):
                    with patch("users.service.store.update_access_request") as uar:
                        with patch("users.service.list_servers", return_value=[srv]):
                            with patch("users.service.set_user_permissions"):
                                ensure_user_and_request("boss@example.com", "sub-1")
    uar.assert_called_once_with("ar1", "approved", "u1")


def test_create_ad_hoc_user_creates_active_human():
    now = datetime.now(timezone.utc)
    created = UserInDB(
        id="u-ad-hoc",
        email="person@example.com",
        google_id="",
        role="user",
        status="active",
        created_at=now,
        updated_at=now,
    )
    with patch("users.service.store.get_user_by_email", return_value=None):
        with patch("users.service.store.create_user", return_value=created) as cu:
            user = create_ad_hoc_user(" Person@Example.com ")
    assert user.id == "u-ad-hoc"
    cu.assert_called_once_with(
        email="person@example.com",
        google_id="",
        role="user",
        status="active",
    )


def test_create_ad_hoc_user_rejects_existing_email():
    existing = UserInDB(
        id="u-existing",
        email="person@example.com",
        google_id="",
        role="user",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    with patch("users.service.store.get_user_by_email", return_value=existing):
        with pytest.raises(ValueError, match="already exists"):
            create_ad_hoc_user("person@example.com")
