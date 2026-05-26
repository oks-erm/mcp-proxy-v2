#!/usr/bin/env python3
"""Copy canonical global/mcp/mcp_platform into each deployable MCP service (Cloud Run --source context)."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_MCP = Path(__file__).resolve().parent.parent
CANON = REPO_MCP / "mcp_platform"

TARGETS = [
    "absence-mcp",
    "breezeway-mcp",
    "guesty-mcp",
    "zendesk-mcp",
    "moloni-mcp",
    "pipedrive-mcp",
    "pricelabs-mcp",
    "quickbooks-mcp",
    "stripe-mcp",
    "n8n-mcp",
    "sql-gateway",
    "firestore-gateway",
    "utils-mcp",
    "cloud-run-deployer-mcp",
]


def sync_one(dest_root: Path) -> None:
    dest = dest_root / "mcp_platform"
    if not CANON.is_dir():
        print(f"Missing canonical package: {CANON}", file=sys.stderr)
        sys.exit(1)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CANON, dest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        nargs="*",
        metavar="SERVICE",
        help=f"Subset of: {', '.join(TARGETS)}",
    )
    args = parser.parse_args()
    targets = TARGETS
    if args.only:
        unknown = [t for t in args.only if t not in TARGETS]
        if unknown:
            print(f"Unknown --only names: {unknown}", file=sys.stderr)
            sys.exit(1)
        targets = list(args.only)
    for name in targets:
        sync_one(REPO_MCP / name)
        print(f"synced mcp_platform -> {name}/mcp_platform")


if __name__ == "__main__":
    main()
