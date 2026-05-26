"""Firestore persistence for MCP proxy improvement requests."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import gcp_firestore
from google.cloud.firestore import DocumentSnapshot, FieldFilter
from improvement_requests_schemas import (
    ImprovementRequestCreate,
    ImprovementRequestErrorContext,
    ImprovementRequestInDB,
)
from users.schemas import UserInDB

logger = logging.getLogger(__name__)


class ImprovementRequestRateLimitError(Exception):
    """Raised when the caller exceeded the improvement-request submission limit."""

    def __init__(self, message: str, *, retry_after_seconds: Optional[int] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _get_db():
    return gcp_firestore.get_client()


def _requests_coll():
    return _get_db().collection("improvement_requests")


def _rate_limits_coll():
    return _get_db().collection("improvement_request_rate_limits")


def _cooldown_seconds() -> int:
    raw = os.getenv("MCP_PROXY_IMPROVEMENT_REQUEST_COOLDOWN_SECONDS", "3600")
    try:
        return max(0, int(raw))
    except ValueError:
        return 3600


def _max_pending_per_user() -> int:
    raw = os.getenv("MCP_PROXY_IMPROVEMENT_REQUEST_MAX_PENDING_PER_USER", "5")
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _request_from_doc(doc: DocumentSnapshot) -> ImprovementRequestInDB:
    data = doc.to_dict() or {}
    raw_ctx = data.get("error_context")
    error_context = None
    if isinstance(raw_ctx, dict):
        try:
            error_context = ImprovementRequestErrorContext.model_validate(raw_ctx)
        except Exception:
            logger.warning("Ignoring invalid improvement request error_context for %s", doc.id)
    requester_kind = data.get("requester_kind", "human")
    if requester_kind not in ("human", "agent"):
        requester_kind = "human"
    return ImprovementRequestInDB(
        id=doc.id,
        requester_user_id=data.get("requester_user_id", ""),
        requester_email=data.get("requester_email", ""),
        requester_kind=requester_kind,
        requester_label=data.get("requester_label", ""),
        summary=data.get("summary", ""),
        details=data.get("details", ""),
        tool_names=list(data.get("tool_names") or []),
        server_ids=list(data.get("server_ids") or []),
        status=data.get("status", "pending"),
        error_context=error_context,
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


def create_improvement_request(
    *,
    user: UserInDB,
    payload: ImprovementRequestCreate,
) -> ImprovementRequestInDB:
    """Create a new pending improvement request after applying Firestore-backed rate limits."""
    now = datetime.now(timezone.utc)
    cooldown_seconds = _cooldown_seconds()
    max_pending = _max_pending_per_user()
    state = _get_rate_limit_state(user.id)
    last_requested_at = state.get("last_requested_at")
    if isinstance(last_requested_at, datetime) and cooldown_seconds > 0:
        retry_at = last_requested_at + timedelta(seconds=cooldown_seconds)
        if retry_at > now:
            retry_after = max(1, int((retry_at - now).total_seconds()))
            raise ImprovementRequestRateLimitError(
                f"Improvement requests are rate limited; retry in about {retry_after} seconds",
                retry_after_seconds=retry_after,
            )
    pending_count = _pending_request_count_for_user(user.id)
    if pending_count >= max_pending:
        raise ImprovementRequestRateLimitError(
            f"You already have {pending_count} pending improvement request(s); wait for review before submitting more"
        )

    ref = _requests_coll().document()
    data: Dict[str, Any] = {
        "requester_user_id": user.id,
        "requester_email": user.email or "",
        "requester_kind": user.kind,
        "requester_label": _requester_label(user),
        "summary": payload.summary,
        "details": payload.details or "",
        "tool_names": payload.tool_names,
        "server_ids": payload.server_ids,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "reviewed_by": None,
        "reviewed_at": None,
        "rate_limit": {
            "cooldown_seconds": cooldown_seconds,
            "max_pending_per_user": max_pending,
            "pending_count_after_create": pending_count + 1,
            "next_allowed_at": now + timedelta(seconds=cooldown_seconds),
        },
    }
    if payload.error_context is not None:
        data["error_context"] = payload.error_context.model_dump(exclude_none=True)
    ref.set(data)
    _rate_limits_coll().document(user.id).set(
        {
            "last_requested_at": now,
            "last_request_id": ref.id,
            "updated_at": now,
        },
        merge=True,
    )
    logger.info(
        "Created improvement request %s for user=%s targets=%s/%s",
        ref.id,
        user.id,
        len(payload.tool_names),
        len(payload.server_ids),
    )
    return _request_from_doc(ref.get())


def list_pending_improvement_requests() -> List[ImprovementRequestInDB]:
    """Return pending improvement requests sorted newest-first."""
    query = _requests_coll().where(filter=FieldFilter("status", "==", "pending"))
    out = [_request_from_doc(doc) for doc in query.stream()]
    out.sort(key=lambda item: (item.created_at or datetime.min).isoformat(), reverse=True)
    return out


def get_improvement_request(request_id: str) -> Optional[ImprovementRequestInDB]:
    """Fetch one improvement request by id."""
    doc = _requests_coll().document(request_id).get()
    if not doc.exists:
        return None
    return _request_from_doc(doc)


def approve_improvement_request(request_id: str, *, reviewed_by: str) -> Optional[ImprovementRequestInDB]:
    """Approve a pending improvement request and keep it in Firestore."""
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


def delete_improvement_request(request_id: str) -> bool:
    """Delete an improvement request. Used when admins decline a request."""
    ref = _requests_coll().document(request_id)
    doc = ref.get()
    if not doc.exists:
        return False
    ref.delete()
    return True
