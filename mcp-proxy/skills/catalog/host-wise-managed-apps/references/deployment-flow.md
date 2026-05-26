# Deployment Flow

Managed apps deploy through the `cloud-run-deployer-mcp` server.

Order:

1. Read `cloud-run-deployer://help/index`.
2. Read `cloud-run-deployer://help/managed-dashboard-workflow`.
3. Read `cloud-run-deployer://templates/dashboard-app`.
4. Read `cloud-run-deployer://schemas/deployment-manifest`.
5. Read `cloud-run-deployer://schemas/query-registry`.
6. Read `cloud-run-deployer://safety/checklist`.
7. Build and smoke test the app locally.
8. Call `plan_source_archive(app_id, source_path)` and run the returned local packaging/upload commands.
9. Use the printed GCS URI in the deployment manifest.
10. Call `validate_deployment_manifest`.
11. Call `plan_deployment`.
12. Call `deploy_app` with `execute=true`.

Manifest essentials:

```json
{
  "app_id": "owner-tools",
  "name": "Owner Tools",
  "summary": "Small internal app for owner operations.",
  "framework": "fastapi",
  "source": {
    "type": "gcs_archive",
    "gcs_archive": "gs://it-team-hw-project-cloud-run-deployer-staging/apps/owner-tools/source.tgz"
  },
  "data": { "sqlite": true, "uploads": true },
  "access": { "mode": "iap" }
}
```

Lifecycle:

- `deploy_app` builds and deploys the service, then applies IAP (Google sign-in) on the Run URL in one pipeline.
- MCP Proxy records the app (admin view for cleanup; creators see their own apps).
- `approve_app` remains available if IAP must be re-applied after a manual service change.
- Admin deletion in MCP Proxy Apps calls `delete_app` with `full_cleanup` by default.
- Source code remains in the repo unless a later repo cleanup policy is added.
