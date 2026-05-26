"""Tests for managed app ↔ Cloud Run reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from managed_apps_cloud_run import cloud_run_service_exists, prune_stale_managed_apps
from models import ManagedAppRecord


def _sample_app(**overrides) -> ManagedAppRecord:
    data: dict = {
        "app_id": "owner-kpis",
        "name": "Owner",
        "summary": "KPIs",
        "data_access_summary": "Reads",
        "data_connections": [],
        "service_name": "app-owner-kpis",
        "service_url": "https://x.run.app",
        "project_id": "p1",
        "region": "europe-west1",
        "runtime_service_account": "sa@p1.iam.gserviceaccount.com",
        "status": "approved",
        "created_by_user_id": "u1",
        "created_by_email": "u@example.com",
        "created_by_kind": "human",
        "created_by_label": "u",
        "created_at": datetime.now(timezone.utc),
        "updated_at": None,
    }
    data.update(overrides)
    return ManagedAppRecord(**data)  # type: ignore[arg-type]


def test_cloud_run_service_exists_returns_none_without_token(monkeypatch):
    monkeypatch.setattr("managed_apps_cloud_run._cloud_platform_access_token", lambda: None)
    assert cloud_run_service_exists("p", "europe-west1", "svc") is None


def test_prune_marks_gone_service_deleted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MCP_PROXY_MANAGED_APPS_SKIP_CLOUD_RUN_SYNC", raising=False)
    rec = _sample_app()
    with patch("managed_apps_cloud_run.list_managed_apps", return_value=[rec]):
        with patch("managed_apps_cloud_run.cloud_run_service_exists", return_value=False):
            with patch("managed_apps_cloud_run.mark_managed_app_deleted") as mark:
                n = prune_stale_managed_apps()
    assert n == 1
    mark.assert_called_once_with("owner-kpis", delete_mode="cloud_run_only", status="deleted")


def test_prune_noop_when_service_still_there(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MCP_PROXY_MANAGED_APPS_SKIP_CLOUD_RUN_SYNC", raising=False)
    rec = _sample_app()
    with patch("managed_apps_cloud_run.list_managed_apps", return_value=[rec]):
        with patch("managed_apps_cloud_run.cloud_run_service_exists", return_value=True):
            with patch("managed_apps_cloud_run.mark_managed_app_deleted") as mark:
                n = prune_stale_managed_apps()
    assert n == 0
    mark.assert_not_called()
