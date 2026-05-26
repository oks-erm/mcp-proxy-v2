"""Firestore persistence for n8n workflows created through the MCP proxy."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import gcp_firestore
from google.cloud.firestore import DocumentSnapshot
from models import ManagedWorkflowRecord
from users.schemas import UserInDB

logger = logging.getLogger(__name__)


def _get_db():
    return gcp_firestore.get_client()


def _collection():
    collection_name = os.getenv("MCP_PROXY_MANAGED_WORKFLOWS_COLLECTION", "managed_n8n_workflows")
    return _get_db().collection(collection_name)


def _user_label(user: UserInDB) -> str:
    if user.kind == "agent" and user.agent_name:
        return user.agent_name
    if user.email:
        return user.email
    return user.id


def _record_from_doc(doc: DocumentSnapshot) -> ManagedWorkflowRecord:
    data = doc.to_dict() or {}
    kind = data.get("created_by_kind", "human")
    if kind not in ("human", "agent"):
        kind = "human"
    return ManagedWorkflowRecord(
        workflow_id=doc.id,
        name=data.get("name", ""),
        summary=data.get("summary", ""),
        editor_url=data.get("editor_url"),
        server_id=data.get("server_id", "n8n"),
        created_by_user_id=data.get("created_by_user_id", ""),
        created_by_email=data.get("created_by_email", ""),
        created_by_kind=kind,
        created_by_label=data.get("created_by_label", ""),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        deleted_at=data.get("deleted_at"),
    )


def upsert_managed_workflow(
    *,
    workflow_id: str,
    name: str,
    summary: str,
    editor_url: Optional[str],
    user: UserInDB,
    server_id: str = "n8n",
) -> ManagedWorkflowRecord:
    """Create or refresh a managed-workflow record after a successful MCP create."""
    now = datetime.now(timezone.utc)
    ref = _collection().document(str(workflow_id))
    existing = ref.get()
    data: Dict[str, Any] = {
        "name": name,
        "summary": summary,
        "editor_url": editor_url,
        "server_id": server_id,
        "created_by_user_id": user.id,
        "created_by_email": user.email or "",
        "created_by_kind": user.kind,
        "created_by_label": _user_label(user),
        "updated_at": now,
        "deleted_at": None,
    }
    if existing.exists:
        current = existing.to_dict() or {}
        data["created_at"] = current.get("created_at") or now
        if current.get("created_by_user_id"):
            data["created_by_user_id"] = current.get("created_by_user_id", user.id)
            data["created_by_email"] = current.get("created_by_email", user.email or "")
            data["created_by_kind"] = current.get("created_by_kind", user.kind)
            data["created_by_label"] = current.get("created_by_label", _user_label(user))
        ref.set(data, merge=True)
    else:
        data["created_at"] = now
        ref.set(data)
    logger.info("Upserted managed n8n workflow record: %s", workflow_id)
    return _record_from_doc(ref.get())


def list_managed_workflows(*, include_deleted: bool = False) -> List[ManagedWorkflowRecord]:
    """Return managed workflows sorted newest-first."""
    docs = [_record_from_doc(doc) for doc in _collection().stream()]
    if not include_deleted:
        docs = [item for item in docs if item.deleted_at is None]
    docs.sort(key=lambda item: (item.created_at or datetime.min.replace(tzinfo=timezone.utc)).isoformat(), reverse=True)
    return docs


def get_managed_workflow(workflow_id: str) -> Optional[ManagedWorkflowRecord]:
    """Fetch one managed workflow by workflow id."""
    doc = _collection().document(str(workflow_id)).get()
    if not doc.exists:
        return None
    return _record_from_doc(doc)


def mark_managed_workflow_deleted(workflow_id: str) -> Optional[ManagedWorkflowRecord]:
    """Mark a managed workflow deleted after the upstream n8n delete succeeds."""
    ref = _collection().document(str(workflow_id))
    doc = ref.get()
    if not doc.exists:
        return None
    now = datetime.now(timezone.utc)
    ref.set({"deleted_at": now, "updated_at": now}, merge=True)
    logger.info("Marked managed n8n workflow deleted: %s", workflow_id)
    return _record_from_doc(ref.get())
