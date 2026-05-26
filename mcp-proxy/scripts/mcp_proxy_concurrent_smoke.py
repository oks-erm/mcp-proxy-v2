#!/usr/bin/env python3
"""
Smoke / load-style exercise for mcp-proxy: many concurrent MCP sessions, each calling
a random subset of tools (JSON-RPC over Streamable HTTP).

Each "agent" performs: initialize -> tools/list -> tools/call on K randomly chosen
tools (default K=10) with empty arguments. Upstream MCP servers may return JSON-RPC
errors for incomplete args; HTTP success still validates proxy routing under load.

Usage:

  cd global/mcp/mcp-proxy && poetry run python scripts/mcp_proxy_concurrent_smoke.py \\
    --base-url https://your-mcp-proxy.run.app

  # or from global/mcp/scripts (shared Poetry project):
  cd global/mcp/scripts && poetry run python mcp_proxy_concurrent_smoke.py \\
    --base-url https://your-mcp-proxy.run.app

  Optional: --agents N (default 20), --tools K (default 10). Omitted flags use defaults.

  --base-url is the service origin only, or a full MCP URL; trailing /mcp-server/mcp is stripped.

Environment:
  MCP_PROXY_BASE_URL — default http://127.0.0.1:8080
  MCP_PROXY_API_KEY  — required unless --api-key is set
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

MCP_PATH = "/mcp-server/mcp"

INIT_BODY: dict[str, Any] = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-proxy-concurrent-smoke", "version": "0.1.0"},
    },
}


@dataclass
class AgentResult:
    agent_id: int
    init_ms: float | None = None
    list_ms: float | None = None
    tool_latencies_ms: list[float] = field(default_factory=list)
    tools_sampled: int = 0
    tools_available: int = 0
    http_errors: list[str] = field(default_factory=list)
    jsonrpc_errors: int = 0
    jsonrpc_ok: int = 0

    @property
    def ok(self) -> bool:
        return not self.http_errors and self.init_ms is not None and self.list_ms is not None


def normalize_proxy_base_url(url: str) -> str:
    """Accept origin or full MCP POST URL; return origin without trailing slash."""
    u = url.strip().rstrip("/")
    for suffix in ("/mcp-server/mcp", "/mcp-server"):
        if u.lower().endswith(suffix.lower()):
            u = u[: -len(suffix)].rstrip("/")
            break
    return u


def _mcp_url(base: str) -> str:
    return f"{normalize_proxy_base_url(base).rstrip('/')}{MCP_PATH}"


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
) -> httpx.Response:
    return await client.post(url, json=body, headers=headers)


async def run_one_agent(
    *,
    agent_id: int,
    url: str,
    api_key: str,
    num_tools: int,
    rng: random.Random,
    timeout_s: float,
) -> AgentResult:
    out = AgentResult(agent_id=agent_id)
    base_headers = {"X-API-Key": api_key}

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        t0 = time.perf_counter()
        r = await _post_json(client, url, headers=base_headers, body=INIT_BODY)
        out.init_ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            out.http_errors.append(f"initialize HTTP {r.status_code}: {r.text[:200]}")
            return out
        sid = r.headers.get("mcp-session-id")
        if not sid:
            out.http_errors.append("initialize missing mcp-session-id header")
            return out

        headers = {**base_headers, "mcp-session-id": sid}
        t1 = time.perf_counter()
        lr = await _post_json(
            client,
            url,
            headers=headers,
            body={"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
        )
        out.list_ms = (time.perf_counter() - t1) * 1000
        if lr.status_code != 200:
            out.http_errors.append(f"tools/list HTTP {lr.status_code}: {lr.text[:200]}")
            return out
        payload = lr.json()
        if "error" in payload:
            out.http_errors.append(f"tools/list JSON-RPC error: {payload.get('error')}")
            return out
        tools = (payload.get("result") or {}).get("tools") or []
        out.tools_available = len(tools)
        if not tools:
            return out

        k = min(num_tools, len(tools))
        chosen = rng.sample(tools, k=k)
        out.tools_sampled = k
        req_id = 3
        for t in chosen:
            name = t.get("name")
            if not name:
                continue
            t_call0 = time.perf_counter()
            cr = await _post_json(
                client,
                url,
                headers=headers,
                body={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": req_id,
                    "params": {"name": name, "arguments": {}},
                },
            )
            req_id += 1
            out.tool_latencies_ms.append((time.perf_counter() - t_call0) * 1000)
            if cr.status_code != 200:
                out.http_errors.append(f"tools/call HTTP {cr.status_code} ({name}): {cr.text[:120]}")
                continue
            cj = cr.json()
            if cj.get("error"):
                out.jsonrpc_errors += 1
            else:
                out.jsonrpc_ok += 1

    return out


def _percentile(xs: list[float], p: float) -> float | None:
    """Linear interpolation percentile, p in [0, 100]."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


