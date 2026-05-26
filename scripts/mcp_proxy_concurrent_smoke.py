#!/usr/bin/env python3
"""Run the mcp-proxy concurrent smoke script from this Poetry project (global/mcp/scripts).

Delegates to ../mcp-proxy/scripts/mcp_proxy_concurrent_smoke.py — same CLI and env vars.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent.parent / "mcp-proxy" / "scripts" / "mcp_proxy_concurrent_smoke.py"

if __name__ == "__main__":
    if not _TARGET.is_file():
        print(f"Expected script at {_TARGET}", file=sys.stderr)
        raise SystemExit(127)
    raise SystemExit(subprocess.call([sys.executable, str(_TARGET), *sys.argv[1:]]))
