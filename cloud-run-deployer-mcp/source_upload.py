"""Resumable MCP source uploads for Cloud Run deployer apps."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import math
import os
import posixpath
import re
import tarfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

from deploy_policy import validate_manifest

try:
    from google.cloud import storage
except ImportError:  # pragma: no cover - production image installs google-cloud-storage.
    storage = None

CHUNK_SIZE_BYTES = 2 * 1024 * 1024
MAX_CHUNK_BASE64_BYTES = 3 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_COMPRESSED_BYTES = 100 * 1024 * 1024
MAX_BINARY_FILE_BYTES = 10 * 1024 * 1024

ALLOWED_BINARY_EXTENSIONS = {
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".otf",
    ".png",
    ".ttf",
    ".webp",
    ".woff",
    ".woff2",
}
ALLOWED_TEXT_EXTENSIONS = {
    "",
    ".cjs",
    ".conf",
    ".css",
    ".csv",
    ".dockerignore",
    ".env.example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".lock",
    ".md",
    ".mjs",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
BLOCKED_PATH_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}
BLOCKED_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    "credentials.json",
    "service-account.json",
}
BUILD_MARKERS = {"hostwise.app.json", "package.json", "pyproject.toml", "requirements.txt"}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class UploadResult:
    ok: bool
    payload: dict[str, Any]


def start_upload_session(
    *,
    app_id: str,
    name: str = "",
    summary: str = "",
    framework: str = "auto",
    files: list[dict[str, Any]] | None = None,
    data: dict[str, Any] | None = None,
    access: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    data_connections: list[dict[str, Any]] | None = None,
    project_id: str = "",
    region: str = "",
    bucket: str = "",
) -> UploadResult:
    declared_files, errors = _validate_declared_files(files or [])
    manifest = _manifest_draft(
        app_id=app_id,
        name=name,
        summary=summary,
        framework=framework,
        data=data,
        access=access,
        runtime=runtime,
        data_connections=data_connections,
        source={"type": "image", "image": "placeholder"},
        project_id=project_id,
        region=region,
    )
    manifest_validation = validate_manifest(manifest)
    errors.extend(manifest_validation.errors)
    if errors:
        return UploadResult(False, {"error": "validation_error", "details": "source upload rejected", "errors": errors})

    normalized = manifest_validation.normalized
    upload_id = f"{normalized['app_id']}-{uuid.uuid4().hex[:16]}"
    target_bucket = _source_bucket(normalized["project_id"], bucket)
    prefix = f"source_uploads/{upload_id}"
    session = {
        "upload_id": upload_id,
        "app_id": normalized["app_id"],
        "created_at": int(time.time()),
        "bucket": target_bucket,
        "prefix": prefix,
        "manifest": {
            **normalized,
            "source": {"type": "gcs_archive", "image": "", "gcs_archive": "", "path": ""},
        },
        "files": declared_files,
        "chunk_size_bytes": CHUNK_SIZE_BYTES,
        "max_chunk_base64_bytes": MAX_CHUNK_BASE64_BYTES,
    }
    _blob(target_bucket, f"{prefix}/session.json").upload_from_string(
        json.dumps(session, sort_keys=True), content_type="application/json"
    )
    return UploadResult(
        True,
        {
            "ok": True,
            "upload_id": upload_id,
            "bucket": target_bucket,
            "prefix": prefix,
            "chunk_size_bytes": CHUNK_SIZE_BYTES,
            "max_chunk_base64_bytes": MAX_CHUNK_BASE64_BYTES,
            "max_uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
            "max_compressed_bytes": MAX_COMPRESSED_BYTES,
            "files": [
                {
                    "path": f["path"],
                    "size_bytes": f["size_bytes"],
                    "sha256": f["sha256"],
                    "content_type": f["content_type"],
                    "total_chunks": _expected_chunks(f["size_bytes"]),
                }
                for f in declared_files
            ],
            "next_steps": [
                "Call upload_source_chunk for every declared file chunk.",
                "Then call finalize_source_upload.",
            ],
        },
    )


def upload_chunk(
    *,
    upload_id: str,
    path: str,
    chunk_index: int,
    total_chunks: int,
    content_base64: str,
    sha256: str = "",
) -> UploadResult:
    session = _load_session(upload_id)
    if not session:
        return UploadResult(False, {"error": "not_found", "details": "upload session not found"})
    file_meta = _file_meta(session, path)
    if not file_meta:
        return UploadResult(False, {"error": "validation_error", "details": "path was not declared for this upload"})
    try:
        chunk_num = int(chunk_index)
        total_num = int(total_chunks)
    except (TypeError, ValueError):
        return UploadResult(
            False, {"error": "validation_error", "details": "chunk_index and total_chunks must be integers"}
        )
    expected_chunks = _expected_chunks(file_meta["size_bytes"])
    if total_num != expected_chunks:
        return UploadResult(
            False,
            {
                "error": "validation_error",
                "details": f"total_chunks must be {expected_chunks} for {path}",
                "expected_total_chunks": expected_chunks,
            },
        )
    if chunk_num < 0 or chunk_num >= expected_chunks:
        return UploadResult(False, {"error": "validation_error", "details": "chunk_index out of range"})
    if len(content_base64.encode("utf-8")) > MAX_CHUNK_BASE64_BYTES:
        return UploadResult(False, {"error": "validation_error", "details": "chunk payload exceeds base64 limit"})
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except Exception:
        return UploadResult(False, {"error": "validation_error", "details": "content_base64 is not valid base64"})
    if len(raw) > CHUNK_SIZE_BYTES:
        return UploadResult(False, {"error": "validation_error", "details": "decoded chunk exceeds size limit"})
    if sha256 and _clean_sha(sha256) != hashlib.sha256(raw).hexdigest():
        return UploadResult(False, {"error": "validation_error", "details": "chunk sha256 mismatch"})
    chunk_blob = _blob(session["bucket"], _chunk_name(session, path, chunk_num))
    if chunk_blob.exists():
        return UploadResult(False, {"error": "validation_error", "details": "duplicate chunk"})
    chunk_blob.upload_from_string(raw, content_type="application/octet-stream")
    return UploadResult(
        True,
        {
            "ok": True,
            "upload_id": upload_id,
            "path": path,
            "chunk_index": chunk_num,
            "total_chunks": expected_chunks,
            "received_bytes": len(raw),
        },
    )


def finalize_upload_session(upload_id: str, *, execute: bool = True) -> UploadResult:
    session = _load_session(upload_id)
    if not session:
        return UploadResult(False, {"error": "not_found", "details": "upload session not found"})
    if not execute:
        return UploadResult(
            True,
            {
                "ok": True,
                "executed": False,
                "dry_run": True,
                "upload_id": upload_id,
                "expected_files": len(session["files"]),
                "manifest": _manifest_with_archive(session, ""),
            },
        )

    files: list[tuple[dict[str, Any], bytes]] = []
    errors: list[str] = []
    for file_meta in session["files"]:
        path = file_meta["path"]
        chunks: list[bytes] = []
        for idx in range(_expected_chunks(file_meta["size_bytes"])):
            chunk = _blob(session["bucket"], _chunk_name(session, path, idx))
            if not chunk.exists():
                errors.append(f"missing chunk {idx} for {path}")
                continue
            chunks.append(chunk.download_as_bytes())
        if errors:
            continue
        data = b"".join(chunks)
        if len(data) != file_meta["size_bytes"]:
            errors.append(f"size mismatch for {path}")
        if hashlib.sha256(data).hexdigest() != file_meta["sha256"]:
            errors.append(f"sha256 mismatch for {path}")
        files.append((file_meta, data))
    if errors:
        return UploadResult(
            False, {"error": "validation_error", "details": "source upload incomplete", "errors": errors}
        )

    archive_bytes = _build_archive(files)
    if len(archive_bytes) > MAX_COMPRESSED_BYTES:
        return UploadResult(False, {"error": "validation_error", "details": "compressed archive exceeds 100 MB"})
    gcs_archive = f"gs://{session['bucket']}/apps/{session['app_id']}/source-{int(time.time())}.tgz"
    archive_name = gcs_archive.replace(f"gs://{session['bucket']}/", "", 1)
    _blob(session["bucket"], archive_name).upload_from_string(archive_bytes, content_type="application/gzip")
    _delete_session_objects(session)
    manifest = _manifest_with_archive(session, gcs_archive)
    return UploadResult(
        True,
        {
            "ok": True,
            "executed": True,
            "upload_id": upload_id,
            "gcs_archive": gcs_archive,
            "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "archive_size_bytes": len(archive_bytes),
            "manifest": manifest,
            "next_steps": ["Call validate_deployment_manifest, plan_deployment, and deploy_app with this manifest."],
        },
    )


def _validate_declared_files(files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_size = 0
    paths: set[str] = set()
    for idx, item in enumerate(files):
        if not isinstance(item, dict):
            errors.append(f"files[{idx}] must be an object")
            continue
        path = _clean_path(item.get("path"))
        size = _coerce_int(item.get("size_bytes"), -1)
        sha = _clean_sha(item.get("sha256"))
        content_type = str(item.get("content_type") or "").strip().lower()
        path_errors = _validate_path(path, content_type, size)
        errors.extend(path_errors)
        if not sha:
            errors.append(f"{path or f'files[{idx}]'} sha256 must be a lowercase hex SHA-256")
        if content_type not in {"text", "binary"}:
            errors.append(f"{path or f'files[{idx}]'} content_type must be text or binary")
        if path in seen:
            errors.append(f"duplicate file path: {path}")
        seen.add(path)
        total_size += max(size, 0)
        paths.add(posixpath.basename(path))
        out.append({"path": path, "size_bytes": size, "sha256": sha, "content_type": content_type})
    if not out:
        errors.append("files must declare at least one source file")
    if total_size > MAX_UNCOMPRESSED_BYTES:
        errors.append("declared source exceeds 250 MB uncompressed")
    if "Dockerfile" not in paths:
        errors.append("source must include a Dockerfile")
    if not BUILD_MARKERS.intersection(paths):
        errors.append("source must include package.json, requirements.txt, pyproject.toml, or hostwise.app.json")
    return out, errors


def _validate_path(path: str, content_type: str, size: int) -> list[str]:
    errors: list[str] = []
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        errors.append(f"invalid relative POSIX path: {path!r}")
        return errors
    normalized = posixpath.normpath(path)
    if normalized != path or path.startswith("../") or "/../" in path or path == "..":
        errors.append(f"path traversal is not allowed: {path}")
    parts = path.split("/")
    if any(part in BLOCKED_PATH_PARTS for part in parts):
        errors.append(f"blocked source directory in path: {path}")
    if posixpath.basename(path) in BLOCKED_FILENAMES:
        errors.append(f"blocked secret-like filename: {path}")
    if size < 0:
        errors.append(f"size_bytes must be non-negative for {path}")
    filename = posixpath.basename(path)
    ext = posixpath.splitext(path)[1].lower()
    if content_type == "binary":
        if ext not in ALLOWED_BINARY_EXTENSIONS:
            errors.append(f"binary file extension is not allowed: {path}")
        if size > MAX_BINARY_FILE_BYTES:
            errors.append(f"binary file exceeds 10 MB limit: {path}")
    elif content_type == "text":
        if ext not in ALLOWED_TEXT_EXTENSIONS and filename not in ALLOWED_TEXT_EXTENSIONS:
            errors.append(f"text file extension is not allowed: {path}")
    return errors


def _manifest_draft(**kwargs: Any) -> dict[str, Any]:
    manifest = {
        "app_id": kwargs["app_id"],
        "name": kwargs["name"] or kwargs["app_id"],
        "summary": kwargs["summary"],
        "framework": kwargs["framework"] or "auto",
        "source": kwargs["source"],
        "data": kwargs["data"] or {"sqlite": True, "uploads": True},
        "access": kwargs["access"] or {"mode": "iap"},
        "runtime": kwargs["runtime"] or {"max_instances": 1},
        "data_connections": kwargs["data_connections"] or [],
    }
    if kwargs.get("project_id"):
        manifest["project_id"] = kwargs["project_id"]
    if kwargs.get("region"):
        manifest["region"] = kwargs["region"]
    return manifest


def _manifest_with_archive(session: dict[str, Any], gcs_archive: str) -> dict[str, Any]:
    manifest = dict(session["manifest"])
    manifest["source"] = {"type": "gcs_archive", "image": "", "gcs_archive": gcs_archive, "path": ""}
    return manifest


def _build_archive(files: list[tuple[dict[str, Any], bytes]]) -> bytes:
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for file_meta, data in sorted(files, key=lambda item: item[0]["path"]):
                info = tarfile.TarInfo(file_meta["path"])
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _load_session(upload_id: str) -> dict[str, Any] | None:
    if not upload_id or "/" in upload_id or ".." in upload_id:
        return None
    prefix = f"source_uploads/{upload_id}"
    bucket = _bucket_from_upload_id(upload_id)
    session_blob = _blob(bucket, f"{prefix}/session.json")
    if not session_blob.exists():
        return None
    return json.loads(session_blob.download_as_text())


def _bucket_from_upload_id(upload_id: str) -> str:
    project = os.getenv("GCP_PROJECT_ID", "it-team-hw-project")
    return _source_bucket(project, "")


def _file_meta(session: dict[str, Any], path: str) -> dict[str, Any] | None:
    clean = _clean_path(path)
    for item in session.get("files") or []:
        if item.get("path") == clean:
            return item
    return None


def _chunk_name(session: dict[str, Any], path: str, chunk_index: int) -> str:
    return f"{session['prefix']}/chunks/{path}.part{chunk_index:06d}"


def _delete_session_objects(session: dict[str, Any]) -> None:
    client = _storage_client()
    bucket = client.bucket(session["bucket"])
    for blob in client.list_blobs(bucket, prefix=f"{session['prefix']}/"):
        blob.delete()


def _blob(bucket_name: str, name: str):
    return _storage_client().bucket(bucket_name).blob(name)


def _storage_client():
    if storage is None:
        raise RuntimeError("google-cloud-storage is required for MCP source uploads")
    return storage.Client()


def _source_bucket(project_id: str, bucket: str) -> str:
    value = bucket or os.getenv("CLOUD_RUN_DEPLOYER_SOURCE_ARCHIVE_BUCKET", f"{project_id}-cloud-run-deployer-staging")
    return str(value).replace("gs://", "").strip("/")


def _expected_chunks(size_bytes: int) -> int:
    if size_bytes <= 0:
        return 1
    return int(math.ceil(size_bytes / CHUNK_SIZE_BYTES))


def _clean_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def _clean_sha(value: Any) -> str:
    sha = str(value or "").strip().lower()
    return sha if SHA256_RE.match(sha) else ""


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
