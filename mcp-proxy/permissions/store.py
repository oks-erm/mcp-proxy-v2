"""Firestore CRUD for permissions collection in mcp-proxy-database."""

import logging
from datetime import datetime, timezone
from typing import List

import gcp_firestore
from google.cloud.firestore import FieldFilter
from permissions.models import PermissionItem

logger = logging.getLogger(__name__)


def _get_db():
    return gcp_firestore.get_client()


def _permissions_coll():
    return _get_db().collection("permissions")


def _doc_id(user_id: str, server_id: str) -> str:
    """Composite document ID for permission (user_id, server_id)."""
    return f"{user_id}_{server_id}"


def get_user_permissions(user_id: str) -> List[PermissionItem]:
    """List all permissions for a user."""
    q = _permissions_coll().where(filter=FieldFilter("user_id", "==", user_id))
    out = []
    for doc in q.stream():
        d = doc.to_dict() or {}
        out.append(
            PermissionItem(
                server_id=d.get("server_id", ""),
                read=d.get("read", True),
                write=d.get("write", False),
            )
        )
    return out


def set_user_permissions(user_id: str, permissions: List[PermissionItem]) -> None:
    """Replace all permissions for a user (delete existing, write new)."""
    coll = _permissions_coll()
    # Delete existing
    q = coll.where(filter=FieldFilter("user_id", "==", user_id))
    for doc in q.stream():
        doc.reference.delete()
    now = datetime.now(timezone.utc)
    for p in permissions:
        doc_id = _doc_id(user_id, p.server_id)
        coll.document(doc_id).set(
            {
                "user_id": user_id,
                "server_id": p.server_id,
                "read": p.read,
                "write": p.write,
                "created_at": now,
                "updated_at": now,
            }
        )
    logger.info("Set %d permissions for user %s", len(permissions), user_id)
