from mcp_server import (
    agent_deployment_guide,
    complete_deployment,
    dashboard_app_template,
    deploy_app,
    deployment_manifest_schema,
    deployment_manifest_writer,
    diagnose_runtime,
    get_deployment_status,
    help_index,
    managed_dashboard_app_builder,
    managed_dashboard_review_checklist,
    managed_dashboard_workflow,
    plan_source_archive,
    query_registry_schema,
    safety_checklist,
    start_source_upload,
)


def base_gcs_manifest():
    return {
        "app_id": "owner-kpis",
        "name": "Owner KPIs",
        "summary": "Shows owner KPI trends for operations review.",
        "data_access_summary": "Reads aggregated reservation and revenue data through named backend queries.",
        "source": {
            "type": "gcs_archive",
            "gcs_archive": "gs://it-team-hw-project-cloud-run-deployer-staging/apps/owner-kpis/source.tgz",
        },
        "access": {"mode": "iap"},
        "data_connections": [{"id": "warehouse", "type": "none", "access": "read_only"}],
    }


def test_guidance_resources_include_required_contracts():
    assert "deploy_app" in help_index()
    assert "approve_app" in help_index()
    assert "get_app_status" in help_index()
    assert "agent-deployment" in help_index()
    assert "gcs_archive" in agent_deployment_guide() and "MCP Proxy" in agent_deployment_guide()
    assert "start_source_upload" in agent_deployment_guide()
    assert "SQLite" in managed_dashboard_workflow()
    assert "Next.js" in dashboard_app_template()
    assert "IAP" in dashboard_app_template() or "MCP Proxy" in dashboard_app_template()
    assert "summary" in deployment_manifest_schema()
    assert "SQLite" in query_registry_schema()
    assert "validate_deployment_manifest" in safety_checklist()
    assert "finalize_source_upload" in safety_checklist()


def test_guidance_prompts_reference_required_flow():
    builder = managed_dashboard_app_builder("Build an owner KPI dashboard")
    review = managed_dashboard_review_checklist("Shows KPIs", "Reads aggregated bookings")
    manifest = deployment_manifest_writer("Build an owner KPI dashboard", "owner-kpis")

    assert "cloud-run-deployer://help/index" in builder
    assert "cloud-run-deployer://help/agent-deployment" in builder
    assert "deploy_app" in builder
    assert "start_source_upload" in builder
    assert "production database" in review.lower()
    assert "owner-kpis" in manifest
    assert "MCP Proxy" in manifest
    assert "SQLite" in manifest


def test_start_source_upload_rejects_invalid_declarations_before_gcs():
    result = start_source_upload(
        app_id="owner-kpis",
        summary="Shows owner KPIs.",
        files=[{"path": "../secret.txt", "size_bytes": 5, "sha256": "bad", "content_type": "text"}],
    )

    structured = result.structuredContent
    assert structured["error"] == "validation_error"
    assert any("path traversal" in err or "invalid relative" in err for err in structured["errors"])


def test_diagnose_runtime_tool_returns_structured_payload(monkeypatch):
    monkeypatch.setattr(
        "mcp_server.runtime_diagnostics",
        lambda project_id=None, bucket=None: {"project_id": project_id, "bucket": bucket, "probes": {}},
    )

    result = diagnose_runtime(project_id="it-team-hw-project", bucket="it-team-hw-project_cloudbuild")

    structured = result.structuredContent
    assert structured["ok"] is True
    assert structured["diagnostics"]["project_id"] == "it-team-hw-project"


def test_plan_source_archive_returns_agent_packaging_steps():
    result = plan_source_archive(
        app_id="owner-kpis",
        source_path="apps/owner-kpis",
        project_id="it-team-hw-project",
        bucket="it-team-hw-project-cloud-run-deployer-staging",
    )

    structured = result.structuredContent
    assert structured["ok"] is True
    assert any("tar -C" in command for command in structured["commands"])
    assert any("gcloud storage cp" in command for command in structured["commands"])
    assert structured["manifest_source_template"]["type"] == "gcs_archive"


