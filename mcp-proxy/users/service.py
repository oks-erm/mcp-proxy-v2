"""User and access request service: first-login flow, admin shortcuts, etc."""

import logging
import re
from typing import Optional, Set, Tuple

import config
from permissions.models import PermissionItem
from permissions.store import set_user_permissions
from servers.store import list_servers
from users import store
from users.schemas import AccessRequestInDB, UserInDB

logger = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _bootstrap_admin_emails() -> Set[str]:
    raw = getattr(config, "MCP_PROXY_BOOTSTRAP_ADMIN_EMAILS", "") or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _is_bootstrap_admin_email(email: str) -> bool:
    return email.strip().lower() in _bootstrap_admin_emails()


def _sync_bootstrap_admin_permissions(user_id: str) -> None:
    """Persist read+write on every configured MCP server so the admin UI and data stay aligned."""
    try:
        servers = list_servers(enabled_only=False)
        perms = [PermissionItem(server_id=s.id, read=True, write=True) for s in servers]
        set_user_permissions(user_id, perms)
        logger.info(
            "Bootstrap admin permissions synced user_id=%s server_count=%d",
            user_id,
            len(perms),
        )
    except Exception as e:
        logger.warning("Bootstrap admin permission sync failed for %s: %s", user_id, e)


def _ensure_bootstrap_admin(user: UserInDB) -> UserInDB:
    """If email is listed in MCP_PROXY_BOOTSTRAP_ADMIN_EMAILS, force admin + active."""
    if not _is_bootstrap_admin_email(user.email):
        return user
    if user.role != "admin" or user.status != "active":
        updated = store.update_user(user.id, role="admin", status="active")
        user = updated or user
        logger.info("Bootstrap admin applied for email=%s user_id=%s", user.email, user.id)
    for req in store.get_access_requests_by_user(user.id):
        if req.status == "pending":
            store.update_access_request(req.id, "approved", user.id)
    _sync_bootstrap_admin_permissions(user.id)
    return user


def create_ad_hoc_user(email: str) -> UserInDB:
    """
    Admin-only shortcut to create an active human user without going through Google login.

    The user starts with an empty google_id, so a later Google login can still attach to the
    same record by matching on email.
    """
    normalized_email = (email or "").strip().lower()
    if not normalized_email or not _EMAIL_RE.match(normalized_email):
        raise ValueError("email must be a valid email address")
    existing = store.get_user_by_email(normalized_email)
    if existing:
        raise ValueError("User with this email already exists")
    user = store.create_user(
        email=normalized_email,
        google_id="",
        role="user",
        status="active",
    )
    logger.info("Created ad-hoc user_id=%s email=%s", user.id, normalized_email)
    return user


def ensure_user_and_request(
    email: str,
    google_id: str,
) -> Tuple[UserInDB, Optional[AccessRequestInDB], bool]:
    """
    On first login: get or create user; create access_request if new user.
    Returns (user, access_request_if_created, is_new_user).
    """
    user = store.get_user_by_google_id(google_id)
    if user:
        return _ensure_bootstrap_admin(user), None, False
    user = store.get_user_by_email(email)
    if user:
        # Same email, first time with this Google ID - update google_id
        store.update_user(user.id, google_id=google_id)
        user = store.get_user_by_id(user.id) or user
        return _ensure_bootstrap_admin(user), None, False
    # New user: bootstrap admins skip approval
    if _is_bootstrap_admin_email(email):
        user = store.create_user(
            email=email,
            google_id=google_id,
            role="admin",
            status="active",
        )
        logger.info("Created bootstrap admin user_id=%s email=%s", user.id, email)
        _sync_bootstrap_admin_permissions(user.id)
        return user, None, True
    user = store.create_user(email=email, google_id=google_id, role="user", status="pending")
    req = store.create_access_request(user_id=user.id, email=email)
    return user, req, True


def get_user_status_for_response(user: UserInDB) -> str:
    """Return status string for API: active, waiting_for_approval, or rejected."""
    if user.status == "active":
        return "active"
    if user.status == "pending":
        return "waiting_for_approval"
    return "rejected"
