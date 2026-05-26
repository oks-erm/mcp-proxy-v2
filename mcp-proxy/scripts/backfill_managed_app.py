#!/usr/bin/env python3
"""
Register or refresh a managed_apps Firestore document (e.g. when Cloud Run already exists
but the Apps UI has no row because async deploy did not run complete_deployment through the proxy).

Requires Application Default Credentials for the Firestore project.

  export MCP_BACKFILL_USER_ID='<Firestore user id of an admin or the app owner>'
  cd global/mcp/mcp-proxy
  PYTHONPATH=. python3 scripts/backfill_managed_app.py \\
    --app-id owner-revenue-pulse \\
    --name "Owner Revenue Pulse" \\
    --summary "..." \\
    --data-access-summary "..." \\
    --service-name app-owner-revenue-pulse \\
    --project-id it-team-hw-project \\
    --region europe-west1 \\
    --runtime-sa 'app-owner-revenue-pulse-sa@it-team-hw-project.iam.gserviceaccount.com' \\
    --framework fastapi \\
    --build-id '<optional last Cloud Build id>'

The user id is the `id` field of a user document in the MCP users collection (the creator shown in the UI).
"""

from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--app-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--data-access-summary", required=True)
    p.add_argument("--service-name", required=True)
    p.add_argument("--project-id", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--service-url", default="")
    p.add_argument("--runtime-sa", default="")
    p.add_argument("--framework", default="")
    p.add_argument("--build-id", default="")
    p.add_argument(
        "--creator-user-id",
        default=os.environ.get("MCP_BACKFILL_USER_ID", ""),
        help="Firestore user id, or set env MCP_BACKFILL_USER_ID",
    )
    p.add_argument("--creator-email", default="")
    args = p.parse_args()
    if not args.creator_user_id.strip():
        p.error("Pass --creator-user-id or set MCP_BACKFILL_USER_ID")

    from managed_apps_store import upsert_managed_app
    from users.schemas import UserInDB

    user = UserInDB(
        id=args.creator_user_id.strip(),
        email=args.creator_email.strip() or "backfill@hostwise.internal",
        kind="human",
        role="admin",
        status="active",
    )
    rec = upsert_managed_app(
        app_id=args.app_id.strip(),
        name=args.name.strip(),
        summary=args.summary.strip(),
        data_access_summary=args.data_access_summary.strip(),
        data_connections=[],
        service_name=args.service_name.strip(),
        service_url=args.service_url.strip() or None,
        project_id=args.project_id.strip(),
        region=args.region.strip(),
        runtime_service_account=args.runtime_sa.strip(),
        framework=args.framework.strip(),
        build_id=args.build_id.strip(),
        user=user,
    )
    print(f"OK: upserted managed_apps/{rec.app_id} (status={rec.status})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
