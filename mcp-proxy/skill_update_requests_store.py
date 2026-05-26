"""Firestore persistence for skill update requests."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import gcp_firestore
from google.cloud.firestore import DocumentSnapshot, FieldFilter
from skill_update_requests_schemas import (
    SkillUpdateRequestCreate,
    SkillUpdateRequestInDB,
)
from users.schemas import UserInDB

logger = logging.getLogger(__name__)


class SkillUpdateRequestRateLimitError(Exception):
    """Raised when the caller exceeded the skill update request limit."""

    def __init__(self, message: str, *, retry_after_seconds: Optional[int] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _get_db():
    return gcp_firestore.get_client()


def _requests_coll():
    return _get_db().collection("skill_update_requests")


def _rate_limits_coll():
    return _get_db().collection("skill_update_request_rate_limits")


def _cooldown_seconds() -> int:
    raw = os.getenv("MCP_PROXY_SKILL_UPDATE_REQUEST_COOLDOWN_SECONDS", "1800")
    try:
        return max(0, int(raw))
    except ValueError:
        return 1800


def _max_pending_per_user() -> int:
    raw = os.getenv("MCP_PROXY_SKILL_UPDATE_REQUEST_MAX_PENDING_PER_USER", "5")
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _request_from_doc(doc: DocumentSnapshot) -> SkillUpdateRequestInDB:
    data = doc.to_dict() or {}
    requester_kind = data.get("requester_kind", "human")
    if requester_kind not in ("human", "agent"):
        requester_kind = "human"
    return SkillUpdateRequestInDB(
        id=doc.id,
        skill_name=data.get("skill_name", ""),
        requester_user_id=data.get("requester_user_id", ""),
        requester_email=data.get("requester_email", ""),
        requester_kind=requester_kind,
        requester_label=data.get("requester_label", ""),
        summary=data.get("summary", ""),
        details=data.get("details", ""),
        desired_outcome=data.get("desired_outcome", ""),
        status=data.get("status", "pending"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        reviewed_by=data.get("reviewed_by"),
        reviewed_at=data.get("reviewed_at"),
    )


def _requester_label(user: UserInDB) -> str:
    if user.kind == "agent" and user.agent_name:
        return user.agent_name
    if user.email:
        return user.email
    return user.id


def _pending_request_count_for_user(user_id: str) -> int:
    query = _requests_coll().where(filter=FieldFilter("requester_user_id", "==", user_id))
    return sum(1 for doc in query.stream() if (doc.to_dict() or {}).get("status", "pending") == "pending")


def _get_rate_limit_state(user_id: str) -> Dict[str, Any]:
    doc = _rate_limits_coll().document(user_id).get()
    return doc.to_dict() or {}


def create_skill_update_request(*, user: UserInDB, payload: SkillUpdateRequestCreate) -> SkillUpdateRequestInDB:
    """Create a new pending skill update request with basic rate limiting."""
    now = datetime.now(timezone.utc)
    cooldown_seconds = _cooldown_seconds()
    max_pending = _max_pending_per_user()
    state = _get_rate_limit_state(user.id)
    last_requested_at = state.get("last_requested_at")
    if isinstance(last_requested_at, datetime) and cooldown_seconds > 0:
        retry_at = last_requested_at + timedelta(seconds=cooldown_seconds)
        if retry_at > now:
            retry_after = max(1, int((retry_at - now).total_seconds()))
            raise SkillUpdateRequestRateLimitError(
                f"Skill update requests are rate limited; retry in about {retry_after} seconds",
                retry_after_seconds=retry_after,
            )
    pending_count = _pending_request_count_for_user(user.id)
    if pending_count >= max_pending:
        raise SkillUpdateRequestRateLimitError(
            f"You already have {pending_count} pending skill update request(s); wait for review before submitting more"
        )

    ref = _requests_coll().document()
    data = {
        "skill_name": payload.skill_name,
        "requester_user_id": user.id,
        "requester_email": user.email or "",
        "requester_kind": user.kind,
        "requester_label": _requester_label(user),
        "summary": payload.summary,
        "details": payload.details or "",
        "desired_outcome": payload.desired_outcome or "",
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "reviewed_by": None,
        "reviewed_at": None,
    }
    ref.set(data)
    _rate_limits_coll().document(user.id).set(
        {
            "last_requested_at": now,
            "last_request_id": ref.id,
            "updated_at": now,
        },
        merge=True,
    )
    logger.info("Created skill update request %s for user=%s skill=%s", ref.id, user.id, payload.skill_name)
    return _request_from_doc(ref.get())


def list_pending_skill_update_requests() -> List[SkillUpdateRequestInDB]:
    """Return pending skill update requests sorted newest-first."""
    query = _requests_coll().where(filter=FieldFilter("status", "==", "pending"))
    out = [_request_from_doc(doc) for doc in query.stream()]
    out.sort(key=lambda item: (item.created_at or datetime.min).isoformat(), reverse=True)
    return out


def get_skill_update_request(request_id: str) -> Optional[SkillUpdateRequestInDB]:
    """Fetch one skill update request by id."""
    doc = _requests_coll().document(request_id).get()
    if not doc.exists:
        return None
    return _request_from_doc(doc)


def approve_skill_update_request(request_id: str, *, reviewed_by: str) -> Optional[SkillUpdateRequestInDB]:
    """Approve a pending skill update request and keep its record."""
    ref = _requests_coll().document(request_id)
    doc = ref.get()
    if not doc.exists:
        return None
    current = _request_from_doc(doc)
    if current.status != "pending":
        return current
    now = datetime.now(timezone.utc)
    ref.update(
        {
            "status": "approved",
            "reviewed_by": reviewed_by,
            "reviewed_at": now,
            "updated_at": now,
        }
    )
    return _request_from_doc(ref.get())


def delete_skill_update_request(request_id: str) -> bool:
    """Delete a skill update request. Used when admins decline a request."""
    ref = _requests_coll().document(request_id)
    doc = ref.get()
    if not doc.exists:
        return False
    ref.delete()
    return True
