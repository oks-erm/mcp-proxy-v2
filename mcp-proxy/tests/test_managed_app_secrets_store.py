"""Tests for encrypted managed app secret storage."""

from __future__ import annotations

from datetime import datetime, timezone

from conftest import sample_user
from cryptography.fernet import Fernet


class FakeDocSnapshot:
    def __init__(self, doc_id: str, data: dict | None, reference=None):
        self.id = doc_id
        self._data = data
        self.reference = reference

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return dict(self._data or {})


class FakeDocRef:
    def __init__(self, collection, doc_id: str):
        self.collection = collection
        self.doc_id = doc_id

    def get(self):
        data = self.collection.docs.get(self.doc_id)
        return FakeDocSnapshot(self.doc_id, data, self)

    def set(self, payload, merge=False):
        if merge and self.doc_id in self.collection.docs:
            self.collection.docs[self.doc_id].update(payload)
        else:
            self.collection.docs[self.doc_id] = dict(payload)

    def delete(self):
        self.collection.docs.pop(self.doc_id, None)


class FakeQuery:
    def __init__(self, collection, field, value):
        self.collection = collection
        self.field = field
        self.value = value

    def stream(self):
        for doc_id, data in list(self.collection.docs.items()):
            if data.get(self.field) == self.value:
                yield FakeDocSnapshot(doc_id, data, FakeDocRef(self.collection, doc_id))


class FakeCollection:
    def __init__(self):
        self.docs = {}

    def document(self, doc_id: str):
        return FakeDocRef(self, doc_id)

    def where(self, field, op, value):
        assert op == "=="
        return FakeQuery(self, field, value)


def test_app_secret_values_are_encrypted_and_metadata_only(monkeypatch):
    import managed_app_secrets_store as store

    collection = FakeCollection()
    monkeypatch.setenv("MCP_PROXY_APP_SECRET_ENC_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(store, "_collection", lambda: collection)
    user = sample_user(user_id="admin-1", role="admin", email="admin@example.com")

    metadata = store.set_app_secret(app_id="owner-kpis", name="api-token", value="plain-secret", user=user)

    raw_doc = next(iter(collection.docs.values()))
    assert metadata.name == "API_TOKEN"
    assert raw_doc["ciphertext"] != "plain-secret"
    assert "plain-secret" not in str(raw_doc)
    assert store.get_app_secret_value(app_id="owner-kpis", name="API_TOKEN") == "plain-secret"
    listed = store.list_app_secrets("owner-kpis")
    assert listed[0].name == "API_TOKEN"
    assert not hasattr(listed[0], "ciphertext")


def test_delete_app_secrets_removes_all_docs(monkeypatch):
    import managed_app_secrets_store as store

    collection = FakeCollection()
    now = datetime.now(timezone.utc)
    collection.docs = {
        "owner-kpis__A": {"app_id": "owner-kpis", "name": "A", "created_at": now, "updated_at": now},
        "owner-kpis__B": {"app_id": "owner-kpis", "name": "B", "created_at": now, "updated_at": now},
        "other__A": {"app_id": "other", "name": "A", "created_at": now, "updated_at": now},
    }
    monkeypatch.setattr(store, "_collection", lambda: collection)

    assert store.delete_app_secrets("owner-kpis") == 2
    assert sorted(collection.docs) == ["other__A"]
