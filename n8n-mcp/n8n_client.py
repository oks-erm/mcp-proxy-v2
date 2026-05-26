"""n8n API client and helpers used by MCP tools."""

import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Set by main.py lifespan before MCP tools run
N8N_API_KEY: Optional[str] = None
N8N_ADMIN_API_KEY: Optional[str] = None
N8N_BASE_URL: str = os.environ.get("N8N_BASE_URL", "").rstrip("/")
GITHUB_TOKEN: Optional[str] = None

# Cache: tag name -> tag id (populated lazily; avoids repeated GET /tags calls)
_TAG_ID_CACHE: Dict[str, str] = {}

# GitHub node browser
N8N_NODES_REPO = "n8n-io/n8n"
N8N_NODES_PATH = "packages/nodes-base/nodes"
N8N_NODES_BRANCH = "master"
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"


def _build_client(api_key: Optional[str], missing_message: str) -> httpx.AsyncClient:
    """Return an HTTPX client for the given n8n API key, raising if configuration is missing."""
    missing = []
    if not N8N_BASE_URL:
        missing.append("N8N_BASE_URL (e.g. https://n8n.example.com/api/v1)")
    if not api_key:
        missing.append(missing_message)
    if missing:
        raise ValueError(
            "n8n API is not configured. Set: "
            + "; ".join(missing)
            + ". For Cloud Run: set repo secret N8N_BASE_URL and ensure the relevant n8n API secret exists."
        )
    headers = {
        "X-N8N-API-KEY": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    return httpx.AsyncClient(base_url=N8N_BASE_URL, headers=headers)


def get_client() -> httpx.AsyncClient:
    """Returns an async HTTPX client configured for the n8n API (workflows, executions, tags)."""
    return _build_client(
        N8N_API_KEY,
        "N8N_API_KEY (from Secret Manager or set N8N_API_KEY env var for local dev)",
    )


def get_admin_client() -> httpx.AsyncClient:
    """Returns an async HTTPX client configured with the admin API key (credential APIs)."""
    return _build_client(
        N8N_ADMIN_API_KEY,
        "N8N_ADMIN_API_KEY (from secret n8n-admin-api-key or N8N_ADMIN_API_KEY env var)",
    )


async def ensure_tag_id(tag_name: str) -> str:
    """Get or create a named tag in n8n and return its ID (results are cached).

    On the first miss the full tag list is fetched and the cache is populated for
    all existing tags, so subsequent calls for any tag name are cheap.
    """
    if tag_name in _TAG_ID_CACHE:
        return _TAG_ID_CACHE[tag_name]
    async with get_client() as client:
        response = await client.get("/tags")
        response.raise_for_status()
        data = response.json()
        tags = data if isinstance(data, list) else data.get("data", [])
        # Bulk-populate the cache from the full tag list
        for tag in tags:
            name = tag.get("name")
            tid = tag.get("id")
            if name and tid is not None:
                _TAG_ID_CACHE[str(name)] = str(tid)
        if tag_name in _TAG_ID_CACHE:
            return _TAG_ID_CACHE[tag_name]
        create_resp = await client.post("/tags", json={"name": tag_name})
        create_resp.raise_for_status()
        new_tag = create_resp.json()
        tag_id = str(new_tag.get("id"))
        _TAG_ID_CACHE[tag_name] = tag_id
        return tag_id


def _workflow_has_tag_named(workflow: Dict[str, Any], tag_name: str) -> bool:
    """Return whether a workflow payload contains a tag with the given name."""
    if not isinstance(workflow, dict):
        return False
    tags = workflow.get("tags", [])
    if not isinstance(tags, list):
        return False
    for tag in tags:
        if isinstance(tag, dict) and tag.get("name") == tag_name:
            return True
        if isinstance(tag, str) and tag == tag_name:
            return True
    return False


async def _get_workflow(client: httpx.AsyncClient, workflow_id: str) -> Dict[str, Any]:
    """Fetch one workflow and require a dict payload."""
    workflow_resp = await client.get(f"/workflows/{workflow_id}")
    workflow_resp.raise_for_status()
    workflow = workflow_resp.json()
    if not isinstance(workflow, dict):
        raise RuntimeError(f"n8n workflow {workflow_id} returned an unexpected payload shape")
    return workflow


async def _put_workflow_tags(
    client: httpx.AsyncClient,
    workflow_id: str,
    tag_ids: set[str],
) -> Dict[str, Any]:
    """Replace workflow tags, then refetch and verify the required mcp tag is present."""
    response = await client.put(
        f"/workflows/{workflow_id}/tags",
        json=[{"id": id_value} for id_value in sorted(tag_ids)],
    )
    response.raise_for_status()
    refreshed = await _get_workflow(client, workflow_id)
    if not _workflow_has_tag_named(refreshed, "mcp"):
        raise RuntimeError(f"Workflow {workflow_id} is missing the required 'mcp' tag after tag enforcement")
    return refreshed


async def ensure_workflow_has_mcp_tag(
    client: httpx.AsyncClient,
    workflow_id: str,
    workflow: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ensure a workflow already carries the required ``mcp`` tag and return the refreshed payload."""
    current = workflow if isinstance(workflow, dict) else await _get_workflow(client, workflow_id)
    if _workflow_has_tag_named(current, "mcp"):
        return current

    mcp_tag_id = await ensure_tag_id("mcp")
    existing_tag_ids: set[str] = set()
    tags = current.get("tags", [])
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and tag.get("id") is not None:
                existing_tag_ids.add(str(tag["id"]))
            elif isinstance(tag, str):
                existing_tag_ids.add(tag)
    existing_tag_ids.add(mcp_tag_id)
    return await _put_workflow_tags(client, workflow_id, existing_tag_ids)


async def tag_workflow(
    client: httpx.AsyncClient,
    workflow_id: str,
    user_name: Optional[str] = None,
    is_update: bool = False,
    workflow: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply attribution tags to a workflow without removing unrelated existing tags.

    Always ensures the ``mcp`` tag is present.

    When ``user_name`` is provided:
    - On **create** (``is_update=False``): adds a ``created_by:<user_name>`` tag.
    - On **update** (``is_update=True``): if the updater differs from the workflow's
      existing ``created_by:*`` owner, replaces any prior ``last_update:*`` tag with
      ``last_update:<user_name>``.  If the same user updates their own workflow, no
      extra tag is added.
    """
    mcp_tag_id = await ensure_tag_id("mcp")

    workflow = workflow if isinstance(workflow, dict) else await _get_workflow(client, workflow_id)
    tags = workflow.get("tags", [])

    existing_tag_ids: set[str] = set()
    creator_user: Optional[str] = None
    last_update_tag_ids: set[str] = set()

    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict):
                tag_id = str(tag["id"]) if tag.get("id") is not None else None
                tag_name_val: Optional[str] = tag.get("name")
                if tag_id:
                    existing_tag_ids.add(tag_id)
                if tag_name_val and tag_name_val.startswith("created_by:"):
                    creator_user = tag_name_val[len("created_by:") :]
                if tag_name_val and tag_name_val.startswith("last_update:") and tag_id:
                    last_update_tag_ids.add(tag_id)
            elif isinstance(tag, str):
                existing_tag_ids.add(tag)

    existing_tag_ids.add(mcp_tag_id)

    if user_name:
        clean_name = user_name.strip()
        if not is_update:
            created_by_id = await ensure_tag_id(f"created_by:{clean_name}")
            existing_tag_ids.add(created_by_id)
        else:
            if creator_user != clean_name:
                # Remove stale last_update:* tags and add the fresh one
                existing_tag_ids -= last_update_tag_ids
                last_update_id = await ensure_tag_id(f"last_update:{clean_name}")
                existing_tag_ids.add(last_update_id)

    return await _put_workflow_tags(client, workflow_id, existing_tag_ids)


def github_headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = GITHUB_TOKEN or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
