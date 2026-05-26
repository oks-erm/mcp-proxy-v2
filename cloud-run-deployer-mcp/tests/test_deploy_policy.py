from pathlib import Path

import deploy_policy
from deploy_policy import (
    app_metadata,
    build_plan,
    render_approve_commands,
    render_build_commands,
    render_delete_commands,
    render_deploy_commands,
    render_github_commands,
    render_shell,
    validate_manifest,
    verify_dashboard_includes_auth_page,
)


def base_manifest():
    return {
        "app_id": "owner-kpis",
        "name": "Owner KPIs",
        "summary": "Shows owner KPI trends for operations review.",
        "data_access_summary": "Reads aggregated reservation and revenue data through the internal SQL gateway.",
        "source": {
            "type": "image",
            "image": "europe-west1-docker.pkg.dev/it-team-hw-project/docker/hostwise/apps/owner-kpis:latest",
        },
        "access": {
            "mode": "iap",
            "iap_principals": ["group:operations@example.com"],
        },
        "runtime": {
            "port": 8080,
            "max_instances": 3,
        },
        "data_connections": [
            {
                "id": "warehouse",
                "type": "sql_gateway",
                "target": "sql-gateway",
                "access": "read_only",
            }
        ],
    }


def test_valid_manifest_defaults_to_private_iap_plan():
    plan = build_plan(base_manifest())

    assert plan["ok"] is True
    commands = "\n".join(plan["commands"])
    assert plan["approval_commands"] == []
    assert "--no-allow-unauthenticated" in commands
    assert "--ingress internal" in commands
    assert "gcloud run services update" in commands
    assert "--ingress all" in commands
    assert "--iap" in commands
    assert "roles/iap.httpsResourceAccessor" in commands
    assert "roles/run.invoker" in commands


def test_omitted_iap_principals_use_hostwise_domain_default():
    """When access.iap_principals is omitted, approval grants domain:hostwise.pt."""
    manifest = base_manifest()
    manifest["access"] = {"mode": "iap"}
    plan = build_plan(manifest)

    assert plan["ok"] is True
    joined = "\n".join(plan["commands"])
    assert "domain:hostwise.pt" in joined
    assert "group:operations@example.com" not in joined


def test_hostwise_co_iap_principal_is_removed_from_plan():
    manifest = base_manifest()
    manifest["access"] = {"mode": "iap", "iap_principals": ["domain:hostwise.co", "domain:hostwise.pt"]}

    plan = build_plan(manifest)

    assert plan["ok"] is True
    joined = "\n".join(plan["commands"])
    assert (
        "add-iam-policy-binding --project it-team-hw-project --region europe-west1 "
        "--resource-type cloud-run --service app-owner-kpis --member domain:hostwise.co" not in joined
    )
    assert "remove-iam-policy-binding" in joined
    assert "--member domain:hostwise.co" in joined
    assert "domain:hostwise.pt" in joined
    assert any("domain:hostwise.co" in warning for warning in plan["warnings"])


def test_public_access_is_forbidden():
    manifest = base_manifest()
    manifest["access"] = {"mode": "public", "allow_public": True}

    result = validate_manifest(manifest)

    assert result.ok is False
    assert any("public access is forbidden" in e for e in result.errors)


def test_data_connections_must_be_declared_list():
    manifest = base_manifest()
    manifest["data_connections"] = {"type": "sql_gateway"}

    result = validate_manifest(manifest)

    assert result.ok is False
    assert "data_connections must be a list" in result.errors


def test_summary_fields_are_required():
    manifest = base_manifest()
    manifest.pop("summary")
    manifest["data_access_summary"] = ""

    result = validate_manifest(manifest)

    assert result.ok is False
    assert any("summary is required" in e for e in result.errors)
    assert not any("data_access_summary is required" in e for e in result.errors)


def test_dashboard_app_source_uses_shared_runtime_and_build_plan_metadata():
    manifest = base_manifest()
    manifest["source"] = {"type": "dashboard_app", "path": "apps/owner-kpis"}

    plan = build_plan(manifest)

    assert plan["ok"] is True
    assert plan["app"]["app_id"] == "owner-kpis"
    assert plan["app"]["summary"] == manifest["summary"]
    assert plan["normalized_manifest"]["source"]["path"] == "apps/owner-kpis"
    assert plan["normalized_manifest"]["runtime_service_account"].startswith("app-owner-kpis-sa@")
    commands = "\n".join(plan["commands"])
    assert "gcloud builds submit apps/owner-kpis" in commands
    assert "service-accounts create" in commands


def test_github_commands_are_config_gated_for_dashboard_sources(monkeypatch):
    monkeypatch.setattr(deploy_policy, "DEFAULT_GITHUB_ORG", "hostwise-internal-apps")
    monkeypatch.setattr(deploy_policy, "DEFAULT_GITHUB_REPO_PREFIX", "app")
    manifest = base_manifest()
    manifest["source"] = {"type": "dashboard_app", "path": "apps/owner-kpis"}

    commands = "\n".join(render_shell(render_github_commands(manifest)))
    metadata = app_metadata(validate_manifest(manifest).normalized)

    assert "gh repo create hostwise-internal-apps/app-owner-kpis --private" in commands
    assert "git push hostwise-origin HEAD:main --force" in commands
    assert metadata["repo_url"] == "https://github.com/hostwise-internal-apps/app-owner-kpis"


