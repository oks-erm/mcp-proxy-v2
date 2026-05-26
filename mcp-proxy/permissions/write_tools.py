"""Upstream MCP tool names that require Firestore permissions.write per server_id."""

from __future__ import annotations

from typing import Dict, FrozenSet

# Names as returned by upstream tools/list (before mcp-proxy prefixing).
_N8N_WRITE_TOOLS: FrozenSet[str] = frozenset(
    {
        "n8n_create_workflow",
        "n8n_delete_workflow",
        "n8n_update_workflow",
        "n8n_create_credential",
        "n8n_stop_execution",
        "n8n_retry_execution",
    }
)
_BREEZEWAY_WRITE_TOOLS: FrozenSet[str] = frozenset(
    {
        "breezeway_move_task",
        "breezeway_update_task",
    }
)
_CLOUD_RUN_DEPLOYER_WRITE_TOOLS: FrozenSet[str] = frozenset(
    {
        "start_source_upload",
        "upload_source_chunk",
        "finalize_source_upload",
        "deploy_app",
        "complete_deployment",
        "approve_app",
        "delete_app",
    }
)

# Firestore mcp_servers document id -> upstream tool names requiring write
UPSTREAM_WRITE_TOOLS_BY_SERVER: Dict[str, FrozenSet[str]] = {
    "n8n": _N8N_WRITE_TOOLS,
    "breezeway": _BREEZEWAY_WRITE_TOOLS,
    "cloud_run_deployer": _CLOUD_RUN_DEPLOYER_WRITE_TOOLS,
}


def upstream_tool_requires_write(server_id: str, upstream_tool_name: str) -> bool:
    """True if this upstream tool is classified as a write/mutate operation for server_id."""
    tools = UPSTREAM_WRITE_TOOLS_BY_SERVER.get(server_id)
    if not tools:
        # Match hyphens vs underscores in Firestore document id (e.g. cloud-run-deployer).
        norm = server_id.replace("-", "_")
        for key, fs in UPSTREAM_WRITE_TOOLS_BY_SERVER.items():
            if key.replace("-", "_") == norm:
                tools = fs
                break
    if not tools:
        return False
    return upstream_tool_name in tools
