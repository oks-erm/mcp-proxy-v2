"""Firestore persistence for dashboard apps deployed through the MCP proxy."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import gcp_firestore
from google.cloud.firestore import DocumentSnapshot
from models import ManagedAppRecord, ManagedAppStatus
from users.schemas import UserInDB

logger = logging.getLogger(__name__)


def _get_db():
    return gcp_firestore.get_client()


def _collection():
    collection_name = os.getenv("MCP_PROXY_MANAGED_APPS_COLLECTION", "managed_apps")
    return _get_db().collection(collection_name)


def _user_label(user: UserInDB) -> str:
    if user.kind == "agent" and user.agent_name:
        return user.agent_name
    if user.email:
        return user.email
    return user.id


def _record_from_doc(doc: DocumentSnapshot) -> ManagedAppRecord:
    data = doc.to_dict() or {}
    kind = data.get("created_by_kind", "human")
    if kind not in ("human", "agent"):
        kind = "human"
    status = data.get("status", "pending_review")
    if status not in ("pending_review", "approved", "deleted", "delete_failed"):
        status = "pending_review"
    delete_mode = data.get("delete_mode")
    if delete_mode not in ("cloud_run_only", "full_cleanup"):
        delete_mode = None
    return ManagedAppRecord(
        app_id=doc.id,
        name=data.get("name", ""),
        summary=data.get("summary", ""),
        data_access_summary=data.get("data_access_summary", ""),
        data_connections=data.get("data_connections") or [],
        service_name=data.get("service_name", ""),
        service_url=data.get("service_url"),
        project_id=data.get("project_id", ""),
        region=data.get("region", ""),
        runtime_service_account=data.get("runtime_service_account", ""),
        framework=data.get("framework", ""),
        repo_url=data.get("repo_url", ""),
        approved_url=data.get("approved_url"),
        version=data.get("version", ""),
        commit_sha=data.get("commit_sha", ""),
        build_id=data.get("build_id", ""),
        image_digest=data.get("image_digest", ""),
        cloud_run_revision=data.get("cloud_run_revision", ""),
        status=status,
        created_by_user_id=data.get("created_by_user_id", ""),
        created_by_email=data.get("created_by_email", ""),
        created_by_kind=kind,
        created_by_label=data.get("created_by_label", ""),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        approved_by=data.get("approved_by"),
        approved_at=data.get("approved_at"),
        deleted_at=data.get("deleted_at"),
        delete_mode=delete_mode,
    )


def upsert_managed_app(
    *,
    app_id: str,
    name: str,
    summary: str,
    data_access_summary: str,
    data_connections: list[dict[str, Any]],
    service_name: str,
    service_url: Optional[str],
    project_id: str,
    region: str,
    runtime_service_account: str = "",
    framework: str = "",
    repo_url: str = "",
    approved_url: Optional[str] = None,
    version: str = "",
    commit_sha: str = "",
    build_id: str = "",
    image_digest: str = "",
    cloud_run_revision: str = "",
    user: UserInDB,
) -> ManagedAppRecord:
    """Create or refresh a managed app record after a successful deploy."""
    now = datetime.now(timezone.utc)
    ref = _collection().document(str(app_id))
    existing = ref.get()
    data: Dict[str, Any] = {
        "name": name,
        "summary": summary,
        "data_access_summary": data_access_summary,
        "data_connections": data_connections or [],
        "service_name": service_name,
        "service_url": service_url,
        "project_id": project_id,
        "region": region,
        "runtime_service_account": runtime_service_account or "",
        "framework": framework or "",
        "repo_url": repo_url or "",
        "approved_url": (approved_url or "").strip() or service_url,
        "version": version or "",
        "commit_sha": commit_sha or "",
        "build_id": build_id or "",
        "image_digest": image_digest or "",
        "cloud_run_revision": cloud_run_revision or "",
        "status": "approved",
        "created_by_user_id": user.id,
        "created_by_email": user.email or "",
        "created_by_kind": user.kind,
        "created_by_label": _user_label(user),
        "updated_at": now,
        "approved_by": user.id,
        "approved_at": now,
        "deleted_at": None,
        "delete_mode": None,
    }
    if existing.exists:
        current = existing.to_dict() or {}
        data["created_at"] = current.get("created_at") or now
        if current.get("created_by_user_id"):
            data["created_by_user_id"] = current.get("created_by_user_id", user.id)
            data["created_by_email"] = current.get("created_by_email", user.email or "")
            data["created_by_kind"] = current.get("created_by_kind", user.kind)
            data["created_by_label"] = current.get("created_by_label", _user_label(user))
        if not service_url and not (approved_url or "").strip():
            if current.get("service_url") or current.get("approved_url"):
                data["service_url"] = current.get("service_url")
                data["approved_url"] = current.get("approved_url") or current.get("service_url")
        ref.set(data, merge=True)
    else:
        data["created_at"] = now
        ref.set(data)
    logger.info("Upserted managed app record: %s", app_id)
    return _record_from_doc(ref.get())


def list_managed_apps(
    *, include_deleted: bool = False, creator_user_id: Optional[str] = None
) -> List[ManagedAppRecord]:
    """Return managed apps sorted newest-first."""
    docs = [_record_from_doc(doc) for doc in _collection().stream()]
    if creator_user_id:
        docs = [item for item in docs if item.created_by_user_id == creator_user_id]
    if not include_deleted:
        docs = [item for item in docs if item.status != "deleted" and item.deleted_at is None]
    docs.sort(key=lambda item: (item.created_at or datetime.min.replace(tzinfo=timezone.utc)).isoformat(), reverse=True)
    return docs


def get_managed_app(app_id: str) -> Optional[ManagedAppRecord]:
    """Fetch one managed app by app id."""
    doc = _collection().document(str(app_id)).get()
    if not doc.exists:
        return None
    return _record_from_doc(doc)


def approve_managed_app(
    app_id: str, *, reviewed_by: str, approved_url: Optional[str] = None
) -> Optional[ManagedAppRecord]:
    """Approve a pending managed app."""
    ref = _collection().document(str(app_id))
    doc = ref.get()
    if not doc.exists:
        return None
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {"status": "approved", "approved_by": reviewed_by, "approved_at": now, "updated_at": now}
    if approved_url:
        payload["approved_url"] = approved_url
        payload["service_url"] = approved_url
    ref.set(payload, merge=True)
    logger.info("Approved managed app: %s", app_id)
    return _record_from_doc(ref.get())


def mark_managed_app_deleted(
    app_id: str, *, delete_mode: str, status: ManagedAppStatus = "deleted"
) -> Optional[ManagedAppRecord]:
    """Mark a managed app deleted or delete_failed after an upstream delete attempt."""
    ref = _collection().document(str(app_id))
    doc = ref.get()
    if not doc.exists:
        return None
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "status": status,
        "updated_at": now,
        "delete_mode": delete_mode if delete_mode in ("cloud_run_only", "full_cleanup") else None,
    }
    if status == "deleted":
        payload["deleted_at"] = now
    ref.set(payload, merge=True)
    logger.info("Marked managed app %s as %s", app_id, status)
    return _record_from_doc(ref.get())
