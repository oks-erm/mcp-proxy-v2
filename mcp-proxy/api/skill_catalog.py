"""Public routes for browsing and downloading proxy-hosted skill bundles."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from skill_catalog import build_skill_archive, list_catalog_skills, skill_manifest

router = APIRouter(tags=["skills"])


@router.get("/skills/catalog")
def list_published_skills():
    """Return the public skill catalog served by the proxy."""
    return {"skills": [skill_manifest(skill, include_files=False) for skill in list_catalog_skills()]}


@router.get("/skills/catalog/{skill_name}.tar.gz")
def download_skill_bundle(skill_name: str):
    """Download one published skill bundle as a gzipped tar archive."""
    archive = build_skill_archive(skill_name)
    if archive is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return Response(
        content=archive.payload,
        media_type=archive.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{archive.filename}"',
            "X-Skill-Sha256": archive.sha256,
        },
    )
