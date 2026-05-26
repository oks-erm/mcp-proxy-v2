import base64
import hashlib
import io
import tarfile
from types import SimpleNamespace

import source_upload
from deploy_policy import build_plan, validate_manifest
from source_upload import finalize_upload_session, start_upload_session, upload_chunk


class FakeBlob:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def upload_from_string(self, data, content_type=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.store[self.name] = bytes(data)

    def download_as_text(self):
        return self.store[self.name].decode("utf-8")

    def download_as_bytes(self):
        return self.store[self.name]

    def exists(self):
        return self.name in self.store

    def delete(self):
        self.store.pop(self.name, None)


class FakeBucket:
    def __init__(self, store, name):
        self.store = store.setdefault(name, {})
        self.name = name

    def blob(self, name):
        return FakeBlob(self.store, name)


class FakeStorageClient:
    stores = {}

    def bucket(self, name):
        return FakeBucket(self.stores, name)

    def list_blobs(self, bucket, prefix=""):
        return [FakeBlob(bucket.store, name) for name in list(bucket.store) if name.startswith(prefix)]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def declare(path: str, data: bytes, content_type: str = "text"):
    return {"path": path, "size_bytes": len(data), "sha256": sha(data), "content_type": content_type}


def setup_fake_storage(monkeypatch):
    FakeStorageClient.stores = {}
    monkeypatch.setattr(source_upload, "storage", SimpleNamespace(Client=FakeStorageClient))


def test_start_source_upload_rejects_blocked_paths_and_secret_files(monkeypatch):
    setup_fake_storage(monkeypatch)
    dockerfile = b"FROM python:3.11-slim\n"
    package = b'{"scripts":{"start":"vite"}}'

    result = start_upload_session(
        app_id="owner-kpis",
        summary="Shows owner KPI trends.",
        files=[
            declare("Dockerfile", dockerfile),
            declare("package.json", package),
            declare("node_modules/pkg/index.js", b"bad"),
            declare(".env", b"SECRET=1"),
        ],
    )

    assert result.ok is False
    errors = "\n".join(result.payload["errors"])
    assert "node_modules" in errors
    assert "blocked secret-like filename" in errors


def test_start_source_upload_rejects_invalid_binary_asset(monkeypatch):
    setup_fake_storage(monkeypatch)

    result = start_upload_session(
        app_id="owner-kpis",
        summary="Shows owner KPI trends.",
        files=[
            declare("Dockerfile", b"FROM node:20\n"),
            declare("package.json", b"{}"),
            declare("assets/payload.bin", b"binary", "binary"),
        ],
    )

    assert result.ok is False
    assert any("binary file extension is not allowed" in e for e in result.payload["errors"])


def test_upload_source_chunk_rejects_duplicate_and_hash_mismatch(monkeypatch):
    setup_fake_storage(monkeypatch)
    dockerfile = b"FROM python:3.11-slim\n"
    requirements = b"fastapi\n"
    started = start_upload_session(
        app_id="owner-kpis",
        summary="Shows owner KPI trends.",
        files=[declare("Dockerfile", dockerfile), declare("requirements.txt", requirements)],
    )

    bad = upload_chunk(
        upload_id=started.payload["upload_id"],
        path="Dockerfile",
        chunk_index=0,
        total_chunks=1,
        content_base64=b64(dockerfile),
        sha256=sha(b"wrong"),
    )
    good = upload_chunk(
        upload_id=started.payload["upload_id"],
        path="Dockerfile",
        chunk_index=0,
        total_chunks=1,
        content_base64=b64(dockerfile),
        sha256=sha(dockerfile),
    )
    duplicate = upload_chunk(
        upload_id=started.payload["upload_id"],
        path="Dockerfile",
        chunk_index=0,
        total_chunks=1,
        content_base64=b64(dockerfile),
    )

    assert bad.ok is False
    assert "sha256 mismatch" in bad.payload["details"]
    assert good.ok is True
    assert duplicate.ok is False
    assert "duplicate" in duplicate.payload["details"]


def test_finalize_source_upload_requires_all_chunks(monkeypatch):
    setup_fake_storage(monkeypatch)
    dockerfile = b"FROM python:3.11-slim\n"
    requirements = b"fastapi\n"
    started = start_upload_session(
        app_id="owner-kpis",
        summary="Shows owner KPI trends.",
        files=[declare("Dockerfile", dockerfile), declare("requirements.txt", requirements)],
    )
    upload_chunk(
        upload_id=started.payload["upload_id"],
        path="Dockerfile",
        chunk_index=0,
        total_chunks=1,
        content_base64=b64(dockerfile),
    )

    result = finalize_upload_session(started.payload["upload_id"])

    assert result.ok is False
    assert any("missing chunk" in e for e in result.payload["errors"])


def test_finalize_source_upload_writes_archive_and_manifest(monkeypatch):
    setup_fake_storage(monkeypatch)
    dockerfile = b"FROM python:3.11-slim\n"
    requirements = b"fastapi\nuvicorn\n"
    app = b"print('hello')\n"
    started = start_upload_session(
        app_id="owner-kpis",
        name="Owner KPIs",
        summary="Shows owner KPI trends.",
        framework="fastapi",
        files=[
            declare("Dockerfile", dockerfile),
            declare("requirements.txt", requirements),
            declare("src/main.py", app),
            declare("static/logo.png", b"png", "binary"),
        ],
    )
    for path, data in {
        "Dockerfile": dockerfile,
        "requirements.txt": requirements,
        "src/main.py": app,
        "static/logo.png": b"png",
    }.items():
        uploaded = upload_chunk(
            upload_id=started.payload["upload_id"],
            path=path,
            chunk_index=0,
            total_chunks=1,
            content_base64=b64(data),
        )
        assert uploaded.ok is True

    result = finalize_upload_session(started.payload["upload_id"])

    assert result.ok is True
    manifest = result.payload["manifest"]
    assert manifest["source"]["type"] == "gcs_archive"
    assert manifest["source"]["gcs_archive"].startswith("gs://it-team-hw-project-cloud-run-deployer-staging/apps/")
    assert validate_manifest(manifest).ok is True
    assert build_plan(manifest)["ok"] is True
    bucket = FakeStorageClient.stores["it-team-hw-project-cloud-run-deployer-staging"]
    archive_name = manifest["source"]["gcs_archive"].replace("gs://it-team-hw-project-cloud-run-deployer-staging/", "")
    with tarfile.open(fileobj=io.BytesIO(bucket[archive_name]), mode="r:gz") as tar:
        assert sorted(tar.getnames()) == ["Dockerfile", "requirements.txt", "src/main.py", "static/logo.png"]
    assert not any(name.startswith(f"source_uploads/{started.payload['upload_id']}/") for name in bucket)