def test_app_metadata_includes_service_url_when_available():
    manifest = validate_manifest(base_manifest()).normalized

    metadata = app_metadata(manifest, service_url="https://owner-kpis.example.run.app")

    assert metadata["service_url"] == "https://owner-kpis.example.run.app"
    assert metadata["data_access_summary"] == base_manifest()["data_access_summary"]


def test_render_delete_commands_supports_delete_modes():
    cloud_run_only = render_delete_commands(service_name="app-owner-kpis")
    full_cleanup = render_delete_commands(
        service_name="app-owner-kpis",
        delete_mode="full_cleanup",
        image="europe-west1-docker.pkg.dev/project/docker/hostwise/apps/owner-kpis:latest",
        runtime_service_account="app-owner-kpis-sa@project.iam.gserviceaccount.com",
    )

    assert cloud_run_only[0][:4] == ["gcloud", "run", "services", "delete"]
    rendered = [" ".join(command) for command in full_cleanup]
    assert any("artifacts docker images delete" in command for command in rendered)
    assert any("iam service-accounts delete" in command for command in rendered)


def test_build_plan_uses_configured_gcs_source_staging_dir(monkeypatch):
    monkeypatch.setattr(deploy_policy, "DEFAULT_GCS_SOURCE_STAGING_DIR", "gs://deployer-staging/source")
    manifest = base_manifest()
    manifest["source"] = {"type": "dashboard_app", "path": "apps/owner-kpis"}

    plan = build_plan(manifest)

    assert plan["ok"] is True
    commands = "\n".join(plan["commands"])
    assert "--gcs-source-staging-dir gs://deployer-staging/source" in commands


def test_async_build_commands_return_cloud_build_only():
    manifest = base_manifest()
    manifest["source"] = {
        "type": "gcs_archive",
        "gcs_archive": "gs://it-team-hw-project-cloud-run-deployer-staging/apps/owner-kpis/source.tgz",
    }

    commands = render_shell(render_build_commands(manifest, async_build=True))

    assert len(commands) == 1
    assert "gcloud builds submit" in commands[0]
    assert "--async" in commands[0]
    assert "--format json" in commands[0]
    assert "gcloud run deploy" not in commands[0]


def test_deploy_commands_return_cloud_run_without_build():
    manifest = base_manifest()
    manifest["source"] = {
        "type": "gcs_archive",
        "gcs_archive": "gs://it-team-hw-project-cloud-run-deployer-staging/apps/owner-kpis/source.tgz",
    }

    commands = "\n".join(render_shell(render_deploy_commands(manifest)))

    assert "gcloud run deploy" in commands
    assert "gcloud builds submit" not in commands


def test_gcs_image_manifests_warn_auth_not_scanned():
    m = base_manifest()
    v = deploy_policy.validate_manifest(m)
    assert v.ok
    assert any("not scan" in w.lower() and "secrets" in w.lower() for w in v.warnings)

    m2 = base_manifest()
    m2["source"] = {
        "type": "gcs_archive",
        "gcs_archive": "gs://bucket/apps/x/source.tgz",
    }
    v2 = deploy_policy.validate_manifest(m2)
    assert v2.ok
    assert any("not scan" in w.lower() for w in v2.warnings)


def test_dashboard_app_without_auth_page_fails_when_path_on_disk(tmp_path: Path):
    root = tmp_path / "app"
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "frontend" / "src" / "App.tsx").write_text("export function App() { return null }", encoding="utf-8")
    m = base_manifest()
    m["source"] = {"type": "dashboard_app", "path": str(root)}
    r = validate_manifest(m)
    assert r.ok is True
    assert verify_dashboard_includes_auth_page(root)


def test_dashboard_app_with_login_file_passes(tmp_path: Path):
    root = tmp_path / "app"
    (root / "frontend" / "src" / "pages").mkdir(parents=True)
    (root / "frontend" / "src" / "pages" / "Login.tsx").write_text("export function Login() {}", encoding="utf-8")
    m = base_manifest()
    m["source"] = {"type": "dashboard_app", "path": str(root)}
    r = validate_manifest(m)
    assert r.ok is True
    assert verify_dashboard_includes_auth_page(root) == []


def test_dashboard_app_with_login_route_in_router_passes(tmp_path: Path):
    root = tmp_path / "app"
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "frontend" / "src" / "App.tsx").write_text(
        'import {Route} from "r"; <Route path="/login" element={<X/>} />', encoding="utf-8"
    )
    m = base_manifest()
    m["source"] = {"type": "dashboard_app", "path": str(root)}
    r = validate_manifest(m)
    assert r.ok is True


def test_approval_commands_enable_hostwise_iap_route():
    commands = "\n".join(render_shell(render_approve_commands(base_manifest())))

    assert "gcloud run services update app-owner-kpis" in commands
    assert "--ingress all" in commands
    assert "--iap" in commands
    assert "domain:hostwise.pt" not in commands
