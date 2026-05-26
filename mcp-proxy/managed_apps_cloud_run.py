"""Prune managed_apps Firestore rows when the Cloud Run service no longer exists."""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from google.auth import default
from google.auth.transport.requests import Request
from managed_apps_store import list_managed_apps, mark_managed_app_deleted

logger = logging.getLogger(__name__)

_RUN_V2 = "https://run.googleapis.com/v2"


def _sync_disabled() -> bool:
    """Set MCP_PROXY_MANAGED_APPS_SKIP_CLOUD_RUN_SYNC=1 to skip list-time probes (e.g. local tests)."""
    return os.getenv("MCP_PROXY_MANAGED_APPS_SKIP_CLOUD_RUN_SYNC", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _cloud_platform_access_token() -> Optional[str]:
    """OAuth2 access token for Cloud Run Admin API (run.services.get on the project)."""
    try:
        creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())  # type: ignore[union-attr]
        if getattr(creds, "token", None):
            return str(creds.token)
    except Exception as e:
        logger.info("No GCP access token for managed app Cloud Run sync: %s", e)
    return None


def cloud_run_service_exists(project_id: str, region: str, service_name: str) -> Optional[bool]:
    """
    True = service present, False = 404, None = could not determine (keep Firestore as-is).
    """
    if not project_id or not region or not service_name:
        return None
    token = _cloud_platform_access_token()
    if not token:
        return None
    name = f"projects/{project_id}/locations/{region}/services/{service_name}"
    url = f"{_RUN_V2}/{name}"
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        logger.warning("Cloud Run GET failed for %s: %s", name, e)
        return None
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    logger.info(
        "Cloud Run service probe inconclusive: %s status=%s body=%s",
        name,
        r.status_code,
        (r.text or "")[:300],
    )
    return None


def prune_stale_managed_apps() -> int:
    """
    For every non-deleted managed app, if the Cloud Run service returns 404, mark the record deleted.

    Returns the number of records pruned. Never raises (failures are logged, records kept).
    """
    if _sync_disabled():
        return 0
    pruned = 0
    try:
        active = [a for a in list_managed_apps(include_deleted=True) if a.status != "deleted" and a.deleted_at is None]
    except Exception as e:
        logger.warning("prune_stale_managed_apps: could not list apps: %s", e)
        return 0
    for app in active:
        if not (app.service_name and app.project_id and app.region):
            continue
        try:
            exists = cloud_run_service_exists(app.project_id, app.region, app.service_name)
        except Exception as e:
            logger.debug("prune: skip %s: %s", app.app_id, e)
            continue
        if exists is False:
            mark_managed_app_deleted(app.app_id, delete_mode="cloud_run_only", status="deleted")
            pruned += 1
            logger.info(
                "Pruned managed app %s: Cloud Run service %s is gone",
                app.app_id,
                app.service_name,
            )
    return pruned