def test_get_deployment_status_reports_async_next_steps(monkeypatch):
    monkeypatch.setattr(
        "mcp_server._describe_build",
        lambda build_id, project_id, region: {
            "ok": True,
            "build_id": build_id,
            "status": "SUCCESS",
            "log_url": "https://example.com/build-log",
        },
    )
    monkeypatch.setattr(
        "mcp_server._describe_cloud_run_service",
        lambda service_name, project_id, region: {"ok": False, "service_name": service_name},
    )

    result = get_deployment_status(
        build_id="11111111-1111-1111-1111-111111111111",
        service_name="app-owner-kpis",
        project_id="it-team-hw-project",
        region="europe-west1",
    )

    structured = result.structuredContent
    assert structured["ready_to_complete"] is True
    assert "complete_deployment" in structured["next_steps"][0]


def test_complete_deployment_dry_run_renders_only_deploy_commands():
    result = complete_deployment(base_gcs_manifest(), build_id="11111111-1111-1111-1111-111111111111")

    structured = result.structuredContent
    commands = "\n".join(structured["commands"])
    assert structured["dry_run"] is True
    assert "gcloud run deploy" in commands
    assert "gcloud builds submit" not in commands


def test_deploy_app_async_build_returns_polling_contract(monkeypatch):
    monkeypatch.setattr("mcp_server.MUTATIONS_ENABLED", True)
    monkeypatch.setattr(
        "mcp_server._run_commands",
        lambda commands: (
            [
                {
                    "command": "gcloud builds submit ... --async --format json",
                    "returncode": 0,
                    "stdout": '{"metadata":{"build":{"id":"11111111-1111-1111-1111-111111111111"}}}',
                    "stderr": "",
                }
            ],
            None,
        ),
    )
    monkeypatch.setattr(
        "mcp_server._describe_build",
        lambda build_id, project_id, region: {"ok": True, "build_id": build_id, "status": "WORKING"},
    )

    result = deploy_app(base_gcs_manifest(), execute=True, async_build=True)

    structured = result.structuredContent
    assert structured["async_build"] is True
    assert structured["service_deployed"] is False
    assert structured["build_id"] == "11111111-1111-1111-1111-111111111111"
    assert any("get_deployment_status" in step for step in structured["next_steps"])


def test_complete_deployment_returns_app_metadata_when_post_deploy_binding_fails(monkeypatch):
    monkeypatch.setattr("mcp_server.MUTATIONS_ENABLED", True)
    monkeypatch.setattr(
        "mcp_server._run_commands",
        lambda commands: (
            [
                {
                    "command": "gcloud run deploy app-owner-kpis --project it-team-hw-project",
                    "returncode": 0,
                    "stdout": "Service URL: https://app-owner-kpis.run.app",
                    "stderr": "",
                },
                {
                    "command": "gcloud run services add-iam-policy-binding mcp-proxy",
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "PERMISSION_DENIED: run.services.getIamPolicy",
                },
            ],
            {
                "error": "command_failed",
                "details": "PERMISSION_DENIED: run.services.getIamPolicy",
                "command": ["gcloud", "run", "services", "add-iam-policy-binding"],
            },
        ),
    )
    monkeypatch.setattr(
        "mcp_server._describe_build",
        lambda build_id, project_id, region: {"ok": True, "build_id": build_id, "status": "SUCCESS"},
    )
    monkeypatch.setattr(
        "mcp_server._describe_service_url",
        lambda service_name, project_id, region: "https://app-owner-kpis.run.app",
    )
    manifest = base_gcs_manifest()
    manifest["data_connections"] = [{"id": "mcp-proxy", "type": "mcp_proxy", "target": "mcp-proxy"}]

    result = complete_deployment(
        manifest,
        build_id="11111111-1111-1111-1111-111111111111",
        execute=True,
    )

    structured = result.structuredContent
    assert structured["ok"] is False
    assert structured["executed"] is True
    assert structured["service_deployed"] is True
    assert structured["partial_failure"] is True
    assert structured["app"]["app_id"] == "owner-kpis"
    assert structured["app"]["service_url"] == "https://app-owner-kpis.run.app"
