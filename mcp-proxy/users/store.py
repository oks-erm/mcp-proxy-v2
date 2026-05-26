"""Firestore CRUD for users and access_requests in mcp-proxy-database."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import gcp_firestore
from google.cloud.firestore import DocumentSnapshot, FieldFilter
from users.schemas import (
    AccessRequestInDB,
    AccessRequestStatus,
    UserInDB,
    UserKind,
    UserRole,
    UserStatus,
)

logger = logging.getLogger(__name__)


def get_firestore_db():
    """Get Firestore client for mcp-proxy-database."""
    return gcp_firestore.get_client()


def _get_db():
    return get_firestore_db()


def user_in_db_from_document(doc: DocumentSnapshot) -> UserInDB:
    """Build UserInDB from a Firestore document snapshot (id + to_dict)."""
    d = doc.to_dict() or {}
    kind_raw = d.get("kind", "human")
    kind: UserKind = kind_raw if kind_raw in ("human", "agent") else "human"
    return UserInDB(
        id=doc.id,
        google_id=d.get("google_id", ""),
        email=d.get("email", ""),
        kind=kind,
        agent_name=d.get("agent_name", "") or "",
        agent_url=d.get("agent_url", "") or "",
        role=d.get("role", "user"),
        status=d.get("status", "pending"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


def _users_coll():
    return _get_db().collection("users")


def _access_requests_coll():
    return _get_db().collection("access_requests")


def get_user_by_id(user_id: str) -> Optional[UserInDB]:
    """Get user by document ID."""
    doc = _users_coll().document(user_id).get()
    if not doc.exists:
        return None
    return user_in_db_from_document(doc)


def get_user_by_google_id(google_id: str) -> Optional[UserInDB]:
    """Get user by Google subject ID."""
    q = _users_coll().where(filter=FieldFilter("google_id", "==", google_id)).limit(1)
    docs = list(q.stream())
    if not docs:
        return None
    doc = docs[0]
    return user_in_db_from_document(doc)


def get_user_by_email(email: str) -> Optional[UserInDB]:
    """Get user by email."""
    if not (email or "").strip():
        return None
    q = _users_coll().where(filter=FieldFilter("email", "==", email)).limit(1)
    docs = list(q.stream())
    if not docs:
        return None
    doc = docs[0]
    return user_in_db_from_document(doc)


def create_user(
    email: str,
    google_id: str,
    role: UserRole = "user",
    status: UserStatus = "pending",
) -> UserInDB:
    """Create a new user. Returns the created user with assigned ID."""
    now = datetime.now(timezone.utc)
    # Use email as doc ID for idempotency when same user logs in again, or use auto ID.
    # Plan says "Auto or stable ID" - we use auto ID so we can have multiple requests.
    ref = _users_coll().document()
    data: Dict[str, Any] = {
        "google_id": google_id,
        "email": email,
        "role": role,
        "status": status,
        "created_at": now,
        "updated_at": now,
    }
    ref.set(data)
    logger.info("Created user %s (email=%s)", ref.id, email)
    return UserInDB(
        id=ref.id,
        google_id=google_id,
        email=email,
        kind="human",
        agent_name="",
        agent_url="",
        role=role,
        status=status,
        created_at=now,
        updated_at=now,
    )


def create_agent(agent_name: str, agent_url: str = "") -> UserInDB:
    """Create a service agent (no Google login): role user, active, MCP key issued by admin."""
    now = datetime.now(timezone.utc)
    name = (agent_name or "").strip()
    url = (agent_url or "").strip()
    ref = _users_coll().document()
    data: Dict[str, Any] = {
        "google_id": "",
        "email": "",
        "kind": "agent",
        "agent_name": name,
        "agent_url": url,
        "role": "user",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    ref.set(data)
    logger.info("Created agent user_id=%s agent_name=%s", ref.id, name)
    return UserInDB(
        id=ref.id,
        google_id="",
        email="",
        kind="agent",
        agent_name=name,
        agent_url=url,
        role="user",
        status="active",
        created_at=now,
        updated_at=now,
    )


def update_agent_profile(
    user_id: str,
    *,
    agent_name: Optional[str] = None,
    agent_url: Optional[str] = None,
) -> Optional[UserInDB]:
    """Update agent_name / agent_url for kind=agent only. None means leave unchanged."""
    ref = _users_coll().document(user_id)
    doc = ref.get()
    if not doc.exists:
        return None
    d = doc.to_dict() or {}
    if d.get("kind") != "agent":
        return None
    updates: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if agent_name is not None:
        updates["agent_name"] = (agent_name or "").strip()
    if agent_url is not None:
        updates["agent_url"] = (agent_url or "").strip()
    if len(updates) <= 1:
        return get_user_by_id(user_id)
    ref.update(updates)
    return get_user_by_id(user_id)


def update_user(
    user_id: str,
    *,
    google_id: Optional[str] = None,
    role: Optional[UserRole] = None,
    status: Optional[UserStatus] = None,
) -> Optional[UserInDB]:
    """Update user fields. Returns updated user or None if not found."""
    ref = _users_coll().document(user_id)
    doc = ref.get()
    if not doc.exists:
        return None
    updates: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if google_id is not None:
        updates["google_id"] = google_id
    if role is not None:
        updates["role"] = role
    if status is not None:
        updates["status"] = status
    ref.update(updates)
    return get_user_by_id(user_id)


def list_users() -> List[UserInDB]:
    """List all users ordered by created_at."""
    docs = _users_coll().order_by("created_at").stream()
    out = []
    for doc in docs:
        out.append(user_in_db_from_document(doc))
    return out


def create_access_request(user_id: str, email: str) -> AccessRequestInDB:
    """Create an access request."""
    now = datetime.now(timezone.utc)
    ref = _access_requests_coll().document()
    ref.set(
        {
            "user_id": user_id,
            "email": email,
            "requested_at": now,
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
        }
    )
    return AccessRequestInDB(
        id=ref.id,
        user_id=user_id,
        email=email,
        requested_at=now,
        status="pending",
        reviewed_by=None,
        reviewed_at=None,
    )


def get_access_requests_by_user(user_id: str) -> List[AccessRequestInDB]:
    """Get access requests for a user."""
    q = _access_requests_coll().where(filter=FieldFilter("user_id", "==", user_id))
    out = []
    for doc in q.stream():
        d = doc.to_dict() or {}
        out.append(
            AccessRequestInDB(
                id=doc.id,
                user_id=d.get("user_id", ""),
                email=d.get("email", ""),
                requested_at=d.get("requested_at"),
                status=d.get("status", "pending"),
                reviewed_by=d.get("reviewed_by"),
                reviewed_at=d.get("reviewed_at"),
            )
        )
    out.sort(key=lambda r: (r.requested_at or datetime.min).isoformat(), reverse=True)
    return out


def get_pending_access_requests() -> List[AccessRequestInDB]:
    """Pending access requests for users still awaiting approval.

    Rows can be left ``pending`` if a user was promoted (e.g. bootstrap admin) without
    updating the access_request document; those are omitted so the approvals UI matches
    actionable queue items.
    """
    pending_user_ids = {u.id for u in get_pending_users()}
    q = _access_requests_coll().where(filter=FieldFilter("status", "==", "pending"))
    out = []
    for doc in q.stream():
        d = doc.to_dict() or {}
        uid = d.get("user_id", "")
        if uid not in pending_user_ids:
            continue
        out.append(
            AccessRequestInDB(
                id=doc.id,
                user_id=uid,
                email=d.get("email", ""),
                requested_at=d.get("requested_at"),
                status=d.get("status", "pending"),
                reviewed_by=d.get("reviewed_by"),
                reviewed_at=d.get("reviewed_at"),
            )
        )
    out.sort(key=lambda r: (r.requested_at or datetime.min).isoformat(), reverse=True)
    return out


def update_access_request(
    request_id: str,
    status: AccessRequestStatus,
    reviewed_by: str,
) -> Optional[AccessRequestInDB]:
    """Update access request status (approve/reject)."""
    now = datetime.now(timezone.utc)
    ref = _access_requests_coll().document(request_id)
    doc = ref.get()
    if not doc.exists:
        return None
    ref.update(
        {
            "status": status,
            "reviewed_by": reviewed_by,
            "reviewed_at": now,
        }
    )
    d = ref.get().to_dict() or {}
    return AccessRequestInDB(
        id=ref.id,
        user_id=d.get("user_id", ""),
        email=d.get("email", ""),
        requested_at=d.get("requested_at"),
        status=d.get("status", "pending"),
        reviewed_by=d.get("reviewed_by"),
        reviewed_at=d.get("reviewed_at"),
    )


def get_pending_users() -> List[UserInDB]:
    """List users with status pending."""
    q = _users_coll().where(filter=FieldFilter("status", "==", "pending"))
    out = []
    for doc in q.stream():
        out.append(user_in_db_from_document(doc))
    out.sort(key=lambda u: (u.created_at or datetime.min).isoformat())
    return out
