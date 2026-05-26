"""Encrypted Firestore storage for Host Wise managed app secrets."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
import gcp_firestore
from cryptography.fernet import Fernet, InvalidToken
from google.cloud.firestore import DocumentSnapshot
from users.schemas import UserInDB

logger = logging.getLogger(__name__)

SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
KEY_VERSION = "v1"


@dataclass(frozen=True)
class ManagedAppSecretMetadata:
    app_id: str
    name: str
    key_version: str = KEY_VERSION
    created_by_user_id: str = ""
    updated_by_user_id: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def _get_db():
    return gcp_firestore.get_client()


def _collection():
    collection_name = os.getenv("MCP_PROXY_MANAGED_APP_SECRETS_COLLECTION", "managed_app_secrets")
    return _get_db().collection(collection_name)


def normalize_secret_name(name: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]", "_", str(name or "").strip().upper()).strip("_")
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized or not SECRET_NAME_RE.match(normalized):
        raise ValueError("secret name must start with a letter and contain only A-Z, 0-9, and _")
    return normalized


def _doc_id(app_id: str, name: str) -> str:
    return f"{str(app_id).strip()}__{normalize_secret_name(name)}"


def _fernet() -> Fernet:
    explicit = (getattr(config, "MCP_PROXY_APP_SECRET_ENC_KEY", "") or "").strip()
    if explicit:
        return Fernet(explicit.encode("ascii"))
    raise RuntimeError("MCP_PROXY_APP_SECRET_ENC_KEY is required to store managed app secrets")


def encrypt_app_secret_value(value: str) -> str:
    return _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt_app_secret_value(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(str(ciphertext).encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise ValueError("Invalid encrypted app secret") from exc


def _metadata_from_doc(doc: DocumentSnapshot) -> ManagedAppSecretMetadata:
    data = doc.to_dict() or {}
    return ManagedAppSecretMetadata(
        app_id=str(data.get("app_id") or ""),
        name=str(data.get("name") or ""),
        key_version=str(data.get("key_version") or KEY_VERSION),
        created_by_user_id=str(data.get("created_by_user_id") or ""),
        updated_by_user_id=str(data.get("updated_by_user_id") or ""),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def set_app_secret(*, app_id: str, name: str, value: str, user: UserInDB) -> ManagedAppSecretMetadata:
    app = str(app_id or "").strip()
    if not app:
        raise ValueError("app_id is required")
    secret_name = normalize_secret_name(name)
    now = datetime.now(timezone.utc)
    ref = _collection().document(_doc_id(app, secret_name))
    existing = ref.get()
    payload: Dict[str, Any] = {
        "app_id": app,
        "name": secret_name,
        "ciphertext": encrypt_app_secret_value(value),
        "key_version": KEY_VERSION,
        "updated_by_user_id": user.id,
        "updated_at": now,
    }
    if existing.exists:
        current = existing.to_dict() or {}
        payload["created_by_user_id"] = current.get("created_by_user_id") or user.id
        payload["created_at"] = current.get("created_at") or now
        ref.set(payload, merge=True)
    else:
        payload["created_by_user_id"] = user.id
        payload["created_at"] = now
        ref.set(payload)
    logger.info("Stored encrypted managed app secret metadata: app_id=%s name=%s", app, secret_name)
    return _metadata_from_doc(ref.get())


def list_app_secrets(app_id: str) -> List[ManagedAppSecretMetadata]:
    app = str(app_id or "").strip()
    if not app:
        raise ValueError("app_id is required")
    docs = [_metadata_from_doc(doc) for doc in _collection().where("app_id", "==", app).stream()]
    docs.sort(key=lambda item: item.name)
    return docs


def delete_app_secret(*, app_id: str, name: str) -> bool:
    app = str(app_id or "").strip()
    if not app:
        raise ValueError("app_id is required")
    ref = _collection().document(_doc_id(app, name))
    existed = ref.get().exists
    ref.delete()
    logger.info(
        "Deleted managed app secret metadata: app_id=%s name=%s existed=%s", app, normalize_secret_name(name), existed
    )
    return existed


def delete_app_secrets(app_id: str) -> int:
    app = str(app_id or "").strip()
    if not app:
        return 0
    count = 0
    for doc in _collection().where("app_id", "==", app).stream():
        doc.reference.delete()
        count += 1
    if count:
        logger.info("Deleted %s managed app secret(s) for app_id=%s", count, app)
    return count


def get_app_secret_value(*, app_id: str, name: str) -> Optional[str]:
    app = str(app_id or "").strip()
    if not app:
        raise ValueError("app_id is required")
    doc = _collection().document(_doc_id(app, name)).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    ciphertext = str(data.get("ciphertext") or "")
    if not ciphertext:
        return None
    return decrypt_app_secret_value(ciphertext)
