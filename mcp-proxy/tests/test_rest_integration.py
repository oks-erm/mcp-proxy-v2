"""REST integration tests (FastAPI stack, mocked stores and upstreams)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import respx
from conftest import make_access_token, sample_user
from fastapi import HTTPException
from improvement_requests_schemas import ImprovementRequestInDB
from models import ManagedAppRecord, ManagedWorkflowRecord, ServerConfig
from permissions.models import PermissionItem
from skill_update_requests_schemas import SkillUpdateRequestInDB
from users.schemas import (
    AccessRequestInDB,
    UserInDB,
    UserUsageLeaderboard,
    UserUsageSummary,
)
from users.usage_summary import UserUsageSummaryUnavailableError

# jwt_handler binds get_user_by_id at import time — patch auth.jwt_handler.get_user_by_id for JWT.
# /users/* also uses users.store.get_user_by_id — patch both with the same side_effect when needed.


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_public_skill_catalog_ok(client):
    r = client.get("/skills/catalog")
    assert r.status_code == 200
    payload = r.json()
    assert any(skill["name"] == "host-wise-n8n-workflows" for skill in payload["skills"])
    assert any(skill["name"] == "host-wise-managed-apps" for skill in payload["skills"])


def test_public_skill_bundle_download_ok(client):
    r = client.get("/skills/catalog/host-wise-n8n-workflows.tar.gz")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert r.headers["x-skill-sha256"]
    assert len(r.content) > 0


def test_public_managed_apps_skill_bundle_download_ok(client):
    r = client.get("/skills/catalog/host-wise-managed-apps.tar.gz")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert r.headers["x-skill-sha256"]
    assert len(r.content) > 0


def test_public_skill_bundle_download_is_deterministic(client):
    r1 = client.get("/skills/catalog/host-wise-n8n-workflows.tar.gz")
    r2 = client.get("/skills/catalog/host-wise-n8n-workflows.tar.gz")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.content == r2.content
    assert r1.headers["x-skill-sha256"] == r2.headers["x-skill-sha256"]


def test_auth_google_invalid_token(client):
    with patch("api.auth_routes.verify_google_token", side_effect=ValueError("bad")):
        r = client.post("/auth/login/google", json={"credential": "x"})
    assert r.status_code == 401


def test_auth_google_missing_email(client):
    with patch("api.auth_routes.verify_google_token", return_value={"sub": "s1"}):
        r = client.post("/auth/login/google", json={"credential": "x"})
    assert r.status_code == 400


def test_auth_google_pending(client):
    u = sample_user(user_id="new-1", status="pending")
    with patch("api.auth_routes.verify_google_token", return_value={"email": "n@e.com", "sub": "s-new"}):
        with patch("api.auth_routes.ensure_user_and_request", return_value=(u, MagicMock(), True)):
            r = client.post("/auth/login/google", json={"credential": "tok"})
    assert r.status_code == 200
    assert r.json()["status"] == "waiting_for_approval"


def test_auth_google_rejected(client):
    u = sample_user(user_id="rej-1", status="rejected")
    with patch("api.auth_routes.verify_google_token", return_value={"email": "r@e.com", "sub": "s-r"}):
        with patch("api.auth_routes.ensure_user_and_request", return_value=(u, None, False)):
            r = client.post("/auth/login/google", json={"credential": "tok"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


def test_auth_google_active_tokens(client):
    u = sample_user(user_id="act-1", status="active")
    with patch("api.auth_routes.verify_google_token", return_value={"email": "a@e.com", "sub": "s-a"}):
        with patch("api.auth_routes.ensure_user_and_request", return_value=(u, None, False)):
            r = client.post("/auth/login/google", json={"credential": "tok"})
    assert r.status_code == 200
    body = r.json()
    assert "tokens" in body
    assert "access" in body["tokens"]
    assert "refresh" not in body["tokens"]
    assert r.cookies.get("mcp_refresh")
    assert r.cookies.get("mcp_csrf")


def test_auth_config_public(client):
    r = client.get("/auth/config")
    assert r.status_code == 200
    data = r.json()
    assert "google_oauth_client_id" in data
    assert "gcp_project_id" in data


def test_auth_refresh_returns_access(client):
    u = sample_user(user_id="act-1", status="active")
    with patch("api.auth_routes.verify_google_token", return_value={"email": "a@e.com", "sub": "s-a"}):
        with patch("api.auth_routes.ensure_user_and_request", return_value=(u, None, False)):
            r = client.post("/auth/login/google", json={"credential": "tok"})
    assert r.status_code == 200
    csrf = client.cookies.get("mcp_csrf")
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r2 = client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 200
    assert "access" in r2.json()


def test_admin_audit_log_explorer(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        r = client.get("/admin/audit/log-explorer", headers=auth_headers_admin)
    assert r.status_code == 200
    assert "console.cloud.google.com" in r.json()["url"]


def test_admin_metrics_ok(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        r = client.get("/admin/metrics", headers=auth_headers_admin)
    assert r.status_code == 200
    data = r.json()
    assert "process_id" in data
    assert "boot_id" in data
    assert data["counters"]["mcp_denied_missing_key"] >= 0
    assert data["counters"]["mcp_requests_authorized"] >= 0
    assert isinstance(data["by_method"], dict)


def test_admin_metrics_forbidden_power_user(client, auth_headers_power_user):
    u = sample_user(user_id="pu-1", role="power_user", email="pu@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/admin/metrics", headers=auth_headers_power_user)
    assert r.status_code == 403


def test_admin_managed_workflows_list(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    record = ManagedWorkflowRecord(
        workflow_id="wf-1",
        name="Demo workflow",
        summary="Short summary",
        editor_url="https://n8n.example.com/workflow/wf-1",
        created_by_user_id="user-1",
        created_by_email="user@example.com",
        created_by_kind="human",
        created_by_label="user@example.com",
        created_at=datetime.now(timezone.utc),
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.list_managed_workflows", return_value=[record]):
            r = client.get("/admin/n8n-workflows", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()[0]["workflow_id"] == "wf-1"


@respx.mock
def test_admin_managed_workflow_delete(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    record = ManagedWorkflowRecord(
        workflow_id="wf-1",
        name="Demo workflow",
        summary="Short summary",
        editor_url="https://n8n.example.com/workflow/wf-1",
        created_by_user_id="user-1",
        created_by_email="user@example.com",
        created_by_kind="human",
        created_by_label="user@example.com",
        created_at=datetime.now(timezone.utc),
    )
    cfg = ServerConfig(id="n8n", url="https://upstream-n8n.test/mcp", enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "n8n"}},
    }
    delete_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"structuredContent": {"workflow_id": "wf-1", "deleted": True}},
    }
    respx.post(cfg.url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}),
            httpx.Response(200, json=delete_body),
        ]
    )
    deleted_record = record.model_copy(update={"deleted_at": datetime.now(timezone.utc)})
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_managed_workflow", return_value=record):
            with patch("rest_api.get_server", return_value=cfg):
                with patch("rest_api.mark_managed_workflow_deleted", return_value=deleted_record):
                    with patch("rest_api.resolve_upstream_headers", new=AsyncMock(return_value={})):
                        r = client.delete("/admin/n8n-workflows/wf-1", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["workflow_id"] == "wf-1"
    assert r.json()["already_missing_upstream"] is False


def test_admin_managed_workflow_delete_cleans_up_when_upstream_workflow_already_missing(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    record = ManagedWorkflowRecord(
        workflow_id="wf-1",
        name="Demo workflow",
        summary="Short summary",
        editor_url="https://n8n.example.com/workflow/wf-1",
        created_by_user_id="user-1",
        created_by_email="user@example.com",
        created_by_kind="human",
        created_by_label="user@example.com",
        created_at=datetime.now(timezone.utc),
    )
    cfg = ServerConfig(id="n8n", url="https://upstream-n8n.test/mcp", enabled=True)
    deleted_record = record.model_copy(update={"deleted_at": datetime.now(timezone.utc)})
    upstream_missing = HTTPException(
        status_code=400,
        detail='Upstream tool error: {"message":"Workflow with ID \\"wf-1\\" not found."}',
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_managed_workflow", return_value=record):
            with patch("rest_api.get_server", return_value=cfg):
                with patch("rest_api.call_upstream_mcp_tool", new=AsyncMock(side_effect=upstream_missing)):
                    with patch("rest_api.mark_managed_workflow_deleted", return_value=deleted_record) as mark_deleted:
                        r = client.delete("/admin/n8n-workflows/wf-1", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["workflow_id"] == "wf-1"
    assert r.json()["already_missing_upstream"] is True
    mark_deleted.assert_called_once_with("wf-1")


def managed_app_record(**overrides):
    data = {
        "app_id": "owner-kpis",
        "name": "Owner KPIs",
        "summary": "Shows owner KPI trends.",
        "data_access_summary": "Reads aggregated reservation and revenue data.",
        "data_connections": [{"id": "app-data", "type": "none", "access": "read_only"}],
        "service_name": "app-owner-kpis",
        "service_url": "https://app-owner-kpis.run.app",
        "project_id": "test-gcp-project",
        "region": "europe-west1",
        "runtime_service_account": "dashboard-runtime-sa@test-gcp-project.iam.gserviceaccount.com",
        "status": "pending_review",
        "created_by_user_id": "user-1",
        "created_by_email": "user@example.com",
        "created_by_kind": "human",
        "created_by_label": "user@example.com",
        "created_at": datetime.now(timezone.utc),
    }
    data.update(overrides)
    return ManagedAppRecord(**data)


def test_admin_managed_apps_list_and_approve(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    record = managed_app_record()
    cfg = ServerConfig(id="cloud_run_deployer", url="https://deployer.test/mcp", enabled=True)
    approved = record.model_copy(
        update={
            "status": "approved",
            "approved_by": "admin-1",
            "approved_url": "https://app-owner-kpis-abc-ew.a.run.app",
            "service_url": "https://app-owner-kpis-abc-ew.a.run.app",
        }
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.list_managed_apps", return_value=[record]):
            r = client.get("/admin/apps", headers=auth_headers_admin)
        with patch("rest_api.get_managed_app", return_value=record):
            with patch("rest_api.get_cloud_run_deployer_server", return_value=cfg):
                with patch(
                    "rest_api.call_upstream_mcp_tool",
                    new=AsyncMock(
                        return_value={"approved": True, "approved_url": "https://app-owner-kpis-abc-ew.a.run.app"}
                    ),
                ) as call_tool:
                    with patch("rest_api.approve_managed_app", return_value=approved) as approve:
                        r2 = client.post("/admin/apps/owner-kpis/approve", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()[0]["app_id"] == "owner-kpis"
    assert r2.status_code == 200
    assert r2.json()["status"] == "approved"
    call_tool.assert_awaited_once()
    assert call_tool.call_args.kwargs["tool_name"] == "approve_app"
    approve.assert_called_once_with(
        "owner-kpis", reviewed_by="admin-1", approved_url="https://app-owner-kpis-abc-ew.a.run.app"
    )


def test_me_managed_apps_lists_only_creator_apps(client, auth_headers_user):
    user = sample_user(user_id="user-1", role="user", email="user@example.com")
    record = managed_app_record()
    with patch("auth.jwt_handler.get_user_by_id", return_value=user):
        with patch("api.me.list_managed_apps", return_value=[record]) as list_apps:
            r = client.get("/me/apps", headers=auth_headers_user)
    assert r.status_code == 200
    assert r.json()[0]["app_id"] == "owner-kpis"
    list_apps.assert_called_once_with(include_deleted=True, creator_user_id="user-1")


def test_non_admin_cannot_approve_or_delete_managed_apps(client, auth_headers_user):
    user = sample_user(user_id="user-1", role="user", email="user@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=user):
        approve = client.post("/admin/apps/owner-kpis/approve", headers=auth_headers_user)
        delete = client.delete("/admin/apps/owner-kpis", headers=auth_headers_user)
    assert approve.status_code == 403
    assert delete.status_code == 403


def test_power_user_cannot_list_admin_managed_apps(client, auth_headers_power_user):
    pu = sample_user(user_id="pu-1", role="power_user", email="pu@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=pu):
        r = client.get("/admin/apps", headers=auth_headers_power_user)
    assert r.status_code == 403


def test_admin_managed_app_secret_endpoints_are_metadata_only(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    record = managed_app_record(status="approved")
    now = datetime.now(timezone.utc)
    item = SimpleNamespace(
        app_id="owner-kpis",
        name="API_TOKEN",
        key_version="v1",
        created_by_user_id="admin-1",
        updated_by_user_id="admin-1",
        created_at=now,
        updated_at=now,
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_managed_app", return_value=record):
            with patch("rest_api.set_app_secret", return_value=item) as set_secret:
                r = client.post(
                    "/admin/apps/owner-kpis/secrets",
                    json={"name": "API_TOKEN", "value": "super-secret-value"},
                    headers=auth_headers_admin,
                )
            with patch("rest_api.list_app_secrets", return_value=[item]):
                r2 = client.get("/admin/apps/owner-kpis/secrets", headers=auth_headers_admin)
            with patch("rest_api.delete_app_secret", return_value=True):
                r3 = client.delete("/admin/apps/owner-kpis/secrets/API_TOKEN", headers=auth_headers_admin)

    assert r.status_code == 200
    assert r.json()["name"] == "API_TOKEN"
    assert "value" not in r.json()
    assert "ciphertext" not in r.json()
    set_secret.assert_called_once()
    assert set_secret.call_args.kwargs["value"] == "super-secret-value"
    assert r2.status_code == 200
    assert r2.json()[0]["name"] == "API_TOKEN"
    assert "value" not in r2.json()[0]
    assert r3.status_code == 200
    assert r3.json()["deleted"] is True


def test_runtime_secret_endpoint_checks_runtime_service_account(client):
    record = managed_app_record(
        status="approved",
        runtime_service_account="app-owner-kpis-sa@test-gcp-project.iam.gserviceaccount.com",
    )
    with patch("api.managed_app_runtime.get_managed_app", return_value=record):
        with patch(
            "api.managed_app_runtime.verify_runtime_identity",
            return_value={"email": "app-owner-kpis-sa@test-gcp-project.iam.gserviceaccount.com"},
        ):
            with patch("api.managed_app_runtime.get_app_secret_value", return_value="plain-secret"):
                r = client.get(
                    "/internal/apps/owner-kpis/secrets/API_TOKEN",
                    headers={"Authorization": "Bearer runtime-token"},
                )

    assert r.status_code == 200
    assert r.json() == {"app_id": "owner-kpis", "name": "API_TOKEN", "value": "plain-secret"}


def test_runtime_secret_endpoint_rejects_wrong_service_account(client):
    record = managed_app_record(
        status="approved",
        runtime_service_account="app-owner-kpis-sa@test-gcp-project.iam.gserviceaccount.com",
    )
    with patch("api.managed_app_runtime.get_managed_app", return_value=record):
        with patch(
            "api.managed_app_runtime.verify_runtime_identity",
            return_value={"email": "other@test-gcp-project.iam.gserviceaccount.com"},
        ):
            r = client.get(
                "/internal/apps/owner-kpis/secrets/API_TOKEN",
                headers={"Authorization": "Bearer runtime-token"},
            )

    assert r.status_code == 403


def test_admin_managed_app_delete_calls_cloud_run_deployer(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    record = managed_app_record(status="approved")
    cfg = ServerConfig(id="cloud_run_deployer", url="https://deployer.test/mcp", enabled=True)
    deleted_record = record.model_copy(update={"status": "deleted", "deleted_at": datetime.now(timezone.utc)})
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_managed_app", return_value=record):
            with patch("rest_api.get_cloud_run_deployer_server", return_value=cfg):
                with patch(
                    "rest_api.call_upstream_mcp_tool",
                    new=AsyncMock(return_value={"deleted": True, "already_missing_upstream": False}),
                ) as call_tool:
                    with patch("rest_api.delete_app_secrets", return_value=2):
                        with patch("rest_api.mark_managed_app_deleted", return_value=deleted_record) as mark_deleted:
                            r = client.delete(
                                "/admin/apps/owner-kpis?delete_mode=full_cleanup",
                                headers=auth_headers_admin,
                            )
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["deleted_secret_count"] == 2
    call_tool.assert_awaited_once()
    assert call_tool.call_args.kwargs["tool_name"] == "delete_app"
    assert call_tool.call_args.kwargs["arguments"]["delete_mode"] == "full_cleanup"
    mark_deleted.assert_called_once_with("owner-kpis", delete_mode="full_cleanup", status="deleted")


def test_admin_managed_app_delete_marks_delete_failed_on_upstream_error(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    record = managed_app_record(status="approved")
    cfg = ServerConfig(id="cloud_run_deployer", url="https://deployer.test/mcp", enabled=True)
    upstream_error = HTTPException(status_code=400, detail="Upstream tool error: permission denied")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_managed_app", return_value=record):
            with patch("rest_api.get_cloud_run_deployer_server", return_value=cfg):
                with patch("rest_api.call_upstream_mcp_tool", new=AsyncMock(side_effect=upstream_error)):
                    with patch("rest_api.mark_managed_app_deleted") as mark_deleted:
                        r = client.delete("/admin/apps/owner-kpis", headers=auth_headers_admin)
    assert r.status_code == 400
    mark_deleted.assert_called_once_with("owner-kpis", delete_mode="full_cleanup", status="delete_failed")


def test_admin_managed_app_delete_succeeds_when_upstream_unreachable(client, auth_headers_admin):
    """Local Firestore row is removed even if the deployer MCP cannot be reached."""
    from rest_api import UNREACHABLE_UPSTREAM_MSG

    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    record = managed_app_record(status="approved")
    cfg = ServerConfig(id="cloud_run_deployer", url="https://deployer.test/mcp", enabled=True)
    deleted_record = record.model_copy(update={"status": "deleted", "deleted_at": datetime.now(timezone.utc)})
    unreachable = HTTPException(status_code=400, detail=UNREACHABLE_UPSTREAM_MSG)
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_managed_app", return_value=record):
            with patch("rest_api.get_cloud_run_deployer_server", return_value=cfg):
                with patch("rest_api.call_upstream_mcp_tool", new=AsyncMock(side_effect=unreachable)):
                    with patch("rest_api.delete_app_secrets", return_value=1):
                        with patch("rest_api.mark_managed_app_deleted", return_value=deleted_record) as mark_deleted:
                            r = client.delete("/admin/apps/owner-kpis", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["upstream_unreachable"] is True
    mark_deleted.assert_called_once_with("owner-kpis", delete_mode="full_cleanup", status="deleted")


def test_me_activity_log_explorer_ok(client, auth_headers_user):
    u = sample_user(user_id="user-1", role="user", email="user@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/me/activity/log-explorer", headers=auth_headers_user)
    assert r.status_code == 200
    url = r.json()["url"]
    assert "console.cloud.google.com" in url
    assert "user-1" in url


def test_me_activity_log_explorer_pending_forbidden(client):
    u = sample_user(user_id="pend-1", status="pending")
    token = make_access_token(user_id="pend-1", role="user", email="p@e.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get(
            "/me/activity/log-explorer",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 403


def test_admin_access_requests_pending(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    ar = AccessRequestInDB(
        id="ar1",
        user_id="u1",
        email="a@e.com",
        requested_at=datetime.now(timezone.utc),
        status="pending",
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.users_store.get_pending_access_requests", return_value=[ar]):
            r = client.get("/admin/access-requests/pending", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()[0]["id"] == "ar1"


def test_admin_improvement_requests_pending(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    req = ImprovementRequestInDB(
        id="imp-1",
        requester_user_id="u1",
        requester_email="a@e.com",
        requester_kind="human",
        requester_label="a@e.com",
        summary="Need more fields",
        details="details",
        tool_names=["guesty_find_listing"],
        server_ids=["guesty"],
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.list_pending_improvement_requests", return_value=[req]):
            r = client.get("/admin/improvement-requests/pending", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()[0]["id"] == "imp-1"


def test_admin_skill_update_requests_pending(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    req = SkillUpdateRequestInDB(
        id="skill-1",
        skill_name="host-wise-n8n-workflows",
        requester_user_id="u1",
        requester_email="a@e.com",
        requester_kind="human",
        requester_label="a@e.com",
        summary="Add examples",
        details="details",
        desired_outcome="More complete guidance",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.list_pending_skill_update_requests", return_value=[req]):
            r = client.get("/admin/skill-update-requests/pending", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()[0]["id"] == "skill-1"


def test_admin_improvement_request_approve(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    pending_req = ImprovementRequestInDB(
        id="imp-1",
        requester_user_id="u1",
        requester_email="a@e.com",
        requester_kind="human",
        requester_label="a@e.com",
        summary="Need more fields",
        details="details",
        tool_names=["guesty_find_listing"],
        server_ids=["guesty"],
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    approved_req = pending_req.model_copy(update={"status": "approved", "reviewed_by": "admin-1"})
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_improvement_request", return_value=pending_req):
            with patch("rest_api.approve_improvement_request", return_value=approved_req):
                r = client.post("/admin/improvement-requests/imp-1/approve", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


def test_admin_skill_update_request_approve(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    pending_req = SkillUpdateRequestInDB(
        id="skill-1",
        skill_name="host-wise-n8n-workflows",
        requester_user_id="u1",
        requester_email="a@e.com",
        requester_kind="human",
        requester_label="a@e.com",
        summary="Add examples",
        details="details",
        desired_outcome="More complete guidance",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    approved_req = pending_req.model_copy(update={"status": "approved", "reviewed_by": "admin-1"})
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_skill_update_request", return_value=pending_req):
            with patch("rest_api.approve_skill_update_request", return_value=approved_req):
                r = client.post("/admin/skill-update-requests/skill-1/approve", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


def test_admin_improvement_request_decline(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.delete_improvement_request", return_value=True):
            r = client.post("/admin/improvement-requests/imp-1/decline", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["status"] == "declined"


def test_admin_skill_update_request_decline(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.delete_skill_update_request", return_value=True):
            r = client.post("/admin/skill-update-requests/skill-1/decline", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["status"] == "declined"


@respx.mock
def test_admin_server_diagnostics(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    url = "https://upstream-diag.example/mcp"
    respx.post(url).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "up", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": "sid-1"},
            ),
            httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "t1", "description": "d"}]}},
            ),
            httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 3, "result": {"resources": []}},
            ),
            httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 4, "result": {"prompts": []}},
            ),
        ]
    )
    cfg = ServerConfig(id="s-diag", url=url, enabled=True)
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_server", return_value=cfg):
            with patch(
                "api.diagnostics.resolve_upstream_headers",
                new=AsyncMock(return_value={}),
            ):
                r = client.get("/admin/servers/s-diag/diagnostics", headers=auth_headers_admin)
    assert r.status_code == 200
    body = r.json()
    assert body["sections"]["initialize"]["ok"] is True
    assert body["sections"]["tools"]["ok"] is True
    assert body["sections"]["tools"]["items"][0]["name"] == "t1"


def test_admin_regenerate_user_mcp_key(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    target = sample_user(user_id="t1", status="active")

    def resolve_uid(uid_):
        return admin if uid_ == "admin-1" else target

    with patch("auth.jwt_handler.get_user_by_id", side_effect=resolve_uid):
        with patch("api.admin_users.store.get_user_by_id", side_effect=resolve_uid):
            with patch("api.admin_users.generate_user_key", return_value="new-key-raw"):
                with patch("api.admin_users.get_proxy_url", return_value="https://p/mcp"):
                    r = client.post("/users/t1/mcp-credentials/regenerate", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["user_key"] == "new-key-raw"


def test_csrf_blocks_admin_patch_without_header(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        r = client.patch(
            "/admin/servers/s1",
            headers={"Authorization": auth_headers_admin["Authorization"]},
            json={"enabled": True},
        )
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


def test_me_unauthenticated(client):
    r = client.get("/me")
    assert r.status_code == 401


def test_me_invalid_token(client):
    r = client.get("/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_me_ok(client):
    u = sample_user()
    token = make_access_token(user_id=u.id, role=u.role, email=u.email)
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == u.id
    assert data["email"] == u.email
    assert data["role"] == u.role
    assert data["status"] == u.status


def test_me_status(client):
    u = sample_user(status="pending")
    token = make_access_token(user_id=u.id, role=u.role, email=u.email)
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/me/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "waiting_for_approval"


def test_me_permissions_admin_empty(client, auth_headers_admin):
    u = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/me/permissions", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json() == []


def test_me_permissions_user_list(client, auth_headers_user):
    u = sample_user()
    perms = [PermissionItem(server_id="srv-a", read=True, write=False)]
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        with patch("api.me.get_user_permissions", return_value=perms):
            r = client.get("/me/permissions", headers=auth_headers_user)
    assert r.status_code == 200
    assert r.json() == [{"server_id": "srv-a", "read": True, "write": False}]


def test_me_mcp_credentials_pending_forbidden(client, auth_headers_user):
    u = sample_user(status="pending")
    token = make_access_token(user_id=u.id, role=u.role, email=u.email)
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/me/mcp-credentials", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_me_mcp_credentials_first_time(client, auth_headers_user):
    u = sample_user()
    mock_doc = MagicMock()
    mock_doc.to_dict.return_value = {}
    mock_ref = MagicMock()
    mock_ref.get.return_value = mock_doc
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        with patch("api.me.users_store._users_coll", return_value=MagicMock(document=lambda _id: mock_ref)):
            with patch("api.me.generate_user_key", return_value="raw-key-once"):
                with patch("api.me.get_proxy_url", return_value="https://proxy/mcp-server/mcp"):
                    r = client.get("/me/mcp-credentials", headers=auth_headers_user)
    assert r.status_code == 200
    assert r.json()["user_key"] == "raw-key-once"


def test_me_mcp_credentials_legacy_hash_only_no_reveal(client, auth_headers_user):
    u = sample_user()
    mock_doc = MagicMock()
    mock_doc.to_dict.return_value = {"user_key_hash": "somehash"}
    mock_ref = MagicMock()
    mock_ref.get.return_value = mock_doc
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        with patch("api.me.users_store._users_coll", return_value=MagicMock(document=lambda _id: mock_ref)):
            with patch("api.me.get_proxy_url", return_value="https://proxy/mcp-server/mcp"):
                r = client.get("/me/mcp-credentials", headers=auth_headers_user)
    assert r.status_code == 200
    body = r.json()
    assert "user_key" not in body
    assert "message" in body


def test_me_mcp_credentials_reveal_stored_key(client, auth_headers_user):
    from credentials.generator import encrypt_user_key_for_storage

    u = sample_user()
    raw = "mcp_test_reveal_key"
    enc = encrypt_user_key_for_storage(raw)
    mock_doc = MagicMock()
    mock_doc.to_dict.return_value = {"user_key_hash": "any", "user_key_enc": enc}
    mock_ref = MagicMock()
    mock_ref.get.return_value = mock_doc
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        with patch("api.me.users_store._users_coll", return_value=MagicMock(document=lambda _id: mock_ref)):
            with patch("api.me.get_proxy_url", return_value="https://proxy.example/mcp"):
                r = client.get("/me/mcp-credentials", headers=auth_headers_user)
    assert r.status_code == 200
    body = r.json()
    assert body["user_key"] == raw
    assert body["proxy_url"] == "https://proxy.example/mcp"


def test_me_mcp_regenerate(client, auth_headers_user):
    u = sample_user()
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        with patch("api.me.generate_user_key", return_value="new-raw-key"):
            with patch("api.me.get_proxy_url", return_value="https://proxy/mcp-server/mcp"):
                r = client.post("/me/mcp-credentials/regenerate", headers=auth_headers_user)
    assert r.status_code == 200
    assert r.json()["user_key"] == "new-raw-key"


def test_users_routes_forbidden_for_non_admin(client, auth_headers_user):
    u = sample_user()
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/users", headers=auth_headers_user)
    assert r.status_code == 403


def test_users_usage_summary_forbidden_for_non_admin(client, auth_headers_user):
    u = sample_user()
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/users/user-1/usage-summary", headers=auth_headers_user)
    assert r.status_code == 403


def test_users_usage_ranking_forbidden_for_non_admin(client, auth_headers_user):
    u = sample_user()
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/users/usage-ranking", headers=auth_headers_user)
    assert r.status_code == 403


def test_users_create_agent_admin(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    agent = UserInDB(
        id="ag-1",
        email="",
        kind="agent",
        agent_name="Bot A",
        agent_url="https://example.com/agent",
        role="user",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    def se(uid):
        return admin if uid == "admin-1" else None

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch(
        "api.admin_users.store.create_agent", return_value=agent
    ), patch("api.admin_users.generate_user_key", return_value="mcp_testkey"), patch(
        "api.admin_users.get_proxy_url", return_value="https://proxy/mcp"
    ):
        r = client.post(
            "/users/agents",
            headers=auth_headers_admin,
            json={"agent_name": "Bot A", "agent_url": "https://example.com/agent"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["user_key"] == "mcp_testkey"
    assert body["user"]["kind"] == "agent"
    assert body["user"]["agent_name"] == "Bot A"


def test_users_create_agent_bad_url(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        r = client.post(
            "/users/agents",
            headers=auth_headers_admin,
            json={"agent_name": "X", "agent_url": "not-a-url"},
        )
    assert r.status_code == 400


def test_users_create_ad_hoc_user_admin(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    created = UserInDB(
        id="user-42",
        email="person@example.com",
        kind="human",
        role="user",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    def se(uid):
        return admin if uid == "admin-1" else None

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch(
        "api.admin_users.user_service.create_ad_hoc_user", return_value=created
    ), patch("api.admin_users.generate_user_key", return_value="mcp_person_key"), patch(
        "api.admin_users.get_proxy_url", return_value="https://proxy/mcp"
    ):
        r = client.post(
            "/users/ad-hoc",
            headers=auth_headers_admin,
            json={"email": "person@example.com"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["user_key"] == "mcp_person_key"
    assert body["user"]["kind"] == "human"
    assert body["user"]["email"] == "person@example.com"


def test_users_create_ad_hoc_user_conflict(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin), patch(
        "api.admin_users.user_service.create_ad_hoc_user",
        side_effect=ValueError("User with this email already exists"),
    ):
        r = client.post(
            "/users/ad-hoc",
            headers=auth_headers_admin,
            json={"email": "person@example.com"},
        )
    assert r.status_code == 409
    assert r.json()["detail"] == "User with this email already exists"


def test_users_patch_agent_role_forbidden(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    target = UserInDB(
        id="ag-1",
        email="",
        kind="agent",
        agent_name="Bot",
        role="user",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    def se(uid):
        return admin if uid == "admin-1" else target

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch(
        "api.admin_users.store.get_user_by_id", side_effect=se
    ):
        r = client.patch(
            "/users/ag-1/role",
            headers=auth_headers_admin,
            json={"role": "admin"},
        )
    assert r.status_code == 400


def test_users_list_admin(client, auth_headers_admin):
    u = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    listed = [
        UserInDB(
            id="u2",
            email="two@e.com",
            role="user",
            status="active",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    ]
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        with patch("api.admin_users.store.list_users", return_value=listed):
            r = client.get("/users", headers=auth_headers_admin)
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["id"] == "u2"


def test_users_pending_admin(client, auth_headers_admin):
    u = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    pending_u = sample_user(user_id="pend-1", status="pending")
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        with patch("api.admin_users.store.get_pending_users", return_value=[pending_u]):
            r = client.get("/users/pending", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()[0]["status"] == "pending"


def test_users_approve_not_found(client, auth_headers_admin):
    u = sample_user(user_id="admin-1", role="admin", email="admin@example.com")

    def se(uid):
        return u if uid == "admin-1" else None

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch("users.store.get_user_by_id", side_effect=se):
        r = client.post("/users/missing/approve", headers=auth_headers_admin)
    assert r.status_code == 404


def test_users_approve_not_pending(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    target = sample_user(user_id="t1", status="active")

    def se(uid):
        return admin if uid == "admin-1" else target

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch("users.store.get_user_by_id", side_effect=se):
        r = client.post("/users/t1/approve", headers=auth_headers_admin)
    assert r.status_code == 400


def test_users_approve_ok(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    target = sample_user(user_id="t1", status="pending")
    req = AccessRequestInDB(
        id="ar1",
        user_id="t1",
        email="t@e.com",
        requested_at=datetime.now(timezone.utc),
        status="pending",
    )

    def se(uid):
        return admin if uid == "admin-1" else target

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch("users.store.get_user_by_id", side_effect=se):
        with patch("api.admin_users.store.update_user") as upd:
            with patch("api.admin_users.store.get_access_requests_by_user", return_value=[req]):
                with patch("api.admin_users.store.update_access_request") as upd_ar:
                    r = client.post("/users/t1/approve", headers=auth_headers_admin)
    assert r.status_code == 200
    upd.assert_called_once()
    upd_ar.assert_called_once()


def test_users_put_permissions(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    target = sample_user(user_id="t1", status="active")

    def se(uid):
        return admin if uid == "admin-1" else target

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch("users.store.get_user_by_id", side_effect=se):
        with patch("api.admin_users.set_user_permissions") as sup:
            r = client.put(
                "/users/t1/permissions",
                headers=auth_headers_admin,
                json={"permissions": [{"server_id": "s1", "read": True, "write": True}]},
            )
    assert r.status_code == 200
    sup.assert_called_once()


def test_users_usage_summary_admin(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    target = sample_user(user_id="t1", status="active")
    summary = UserUsageSummary(
        window_days=30,
        authorized_request_count=7,
        tool_call_count=5,
        log_explorer_url="https://console.cloud.google.com/logs/query?user=t1",
    )

    def se(uid):
        if uid == "admin-1":
            return admin
        if uid == "t1":
            return target
        return None

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch(
        "api.admin_users.store.get_user_by_id", side_effect=se
    ), patch("api.admin_users.get_user_usage_summary", return_value=summary):
        r = client.get("/users/t1/usage-summary", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["authorized_request_count"] == 7
    assert r.json()["tool_call_count"] == 5


def test_users_usage_summary_not_found(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")

    def se(uid):
        return admin if uid == "admin-1" else None

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch(
        "api.admin_users.store.get_user_by_id", side_effect=se
    ):
        r = client.get("/users/missing/usage-summary", headers=auth_headers_admin)
    assert r.status_code == 404


def test_users_usage_summary_cloud_logging_failure(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    target = sample_user(user_id="t1", status="active")

    def se(uid):
        if uid == "admin-1":
            return admin
        if uid == "t1":
            return target
        return None

    with patch("auth.jwt_handler.get_user_by_id", side_effect=se), patch(
        "api.admin_users.store.get_user_by_id", side_effect=se
    ), patch(
        "api.admin_users.get_user_usage_summary",
        side_effect=UserUsageSummaryUnavailableError("Cloud Logging query failed"),
    ):
        r = client.get("/users/t1/usage-summary", headers=auth_headers_admin)
    assert r.status_code == 503
    assert r.json()["detail"] == "Cloud Logging query failed"


def test_users_usage_ranking_admin(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    leaderboard = UserUsageLeaderboard(
        window_days=30,
        users=[],
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin), patch(
        "api.admin_users.get_usage_leaderboard", return_value=leaderboard
    ):
        r = client.get("/users/usage-ranking", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()["window_days"] == 30


def test_users_usage_ranking_cloud_logging_failure(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin), patch(
        "api.admin_users.get_usage_leaderboard",
        side_effect=UserUsageSummaryUnavailableError("Cloud Logging query failed: permission denied"),
    ):
        r = client.get("/users/usage-ranking", headers=auth_headers_admin)
    assert r.status_code == 503
    assert r.json()["detail"] == "Cloud Logging query failed: permission denied"


def test_admin_servers_forbidden_power_user(client, auth_headers_power_user):
    u = sample_user(user_id="pu-1", role="power_user", email="pu@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=u):
        r = client.get("/admin/servers", headers=auth_headers_power_user)
    assert r.status_code == 403


def test_admin_servers_list(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    configs = [
        ServerConfig(
            id="s1",
            url="https://a/mcp",
            enabled=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    ]
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.list_servers", return_value=configs):
            r = client.get("/admin/servers", headers=auth_headers_admin)
    assert r.status_code == 200
    assert r.json()[0]["id"] == "s1"


def test_admin_servers_get_404(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_server", return_value=None):
            r = client.get("/admin/servers/nope", headers=auth_headers_admin)
    assert r.status_code == 404


def test_admin_servers_post_empty(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        r = client.post("/admin/servers", headers=auth_headers_admin, json=[])
    assert r.status_code == 400


@respx.mock
def test_admin_servers_post_creates_with_upstream_ok(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    url = "https://upstream-test.example/mcp"
    respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "up", "version": "1"},
                },
            },
            headers={"Mcp-Session-Id": "up-sid"},
        )
    )
    created = ServerConfig(
        id="new-srv",
        url=url,
        enabled=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_server", return_value=None):
            with patch(
                "rest_api.resolve_upstream_headers",
                new=AsyncMock(return_value={}),
            ):
                with patch("rest_api.create_server", return_value=created):
                    r = client.post(
                        "/admin/servers",
                        headers=auth_headers_admin,
                        json=[{"id": "new-srv", "url": url, "enabled": True}],
                    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["created"]) == 1
    assert body["created"][0]["id"] == "new-srv"
    assert body["errors"] == []


def test_admin_servers_post_duplicate(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    existing = ServerConfig(id="dup", url="https://x/mcp", enabled=True)
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_server", return_value=existing):
            r = client.post(
                "/admin/servers",
                headers=auth_headers_admin,
                json=[{"id": "dup", "url": "https://x/mcp"}],
            )
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == []
    assert any(e.get("id") == "dup" and "exists" in e.get("detail", "").lower() for e in body["errors"])


def test_admin_servers_patch(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    cfg = ServerConfig(id="s1", url="https://old/mcp", enabled=False)
    updated = ServerConfig(id="s1", url="https://new/mcp", enabled=True)
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_server", return_value=cfg):
            with patch("rest_api.update_server", return_value=updated) as mock_upd:
                r = client.patch(
                    "/admin/servers/s1",
                    headers=auth_headers_admin,
                    json={"url": "https://new/mcp", "enabled": True},
                )
    assert r.status_code == 200
    assert r.json()["url"] == "https://new/mcp"
    assert mock_upd.call_args.kwargs.get("reset_credentials") in (None, False)


def test_admin_servers_patch_reset_credentials(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    cfg = ServerConfig(id="s1", url="https://x/mcp", enabled=True)
    updated = ServerConfig(
        id="s1",
        url="https://x/mcp",
        enabled=True,
        credentials_secret_id="new-secret",
        credentials_header="",
    )
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.get_server", return_value=cfg):
            with patch("rest_api.update_server", return_value=updated) as mock_upd:
                r = client.patch(
                    "/admin/servers/s1",
                    headers=auth_headers_admin,
                    json={
                        "reset_credentials": True,
                        "credentials_secret_id": "new-secret",
                    },
                )
    assert r.status_code == 200
    assert mock_upd.call_args.kwargs["reset_credentials"] is True
    assert mock_upd.call_args.kwargs["credentials_secret_id"] == "new-secret"


def test_admin_servers_delete(client, auth_headers_admin):
    admin = sample_user(user_id="admin-1", role="admin", email="admin@example.com")
    with patch("auth.jwt_handler.get_user_by_id", return_value=admin):
        with patch("rest_api.delete_server", return_value=True):
            r = client.delete("/admin/servers/s1", headers=auth_headers_admin)
    assert r.status_code == 204