async def run_smoke(
    *,
    base_url: str,
    api_key: str,
    num_agents: int,
    num_tools: int,
    seed: int | None,
    timeout_s: float,
) -> list[AgentResult]:
    url = _mcp_url(base_url)
    tasks = []
    for i in range(num_agents):
        if seed is not None:
            rng = random.Random(seed * 1_000_003 + i)
        else:
            rng = random.Random()
        tasks.append(
            run_one_agent(
                agent_id=i,
                url=url,
                api_key=api_key,
                num_tools=num_tools,
                rng=rng,
                timeout_s=timeout_s,
            )
        )
    return await asyncio.gather(*tasks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MCP_PROXY_BASE_URL", "http://127.0.0.1:8080"),
        help="Proxy origin (https://…run.app) or full …/mcp-server/mcp URL. Env: MCP_PROXY_BASE_URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("MCP_PROXY_API_KEY", ""),
        help="User MCP API key. Env: MCP_PROXY_API_KEY",
    )
    parser.add_argument("--agents", type=int, default=20, help="Concurrent agents (default 20)")
    parser.add_argument(
        "--tools",
        type=int,
        default=10,
        help="Random tools to call per agent after tools/list (default 10)",
    )
    parser.add_argument("--seed", type=int, default=None, help="RNG seed (optional, for reproducibility)")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout seconds (default 120)")
    parser.add_argument(
        "--min-tools",
        type=int,
        default=10,
        help="Warn if tools/list returns fewer than this many tools (default 10)",
    )
    args = parser.parse_args()

    if not args.api_key:
        print("Missing API key: set MCP_PROXY_API_KEY or pass --api-key", file=sys.stderr)
        return 2

    if args.agents < 1 or args.tools < 1:
        print("--agents and --tools must be >= 1", file=sys.stderr)
        return 2

    origin = normalize_proxy_base_url(args.base_url)
    url = _mcp_url(args.base_url)
    print(f"Proxy origin: {origin}")
    print(f"Target:      {url}")
    print(f"Agents: {args.agents}, random tools per agent: {args.tools}, seed={args.seed!r}")

    t0 = time.perf_counter()
    results = asyncio.run(
        run_smoke(
            base_url=origin,
            api_key=args.api_key,
            num_agents=args.agents,
            num_tools=args.tools,
            seed=args.seed,
            timeout_s=args.timeout,
        )
    )
    total_s = time.perf_counter() - t0

    ok_agents = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    all_call_latencies: list[float] = []
    for r in results:
        all_call_latencies.extend(r.tool_latencies_ms)

    total_rpc_ok = sum(r.jsonrpc_ok for r in results)
    total_rpc_err = sum(r.jsonrpc_errors for r in results)
    tools_avail = [r.tools_available for r in ok_agents]
    min_avail = min(tools_avail) if tools_avail else 0

    print()
    print("=== Summary ===")
    print(f"Wall time:           {total_s:.2f}s")
    print(f"Agents OK:           {len(ok_agents)}/{args.agents}")
    print(f"tools/list (min):   {min_avail} tools visible to an agent (among OK agents)")
    if min_avail < args.min_tools:
        print(
            f"  WARNING: expected at least {args.min_tools} tools for 10+ tool load; "
            "check upstreams and user permissions.",
            file=sys.stderr,
        )
    print(f"tools/call JSON-RPC: {total_rpc_ok} ok, {total_rpc_err} error (upstream may reject empty args)")
    if all_call_latencies:
        print(
            f"tools/call latency ms: mean={statistics.mean(all_call_latencies):.1f} "
            f"p50={_percentile(all_call_latencies, 50):.1f} "
            f"p95={_percentile(all_call_latencies, 95):.1f}"
        )
    if ok_agents:
        init_ms = [r.init_ms for r in ok_agents if r.init_ms is not None]
        lst_ms = [r.list_ms for r in ok_agents if r.list_ms is not None]
        if init_ms:
            print(f"initialize ms:      mean={statistics.mean(init_ms):.1f}")
        if lst_ms:
            print(f"tools/list ms:      mean={statistics.mean(lst_ms):.1f}")

    for r in bad[:5]:
        print(f"  Agent {r.agent_id} failed: {'; '.join(r.http_errors)}", file=sys.stderr)
    if len(bad) > 5:
        print(f"  ... and {len(bad) - 5} more failed agents", file=sys.stderr)

    if len(ok_agents) < args.agents:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
