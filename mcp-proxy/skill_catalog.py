"""Skill catalog helpers for proxy-hosted Codex skills."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
import yaml

CATALOG_ROOT = Path(__file__).resolve().parent / "skills" / "catalog"


@dataclass(frozen=True)
class CatalogSkill:
    name: str
    path: Path
    title: str
    description: str
    version: Optional[str]
    default_prompt: Optional[str]
    content_sha256: str
    updated_at: Optional[str]
    files: List[str]


@dataclass(frozen=True)
class SkillArchive:
    skill: CatalogSkill
    filename: str
    media_type: str
    payload: bytes
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return {}
    raw = yaml.safe_load(parts[0][4:]) or {}
    return raw if isinstance(raw, dict) else {}


def _extract_markdown_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _skill_content_sha256(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _skill_updated_at(skill_dir: Path) -> Optional[str]:
    mtimes = [path.stat().st_mtime for path in skill_dir.rglob("*") if path.is_file()]
    if not mtimes:
        return None
    latest = max(mtimes)
    return datetime.fromtimestamp(latest, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_default_prompt(skill_dir: Path) -> Optional[str]:
    agent_path = skill_dir / "agents" / "openai.yaml"
    if not agent_path.is_file():
        return None
    raw = yaml.safe_load(agent_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return None
    interface = raw.get("interface")
    if not isinstance(interface, dict):
        return None
    prompt = interface.get("default_prompt")
    if not isinstance(prompt, str):
        return None
    cleaned = prompt.strip()
    return cleaned or None


def _skill_files(skill_dir: Path) -> List[str]:
    files: List[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file():
            files.append(path.relative_to(skill_dir).as_posix())
    return files


def _load_catalog_skill(skill_dir: Path) -> Optional[CatalogSkill]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    text = skill_md.read_text(encoding="utf-8")
    meta = _parse_frontmatter(text)
    name = str(meta.get("name") or skill_dir.name).strip() or skill_dir.name
    description = str(meta.get("description") or "").strip()
    title = _extract_markdown_title(text) or name
    return CatalogSkill(
        name=name,
        path=skill_dir,
        title=title,
        description=description,
        version=str(meta.get("version")).strip() if meta.get("version") is not None else None,
        default_prompt=_load_default_prompt(skill_dir),
        content_sha256=_skill_content_sha256(skill_dir),
        updated_at=_skill_updated_at(skill_dir),
        files=_skill_files(skill_dir),
    )


def list_catalog_skills() -> List[CatalogSkill]:
    skills: List[CatalogSkill] = []
    if not CATALOG_ROOT.is_dir():
        return skills
    for child in sorted(CATALOG_ROOT.iterdir()):
        if not child.is_dir():
            continue
        skill = _load_catalog_skill(child)
        if skill is not None:
            skills.append(skill)
    return skills


def get_catalog_skill(skill_name: str) -> Optional[CatalogSkill]:
    wanted = (skill_name or "").strip()
    if not wanted:
        return None
    for skill in list_catalog_skills():
        if skill.name == wanted:
            return skill
    return None


def _bundle_url(skill_name: str) -> str:
    rel = f"/skills/catalog/{skill_name}.tar.gz"
    base = (config.MCP_PROXY_URL or "").rstrip("/")
    if not base:
        return rel
    return f"{base}{rel}"


def _install_commands(skill_name: str, bundle_url: str) -> Dict[str, List[str]]:
    target = f"$HOME/.codex/skills/{skill_name}"
    return {
        "unix": [
            'mkdir -p "$HOME/.codex/skills"',
            f'curl -L "{bundle_url}" -o "/tmp/{skill_name}.tar.gz"',
            f'tar -xzf "/tmp/{skill_name}.tar.gz" -C /tmp',
            f'mv "/tmp/{skill_name}" "{target}"',
        ],
        "powershell": [
            'New-Item -ItemType Directory -Force -Path "$HOME/.codex/skills" | Out-Null',
            f'Invoke-WebRequest -Uri "{bundle_url}" -OutFile "$env:TEMP\\{skill_name}.tar.gz"',
            f'tar -xzf "$env:TEMP\\{skill_name}.tar.gz" -C "$env:TEMP"',
            f'Move-Item -Path "$env:TEMP\\{skill_name}" -Destination "$HOME/.codex/skills\\{skill_name}"',
        ],
    }


def skill_manifest(skill: CatalogSkill, *, include_files: bool = False) -> Dict[str, Any]:
    version = skill.version or f"sha256:{skill.content_sha256[:12]}"
    manifest: Dict[str, Any] = {
        "name": skill.name,
        "title": skill.title,
        "description": skill.description,
        "version": version,
        "default_prompt": skill.default_prompt,
        "content_sha256": skill.content_sha256,
        "updated_at": skill.updated_at,
        "install_target": f"~/.codex/skills/{skill.name}",
        "bundle_url": _bundle_url(skill.name),
        "bundle_filename": f"{skill.name}.tar.gz",
    }
    if include_files:
        manifest["files"] = list(skill.files)
    return manifest


def build_skill_archive(skill_name: str) -> Optional[SkillArchive]:
    skill = get_catalog_skill(skill_name)
    if skill is None:
        return None
    tar_payload = io.BytesIO()
    with tarfile.open(fileobj=tar_payload, mode="w", format=tarfile.PAX_FORMAT) as archive:
        root_info = tarfile.TarInfo(name=skill.name)
        root_info.type = tarfile.DIRTYPE
        root_info.mode = 0o755
        root_info.mtime = 0
        root_info.uid = 0
        root_info.gid = 0
        root_info.uname = ""
        root_info.gname = ""
        archive.addfile(root_info)

        for rel_path in skill.files:
            full_path = skill.path / rel_path
            data = full_path.read_bytes()
            info = tarfile.TarInfo(name=f"{skill.name}/{rel_path}")
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))

    gz_payload = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=gz_payload, mtime=0) as gz:
        gz.write(tar_payload.getvalue())

    raw = gz_payload.getvalue()
    return SkillArchive(
        skill=skill,
        filename=f"{skill.name}.tar.gz",
        media_type="application/gzip",
        payload=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def build_install_manifest(skill_name: str) -> Optional[Dict[str, Any]]:
    archive = build_skill_archive(skill_name)
    if archive is None:
        return None
    bundle_url = _bundle_url(skill_name)
    return {
        **skill_manifest(archive.skill, include_files=True),
        "archive_sha256": archive.sha256,
        "archive_size_bytes": archive.size_bytes,
        "restart_required": True,
        "install_notes": [
            "Ask before replacing an existing skill with the same name.",
            "Install into ~/.codex/skills/<skill-name> and restart Codex afterward.",
        ],
        "install_commands": _install_commands(skill_name, bundle_url),
    }
