"""MCP Streamable HTTP integration tests (mounted at /mcp-server/mcp)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import httpx
import mcp_proxy
import pytest
import respx
from conftest import sample_user
from models import ServerConfig
from permissions.models import PermissionItem


@pytest.fixture(autouse=True)
def _grant_mcp_permissions_matching_enabled_servers():
    """Regular users need Firestore permissions; grant read+write for each enabled server from list_servers."""

    def side_effect(uid: str):
        return [
            PermissionItem(server_id=s.id, read=True, write=True) for s in mcp_proxy.list_servers(enabled_only=True)
        ]

    with patch("mcp_proxy.get_user_permissions", side_effect=side_effect):
        yield


MCP_PATH = "/mcp-server/mcp"
INIT_PARAMS = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "0.0.1"},
}
LOCAL_PROXY_TOOLS = [
    "mcp_proxy_list_skills",
    "mcp_proxy_install_skill",
    "mcp_proxy_check_skill_updates",
    "mcp_proxy_request_skill_update",
    "mcp_proxy_request_improvement",
    "mcp_proxy_set_app_secret",
    "mcp_proxy_list_app_secrets",
    "mcp_proxy_delete_app_secret",
]


def _post_mcp(client, headers: dict, body: dict):
    h = {"X-API-Key": "test-key", **headers}
    return client.post(MCP_PATH, json=body, headers=h)


def test_local_app_secret_tool_is_write_only_and_owner_scoped():
    user = sample_user(user_id="user-1", email="user@example.com")
    record = SimpleNamespace(app_id="owner-kpis", status="approved", created_by_user_id="user-1")
    secret = SimpleNamespace(
        app_id="owner-kpis",
        name="API_TOKEN",
        key_version="v1",
        created_by_user_id="user-1",
        updated_by_user_id="user-1",
        created_at=None,
        updated_at=None,
    )

    with patch("mcp_proxy.get_managed_app", return_value=record):
        with patch("mcp_proxy.set_app_secret", return_value=secret) as set_secret:
            result = mcp_proxy._handle_local_tool_call(
                req_id=1,
                name="mcp_proxy_set_app_secret",
                arguments={"app_id": "owner-kpis", "name": "api-token", "value": "plain-secret"},
                user=user,
            )

    structured = result["result"]["structuredContent"]
    assert structured["ok"] is True
    assert structured["secret"]["name"] == "API_TOKEN"
    assert "plain-secret" not in json.dumps(structured)
    set_secret.assert_called_once()
    assert set_secret.call_args.kwargs["value"] == "plain-secret"


def test_local_app_secret_tool_rejects_non_owner():
    user = sample_user(user_id="user-2", email="user2@example.com")
    record = SimpleNamespace(app_id="owner-kpis", status="approved", created_by_user_id="user-1")

    with patch("mcp_proxy.get_managed_app", return_value=record):
        with pytest.raises(ValueError, match="Not permitted"):
            mcp_proxy._handle_local_tool_call(
                req_id=1,
                name="mcp_proxy_delete_app_secret",
                arguments={"app_id": "owner-kpis", "name": "API_TOKEN"},
                user=user,
            )


def test_mcp_post_no_api_key(client):
    r = client.post(MCP_PATH, json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
    assert r.status_code == 401


def test_mcp_post_invalid_api_key(client):
    with patch("mcp_proxy.resolve_user_by_key", return_value=None):
        r = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
    assert r.status_code == 401


def test_mcp_post_invalid_json(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        r = client.post(
            MCP_PATH,
            content=b"{not json",
            headers={"Content-Type": "application/json", "X-API-Key": "k"},
        )
    assert r.status_code == 400
    data = r.json()
    assert data.get("error", {}).get("code") == -32700


def test_mcp_initialize_no_upstream_servers(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            r = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["serverInfo"]["name"] == "mcp-proxy"
    assert body["result"]["capabilities"].get("resources") == {}
    assert body["result"]["capabilities"].get("tools") == {}
    assert "mcp_proxy://help/index" in body["result"]["instructions"]
    assert "mcp_proxy_check_skill_updates" in body["result"]["instructions"]
    assert "mcp-session-id" in r.headers


def test_mcp_initialize_mentions_n8n_bootstrap_when_allowed(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            r = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
    assert r.status_code == 200
    instructions = r.json()["result"]["instructions"]
    assert "mcp_proxy://help/n8n" in instructions
    assert "mcp_proxy://help/by-task/n8n-create-workflow" in instructions
    assert "mcp_proxy_n8n_workflow_assistant" in instructions


def test_mcp_initialize_mentions_investigation_bootstrap_when_allowed(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["guesty"]):
            r = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
    assert r.status_code == 200
    instructions = r.json()["result"]["instructions"]
    assert "mcp_proxy_investigation_assistant" in instructions


def test_mcp_initialize_rejects_existing_session_header(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            r = _post_mcp(
                client,
                {"mcp-session-id": "550e8400-e29b-41d4-a716-446655440000"},
                {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS},
            )
    assert r.status_code == 400
    assert "re-initialize" in r.json()["detail"].lower()


def test_mcp_method_requires_session_after_init(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            r = _post_mcp(
                client,
                {},
                {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
            )
    assert r.status_code == 400
    assert "mcp-session-id" in r.json()["detail"].lower()


def test_mcp_unknown_method(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            r = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "experimental/unknown", "id": 3, "params": {}},
            )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == -32601


def test_mcp_tools_list_empty_session(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            r = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
            )
    assert r.status_code == 200
    names = [tool["name"] for tool in r.json()["result"]["tools"]]
    assert names == LOCAL_PROXY_TOOLS


def test_mcp_local_improvement_tool_schema_avoids_top_level_combinators(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            r = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
            )
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    improvement_tool = next(tool for tool in tools if tool["name"] == "mcp_proxy_request_improvement")
    schema = improvement_tool["inputSchema"]
    assert schema["type"] == "object"
    assert "anyOf" not in schema
    assert "oneOf" not in schema
    assert "allOf" not in schema
    assert "enum" not in schema
    assert "not" not in schema


def test_mcp_local_list_skills_tool_returns_catalog(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            tr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 2,
                    "params": {"name": "mcp_proxy_list_skills", "arguments": {}},
                },
            )
    assert tr.status_code == 200
    payload = tr.json()["result"]["structuredContent"]
    assert payload["count"] >= 1
    assert payload["check_updates_tool"] == "mcp_proxy_check_skill_updates"
    assert any(skill["name"] == "host-wise-n8n-workflows" for skill in payload["skills"])
    assert any(skill["name"] == "host-wise-managed-apps" for skill in payload["skills"])


def test_mcp_local_install_skill_tool_returns_manifest(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            tr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 2,
                    "params": {
                        "name": "mcp_proxy_install_skill",
                        "arguments": {"skill_name": "host-wise-n8n-workflows"},
                    },
                },
            )
    assert tr.status_code == 200
    payload = tr.json()["result"]["structuredContent"]
    assert payload["name"] == "host-wise-n8n-workflows"
    assert payload["version"]
    assert payload["content_sha256"]
    assert payload["bundle_url"].endswith("/skills/catalog/host-wise-n8n-workflows.tar.gz")
    assert payload["archive_sha256"]


def test_mcp_local_check_skill_updates_tool_reports_outdated_skill(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            tr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 2,
                    "params": {
                        "name": "mcp_proxy_check_skill_updates",
                        "arguments": {
                            "installed": [
                                {
                                    "name": "host-wise-n8n-workflows",
                                    "version": "old-version",
                                    "content_sha256": "deadbeef",
                                }
                            ]
                        },
                    },
                },
            )
    assert tr.status_code == 200
    payload = tr.json()["result"]["structuredContent"]
    assert payload["outdated_count"] == 1
    skill = payload["skills"][0]
    assert skill["name"] == "host-wise-n8n-workflows"
    assert skill["status"] == "outdated"
    assert skill["outdated"] is True
    assert skill["reason"] == "content_sha256_mismatch"
    assert skill["latest"]["content_sha256"]


def test_mcp_local_skill_update_request_submits_request(client):
    u = sample_user()
    mocked_request = MagicMock(
        id="skill-req-1",
        skill_name="host-wise-n8n-workflows",
        status="pending",
        summary="Add more examples",
        created_at=None,
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            with patch("mcp_proxy.create_skill_update_request", return_value=mocked_request) as create_req:
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                tr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "id": 2,
                        "params": {
                            "name": "mcp_proxy_request_skill_update",
                            "arguments": {
                                "skill_name": "host-wise-n8n-workflows",
                                "summary": "Add more examples",
                            },
                        },
                    },
                )
    assert tr.status_code == 200
    payload = tr.json()["result"]["structuredContent"]
    assert payload["request_id"] == "skill-req-1"
    create_req.assert_called_once()


def test_mcp_local_improvement_tool_submits_request(client):
    u = sample_user()
    mocked_request = MagicMock(
        id="imp-1",
        status="pending",
        summary="Add output schema",
        tool_names=["guesty_find_listing"],
        server_ids=["guesty"],
        created_at=None,
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["guesty"]):
            with patch(
                "mcp_proxy.list_servers", return_value=[ServerConfig(id="guesty", url="https://g", enabled=True)]
            ):
                with patch("mcp_proxy.create_improvement_request", return_value=mocked_request) as create_req:
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 2,
                            "params": {
                                "name": "mcp_proxy_request_improvement",
                                "arguments": {
                                    "summary": "Add output schema",
                                    "tool_names": ["guesty_find_listing"],
                                    "error_context": {
                                        "failed_tool_name": "guesty_find_listing",
                                        "error_message": "Missing field x",
                                    },
                                },
                            },
                        },
                    )
    assert tr.status_code == 200
    payload = tr.json()["result"]["structuredContent"]
    assert payload["request_id"] == "imp-1"
    assert payload["server_ids"] == ["guesty"]
    create_req.assert_called_once()


def test_mcp_local_improvement_tool_rate_limited(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["guesty"]):
            with patch(
                "mcp_proxy.list_servers", return_value=[ServerConfig(id="guesty", url="https://g", enabled=True)]
            ):
                with patch(
                    "mcp_proxy.create_improvement_request",
                    side_effect=mcp_proxy.ImprovementRequestRateLimitError("retry later", retry_after_seconds=30),
                ):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 2,
                            "params": {
                                "name": "mcp_proxy_request_improvement",
                                "arguments": {
                                    "summary": "retry",
                                    "server_ids": ["guesty"],
                                },
                            },
                        },
                    )
    assert tr.status_code == 200
    assert tr.json()["error"]["code"] == -32003
    assert "retry later" in tr.json()["error"]["message"]


@respx.mock
def test_mcp_initialize_and_tools_list_with_upstream(client):
    u = sample_user()
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="my-srv", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "up", "version": "1"},
        },
    }
    tools_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"tools": [{"name": "echo", "description": "d"}]},
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "up-sess"}),
            httpx.Response(200, json=tools_body, headers={"Mcp-Session-Id": "up-sess"}),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["my-srv"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                lr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
                )
    assert ir.status_code == 200
    assert "mcp_proxy://help/capabilities.json" in ir.json()["result"]["instructions"]
    assert lr.status_code == 200
    tools = lr.json()["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == [*LOCAL_PROXY_TOOLS, "my_srv_echo"]


@respx.mock
def test_mcp_tools_list_loads_permissions_once_for_many_tools(client):
    """Regular user tools/list must call get_user_permissions once, not once per tool."""
    u = sample_user()
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="my-srv", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "up", "version": "1"},
        },
    }
    tools_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "tools": [
                {"name": "a", "description": "1"},
                {"name": "b", "description": "2"},
                {"name": "c", "description": "3"},
            ]
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "up-sess"}),
            httpx.Response(200, json=tools_body, headers={"Mcp-Session-Id": "up-sess"}),
        ]
    )
    get_perms = MagicMock(return_value=[PermissionItem(server_id="my-srv", read=True, write=True)])
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["my-srv"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch("mcp_proxy.get_user_permissions", get_perms):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    lr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
                    )
    assert ir.status_code == 200
    assert lr.status_code == 200
    assert get_perms.call_count == 1
    assert get_perms.call_args_list == [call(u.id)]
    names = [tool["name"] for tool in lr.json()["result"]["tools"]]
    assert names == [
        *LOCAL_PROXY_TOOLS,
        "my_srv_a",
        "my_srv_b",
        "my_srv_c",
    ]


@respx.mock
def test_mcp_tools_list_enriches_n8n_tool_descriptions(client):
    u = sample_user()
    url = "https://upstream-n8n.test/mcp"
    srv = ServerConfig(id="n8n", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "n8n"}},
    }
    tools_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "tools": [
                {"name": "n8n_create_workflow", "description": "Create a workflow."},
                {"name": "n8n_get_workflows", "description": "List workflows."},
            ]
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}),
            httpx.Response(200, json=tools_body, headers={"Mcp-Session-Id": "s-n8n"}),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                lr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
                )
    assert lr.status_code == 200
    tools = {tool["name"]: tool for tool in lr.json()["result"]["tools"]}
    assert "mcp_proxy://help/n8n" in tools["n8n_create_workflow"]["description"]
    assert "mcp_proxy_n8n_workflow_assistant" in tools["n8n_create_workflow"]["description"]
    assert "mcp_proxy://help/tool/n8n_get_workflows" in tools["n8n_get_workflows"]["description"]


@respx.mock
def test_mcp_tools_list_includes_reservation_ticket_alias(client):
    u = sample_user()
    url = "https://upstream-zendesk.test/mcp"
    srv = ServerConfig(id="zendesk", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "zendesk"}},
    }
    tools_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "tools": [
                {
                    "name": "zendesk_find_ticket_by_reservation_id",
                    "description": "Resolve ticket candidates from a reservation id.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"reservation_id": {"type": "string"}},
                        "required": ["reservation_id"],
                    },
                }
            ]
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-zd"}),
            httpx.Response(200, json=tools_body, headers={"Mcp-Session-Id": "s-zd"}),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["zendesk"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                lr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
                )
    assert lr.status_code == 200
    tools = {tool["name"]: tool for tool in lr.json()["result"]["tools"]}
    assert "zendesk_find_ticket_by_reservation_id" in tools
    assert "find_ticket_by_reservation_id" in tools
    assert "Alias of `zendesk_find_ticket_by_reservation_id`" in tools["find_ticket_by_reservation_id"]["description"]
    assert tools["find_ticket_by_reservation_id"]["inputSchema"]["required"] == ["reservation_id"]


@respx.mock
def test_mcp_tools_call_unknown_prefixed_name(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            with patch("mcp_proxy.list_servers", return_value=[]):
                r = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "id": 4,
                        "params": {"name": "nope_tool", "arguments": {}},
                    },
                )
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32602


@respx.mock
def test_mcp_tools_call_upstream_ok(client):
    u = sample_user()
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="ab-cd", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "up", "version": "1"},
        },
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {"content": [{"type": "text", "text": "ok"}]},
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s1"}),
            httpx.Response(200, json=call_body),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["ab-cd"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                tr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "id": 5,
                        "params": {"name": "ab_cd_echo", "arguments": {}},
                    },
                )
    assert tr.status_code == 200
    assert "result" in tr.json()


@respx.mock
def test_mcp_tools_call_reservation_ticket_alias_routes_to_canonical_tool(client):
    u = sample_user()
    url = "https://upstream-zendesk.test/mcp"
    srv = ServerConfig(id="zendesk", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "zendesk", "version": "1"},
        },
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {"structuredContent": {"count": 1, "data": [{"id": 42}]}},
    }
    route = respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-zd"}),
            httpx.Response(200, json=call_body),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["zendesk"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                tr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "id": 5,
                        "params": {
                            "name": "find_ticket_by_reservation_id",
                            "arguments": {"reservation_id": "ABC-123"},
                        },
                    },
                )
    assert tr.status_code == 200
    forwarded_body = json.loads(route.calls[1].request.content)
    assert forwarded_body["params"]["name"] == "zendesk_find_ticket_by_reservation_id"
    assert forwarded_body["params"]["arguments"] == {"reservation_id": "ABC-123"}
    assert "result" in tr.json()


@respx.mock
def test_mcp_tools_call_logs_successful_usage(client):
    u = sample_user(email="analyst@example.com")
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="ab-cd", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "up", "version": "1"},
        },
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {"content": [{"type": "text", "text": "ok"}]},
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s1"}),
            httpx.Response(200, json=call_body),
        ]
    )
    with patch("mcp_proxy.log_audit") as audit_mock:
        with patch("mcp_proxy.resolve_user_by_key", return_value=u):
            with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["ab-cd"]):
                with patch("mcp_proxy.list_servers", return_value=[srv]):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {"name": "ab_cd_echo", "arguments": {"foo": "bar", "count": 1}},
                        },
                    )
    assert tr.status_code == 200
    tool_calls = [c.kwargs for c in audit_mock.call_args_list if c.args and c.args[0] == "mcp_tool_call"]
    assert len(tool_calls) == 1
    logged = tool_calls[0]
    assert logged["user_id"] == u.id
    assert logged["user_email"] == u.email
    assert logged["tool_name"] == "ab_cd_echo"
    assert logged["upstream_tool_name"] == "echo"
    assert logged["server_id"] == "ab-cd"
    assert logged["result"] == "success"
    assert logged["reason"] == "completed"
    assert logged["argument_keys"] == ["count", "foo"]
    assert logged["argument_count"] == 2
    assert logged["write_tool"] is False
    assert isinstance(logged["latency_ms"], int)
    assert logged["latency_ms"] >= 0


@respx.mock
def test_mcp_tools_call_upstream_error(client):
    u = sample_user()
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="x", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "u"}},
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s1"}),
            httpx.Response(502, text="bad gateway"),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["x"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                tr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "id": 6,
                        "params": {"name": "x_ping", "arguments": {}},
                    },
                )
    assert tr.status_code == 200
    err = tr.json()["error"]
    assert err["code"] == -32603
    assert "data" in err


@respx.mock
def test_mcp_tools_call_logs_transport_error_usage(client):
    u = sample_user()
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="x", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "u"}},
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s1"}),
            httpx.Response(502, text="bad gateway"),
        ]
    )
    with patch("mcp_proxy.log_audit") as audit_mock:
        with patch("mcp_proxy.resolve_user_by_key", return_value=u):
            with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["x"]):
                with patch("mcp_proxy.list_servers", return_value=[srv]):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 6,
                            "params": {"name": "x_ping", "arguments": {"probe": True}},
                        },
                    )
    assert tr.status_code == 200
    tool_calls = [c.kwargs for c in audit_mock.call_args_list if c.args and c.args[0] == "mcp_tool_call"]
    assert len(tool_calls) == 1
    logged = tool_calls[0]
    assert logged["tool_name"] == "x_ping"
    assert logged["upstream_tool_name"] == "ping"
    assert logged["server_id"] == "x"
    assert logged["result"] == "error"
    assert logged["reason"] == "upstream_transport_error"
    assert logged["error_code"] == -32603
    assert logged["upstream_error_detail_present"] is True
    assert logged["argument_keys"] == ["probe"]


def test_mcp_resources_read_unknown_uri(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            with patch("mcp_proxy.list_servers", return_value=[]):
                r = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {"jsonrpc": "2.0", "method": "resources/read", "id": 7, "params": {"uri": "unknown://x"}},
                )
    assert r.json()["error"]["code"] == -32602


def test_mcp_help_resources_list_and_read(client):
    u = sample_user(role="admin")
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            lr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "resources/list", "id": 2, "params": {}},
            )
    assert lr.status_code == 200
    uris = [x["uri"] for x in lr.json()["result"]["resources"]]
    assert "mcp_proxy://help/index" in uris
    assert "mcp_proxy://help/capabilities.json" in uris
    assert "mcp_proxy://help/guesty" in uris
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        rr = _post_mcp(
            client,
            {"mcp-session-id": sid},
            {
                "jsonrpc": "2.0",
                "method": "resources/read",
                "id": 3,
                "params": {"uri": "mcp_proxy://help/index"},
            },
        )
    assert rr.status_code == 200
    text = rr.json()["result"]["contents"][0]["text"]
    assert "MCP proxy help index" in text
    assert "Verb ladder" in text
    assert "mcp_proxy_request_improvement" in text
    assert "mcp_proxy_list_skills" in text

    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        jr = _post_mcp(
            client,
            {"mcp-session-id": sid},
            {
                "jsonrpc": "2.0",
                "method": "resources/read",
                "id": 4,
                "params": {"uri": "mcp_proxy://help/capabilities.json"},
            },
        )
    assert jr.status_code == 200
    cap = json.loads(jr.json()["result"]["contents"][0]["text"])
    assert "domains" in cap
    assert "guesty" in cap["domains"]
    assert cap["proxy_features"]["improvement_request_tool"] == "mcp_proxy_request_improvement"
    assert cap["proxy_features"]["skill_catalog_tools"]["list"] == "mcp_proxy_list_skills"

    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        bad = _post_mcp(
            client,
            {"mcp-session-id": sid},
            {
                "jsonrpc": "2.0",
                "method": "resources/read",
                "id": 5,
                "params": {"uri": "mcp_proxy://help/no-such-help-page"},
            },
        )
    assert bad.json()["error"]["code"] == -32602


def test_mcp_help_discoverability_respects_permissions(client):
    """Help list, capabilities JSON, and template blurbs reflect the same server_id scope as tools/list."""
    u = sample_user(role="user")
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            lr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "resources/list", "id": 2, "params": {}},
            )
    assert lr.status_code == 200
    uris = [x["uri"] for x in lr.json()["result"]["resources"]]
    assert "mcp_proxy://help/n8n" in uris
    assert "mcp_proxy://help/guesty" not in uris
    idx_meta = next(x for x in lr.json()["result"]["resources"] if x["uri"] == "mcp_proxy://help/index")
    assert "permission" in idx_meta["description"].lower()

    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            rr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {
                    "jsonrpc": "2.0",
                    "method": "resources/read",
                    "id": 3,
                    "params": {"uri": "mcp_proxy://help/capabilities.json"},
                },
            )
    assert rr.status_code == 200
    cap = json.loads(rr.json()["result"]["contents"][0]["text"])
    assert cap["access"]["help_matches_tools_list"] is True
    assert "guesty" in cap["access"]["hidden_domain_keys"]
    assert "n8n" in cap["access"]["visible_domain_keys"]
    assert "guesty" not in cap.get("domains", {})
    assert cap["proxy_features"]["improvement_request_tool"] == "mcp_proxy_request_improvement"
    assert cap["proxy_features"]["skill_catalog_tools"]["install"] == "mcp_proxy_install_skill"
    assert cap["proxy_features"]["workflow_guidance"]["n8n"]["prompts"] == [
        "mcp_proxy_n8n_workflow_assistant",
        "mcp_proxy_n8n_workflow_rules",
    ]

    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            tr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "resources/templates/list", "id": 4, "params": {}},
            )
    assert tr.status_code == 200
    descs = [x.get("description") or "" for x in tr.json()["result"]["resourceTemplates"]]
    assert any("tools/list" in d for d in descs)


def test_mcp_help_resource_templates_list(client):
    u = sample_user(role="admin")
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            tr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "resources/templates/list", "id": 2, "params": {}},
            )
    assert tr.status_code == 200
    body = tr.json()["result"]
    assert "resourceTemplates" in body
    ut = [x.get("uriTemplate") for x in body["resourceTemplates"]]
    assert "mcp_proxy://help/tool/{tool_name}" in ut
    assert "mcp_proxy://help/workflow/{domain}/{goal}" in ut


def test_mcp_help_tool_uri_via_read(client):
    u = sample_user(role="admin")
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            rr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {
                    "jsonrpc": "2.0",
                    "method": "resources/read",
                    "id": 3,
                    "params": {"uri": "mcp_proxy://help/tool/guesty_find_listing"},
                },
            )
    assert rr.status_code == 200
    text = rr.json()["result"]["contents"][0]["text"]
    assert "guesty_find_listing" in text
    assert "Required inputs" in text


def test_mcp_prompts_get_unknown_name(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            with patch("mcp_proxy.list_servers", return_value=[]):
                r = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {"jsonrpc": "2.0", "method": "prompts/get", "id": 8, "params": {"name": "no_such_prompt"}},
                )
    assert r.json()["error"]["code"] == -32602


def test_mcp_prompts_list_includes_local_n8n_guidance(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            pr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "prompts/list", "id": 2, "params": {}},
            )
    assert pr.status_code == 200
    names = [x["name"] for x in pr.json()["result"]["prompts"]]
    assert "mcp_proxy_n8n_workflow_assistant" in names
    assert "mcp_proxy_n8n_workflow_rules" in names


def test_mcp_prompts_list_includes_local_investigation_guidance(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["guesty"]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            pr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "prompts/list", "id": 2, "params": {}},
            )
    assert pr.status_code == 200
    names = [x["name"] for x in pr.json()["result"]["prompts"]]
    assert "mcp_proxy_investigation_assistant" in names
    assert "mcp_proxy_investigation_rules" in names


def test_mcp_prompts_get_local_n8n_guidance(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            pr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {
                    "jsonrpc": "2.0",
                    "method": "prompts/get",
                    "id": 2,
                    "params": {"name": "mcp_proxy_n8n_workflow_assistant"},
                },
            )
    assert pr.status_code == 200
    result = pr.json()["result"]
    assert result["description"]
    text = result["messages"][0]["content"]["text"]
    assert "mcp_proxy://help/n8n" in text
    assert "mcp_proxy://help/by-task/n8n-create-workflow" in text
    assert "canonical Host Wise guidance" in text


def test_mcp_prompts_get_local_investigation_guidance(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["guesty", "zendesk", "breezeway", "sql"]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            pr = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {
                    "jsonrpc": "2.0",
                    "method": "prompts/get",
                    "id": 2,
                    "params": {"name": "mcp_proxy_investigation_assistant"},
                },
            )
    assert pr.status_code == 200
    result = pr.json()["result"]
    assert result["description"]
    text = result["messages"][0]["content"]["text"]
    assert "mcp_proxy://help/by-task/ops-guest-complaint-triage" in text
    assert "mcp_proxy://help/by-task/vacancy-diagnosis" in text
    assert "canonical Host Wise investigation guidance" in text


@respx.mock
def test_mcp_resources_list_merge(client):
    u = sample_user()
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="r1", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {}},
    }
    list_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"resources": [{"uri": "file:///a", "name": "a"}]},
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s1"}),
            httpx.Response(200, json=list_body),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["r1"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                rr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {"jsonrpc": "2.0", "method": "resources/list", "id": 2, "params": {}},
                )
    assert rr.status_code == 200
    uris = [x["uri"] for x in rr.json()["result"]["resources"]]
    # _prefix_resource turns file:///a into r1://file//a (first :// collapsed)
    assert "r1://file//a" in uris
    assert "mcp_proxy://help/index" in uris
    assert "mcp_proxy://help/capabilities.json" in uris


@respx.mock
def test_mcp_prompts_list_merge(client):
    u = sample_user()
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="p1", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {}},
    }
    list_body = {"jsonrpc": "2.0", "id": 2, "result": {"prompts": [{"name": "summarize", "description": "d"}]}}
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s1"}),
            httpx.Response(200, json=list_body),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["p1"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                pr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {"jsonrpc": "2.0", "method": "prompts/list", "id": 2, "params": {}},
                )
    assert pr.status_code == 200
    names = [x["name"] for x in pr.json()["result"]["prompts"]]
    assert names == ["p1_summarize"]


@respx.mock
def test_mcp_prompts_list_no_double_prefix_for_n8n_self_named_prompts(client):
    u = sample_user()
    url = "https://upstream-n8n.test/mcp"
    srv = ServerConfig(id="n8n", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "n8n"}},
    }
    list_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"prompts": [{"name": "n8n_workflow_assistant", "description": "d"}]},
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}),
            httpx.Response(200, json=list_body),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                pr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {"jsonrpc": "2.0", "method": "prompts/list", "id": 2, "params": {}},
                )
    assert pr.status_code == 200
    names = [x["name"] for x in pr.json()["result"]["prompts"]]
    assert names == [
        "mcp_proxy_n8n_workflow_assistant",
        "mcp_proxy_n8n_workflow_rules",
        "n8n_workflow_assistant",
    ]


def test_mcp_notification_returns_202(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            r = _post_mcp(
                client,
                {"mcp-session-id": sid},
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
    assert r.status_code == 202


def test_mcp_get_sse_no_api_key(client):
    r = client.get(MCP_PATH, headers={"mcp-session-id": "x"})
    assert r.status_code == 401


def test_mcp_get_sse_no_session_header(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        r = client.get(MCP_PATH, headers={"X-API-Key": "k"})
    assert r.status_code == 400


def test_mcp_get_sse_unknown_session(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        r = client.get(MCP_PATH, headers={"X-API-Key": "k", "mcp-session-id": "00000000-0000-0000-0000-000000000099"})
    assert r.status_code == 404


def test_mcp_get_sse_wrong_user(client):
    owner = sample_user(user_id="owner-1")
    other = sample_user(user_id="other-1", email="o@e.com")
    fake_sid = "11111111-1111-1111-1111-111111111111"
    mcp_proxy._sessions[fake_sid] = {"user_id": owner.id, "servers": {}}
    try:
        with patch("mcp_proxy.resolve_user_by_key", return_value=other):
            r = client.get(MCP_PATH, headers={"X-API-Key": "k", "mcp-session-id": fake_sid})
        assert r.status_code == 403
    finally:
        mcp_proxy._sessions.pop(fake_sid, None)


def test_mcp_get_sse_ok(client):
    u = sample_user()
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=[]):
            ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
            sid = ir.headers["mcp-session-id"]
            r = client.get(MCP_PATH, headers={"X-API-Key": "k", "mcp-session-id": sid})
    assert r.status_code == 200
    assert r.headers.get("mcp-session-id") == sid
    assert "text/event-stream" in r.headers.get("content-type", "")


@respx.mock
def test_mcp_tools_list_hides_n8n_write_tools_without_write_perm(client):
    u = sample_user()
    url = "https://upstream-n8n.test/mcp"
    srv = ServerConfig(id="n8n", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "n8n"}},
    }
    tools_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "tools": [
                {"name": "n8n_get_workflows", "description": "r"},
                {"name": "n8n_create_workflow", "description": "w"},
            ]
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}),
            httpx.Response(200, json=tools_body, headers={"Mcp-Session-Id": "s-n8n"}),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch(
                    "mcp_proxy.get_user_permissions",
                    return_value=[PermissionItem(server_id="n8n", read=True, write=False)],
                ):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    lr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
                    )
    assert lr.status_code == 200
    names = [t["name"] for t in lr.json()["result"]["tools"]]
    assert names == [*LOCAL_PROXY_TOOLS, "n8n_get_workflows"]


@respx.mock
def test_mcp_tools_call_denies_n8n_write_without_write_perm(client):
    u = sample_user()
    url = "https://upstream-n8n.test/mcp"
    srv = ServerConfig(id="n8n", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "n8n"}},
    }
    respx.post(url).mock(return_value=httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}))
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch(
                    "mcp_proxy.get_user_permissions",
                    return_value=[PermissionItem(server_id="n8n", read=True, write=False)],
                ):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {
                                "name": "n8n_create_workflow",
                                "arguments": {"name": "Demo", "summary": "Short summary"},
                            },
                        },
                    )
    assert tr.status_code == 200
    err = tr.json()["error"]
    assert err["code"] == -32003


@respx.mock
def test_mcp_tools_call_allows_n8n_write_with_write_perm(client):
    u = sample_user()
    url = "https://upstream-n8n.test/mcp"
    srv = ServerConfig(id="n8n", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "n8n"}},
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {"content": [{"type": "text", "text": "created"}]},
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}),
            httpx.Response(200, json=call_body),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch(
                    "mcp_proxy.get_user_permissions",
                    return_value=[PermissionItem(server_id="n8n", read=True, write=True)],
                ):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {
                                "name": "n8n_create_workflow",
                                "arguments": {"name": "Demo", "summary": "Short summary"},
                            },
                        },
                    )
    assert tr.status_code == 200
    assert "result" in tr.json()


@respx.mock
def test_mcp_create_workflow_persists_managed_workflow_metadata(client):
    u = sample_user(user_id="user-1", email="user@example.com")
    url = "https://upstream-n8n.test/mcp"
    srv = ServerConfig(id="n8n", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "n8n"}},
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {
            "structuredContent": {
                "id": "wf-1",
                "name": "Demo",
                "editor_url": "https://n8n.example.com/workflow/wf-1",
            }
        },
    }
    route = respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}),
            httpx.Response(200, json=call_body),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch("mcp_proxy.upsert_managed_workflow") as upsert_record:
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {
                                "name": "n8n_create_workflow",
                                "arguments": {
                                    "name": "Demo",
                                    "summary": "Short summary",
                                },
                            },
                        },
                    )
    assert tr.status_code == 200
    forwarded_body = json.loads(route.calls[1].request.content)
    assert forwarded_body["params"]["arguments"]["user_name"] == "user"
    upsert_record.assert_called_once()
    assert upsert_record.call_args.kwargs["workflow_id"] == "wf-1"
    assert upsert_record.call_args.kwargs["summary"] == "Short summary"


@respx.mock
def test_mcp_create_workflow_rejects_empty_summary_before_upstream(client):
    u = sample_user(role="admin")
    url = "https://upstream-n8n.test/mcp"
    srv = ServerConfig(id="n8n", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "n8n"}},
    }
    route = respx.post(url).mock(return_value=httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}))
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                tr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "id": 5,
                        "params": {"name": "n8n_create_workflow", "arguments": {"name": "Demo", "summary": "   "}},
                    },
                )
    assert tr.status_code == 200
    assert tr.json()["error"]["code"] == -32602
    assert "non-empty `summary`" in tr.json()["error"]["message"]
    assert len(route.calls) == 1


@respx.mock
def test_mcp_deploy_app_persists_metadata_when_firestore_server_id_uses_hyphens(client):
    """Sync must run for cloud-run-deployer, not only cloud_run_deployer (document id in Firestore)."""
    u = sample_user(user_id="user-1", email="user@example.com")
    url = "https://deployer.test/mcp"
    srv = ServerConfig(id="cloud-run-deployer", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "deployer"}},
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {
            "structuredContent": {
                "ok": True,
                "executed": True,
                "app": {
                    "app_id": "owner-kpis",
                    "name": "Owner KPIs",
                    "summary": "Shows owner KPIs.",
                    "data_access_summary": "Reads aggregated reservations.",
                    "data_connections": [{"id": "warehouse", "type": "none"}],
                    "service_name": "app-owner-kpis",
                    "service_url": "https://app-owner-kpis.run.app",
                    "project_id": "test-gcp-project",
                    "region": "europe-west1",
                    "runtime_service_account": "dashboard-runtime-sa@test-gcp-project.iam.gserviceaccount.com",
                },
            }
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-deployer"}),
            httpx.Response(200, json=call_body),
        ]
    )
    manifest = {
        "app_id": "owner-kpis",
        "name": "Owner KPIs",
        "summary": "Shows owner KPIs.",
        "data_access_summary": "Reads aggregated reservations.",
    }
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["cloud-run-deployer"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch("mcp_proxy.upsert_managed_app") as upsert_app:
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {
                                "name": "cloud_run_deployer_deploy_app",
                                "arguments": {"manifest": manifest, "execute": True},
                            },
                        },
                    )
    assert tr.status_code == 200
    upsert_app.assert_called_once()


@respx.mock
def test_mcp_deploy_app_persists_managed_app_metadata(client):
    u = sample_user(user_id="user-1", email="user@example.com")
    url = "https://deployer.test/mcp"
    srv = ServerConfig(id="cloud_run_deployer", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "deployer"}},
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {
            "structuredContent": {
                "ok": True,
                "executed": True,
                "app": {
                    "app_id": "owner-kpis",
                    "name": "Owner KPIs",
                    "summary": "Shows owner KPIs.",
                    "data_access_summary": "Reads aggregated reservations.",
                    "data_connections": [{"id": "warehouse", "type": "none"}],
                    "service_name": "app-owner-kpis",
                    "service_url": "https://app-owner-kpis.run.app",
                    "project_id": "test-gcp-project",
                    "region": "europe-west1",
                    "runtime_service_account": "dashboard-runtime-sa@test-gcp-project.iam.gserviceaccount.com",
                },
            }
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-deployer"}),
            httpx.Response(200, json=call_body),
        ]
    )
    manifest = {
        "app_id": "owner-kpis",
        "name": "Owner KPIs",
        "summary": "Shows owner KPIs.",
        "data_access_summary": "Reads aggregated reservations.",
    }
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["cloud_run_deployer"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch("mcp_proxy.upsert_managed_app") as upsert_app:
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {
                                "name": "cloud_run_deployer_deploy_app",
                                "arguments": {"manifest": manifest, "execute": True},
                            },
                        },
                    )
    assert tr.status_code == 200
    upsert_app.assert_called_once()
    assert upsert_app.call_args.kwargs["app_id"] == "owner-kpis"
    assert upsert_app.call_args.kwargs["data_access_summary"] == "Reads aggregated reservations."


@respx.mock
def test_mcp_deploy_app_rejects_empty_summaries_before_upstream(client):
    u = sample_user(role="admin")
    url = "https://deployer.test/mcp"
    srv = ServerConfig(id="cloud_run_deployer", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "deployer"}},
    }
    route = respx.post(url).mock(
        return_value=httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-deployer"})
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["cloud_run_deployer"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                tr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "id": 5,
                        "params": {
                            "name": "cloud_run_deployer_deploy_app",
                            "arguments": {
                                "manifest": {"app_id": "owner-kpis", "summary": "", "data_access_summary": ""}
                            },
                        },
                    },
                )
    assert tr.status_code == 200
    assert tr.json()["error"]["code"] == -32602
    assert "manifest.summary" in tr.json()["error"]["message"]
    assert len(route.calls) == 1


@respx.mock
def test_mcp_deploy_app_dry_run_does_not_persist_managed_app(client):
    u = sample_user(user_id="user-1", email="user@example.com")
    url = "https://deployer.test/mcp"
    srv = ServerConfig(id="cloud_run_deployer", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "deployer"}},
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {
            "structuredContent": {
                "ok": True,
                "executed": False,
                "app": {
                    "app_id": "owner-kpis",
                    "summary": "Shows owner KPIs.",
                    "data_access_summary": "Reads aggregated reservations.",
                    "service_name": "app-owner-kpis",
                    "project_id": "test-gcp-project",
                    "region": "europe-west1",
                },
            }
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-deployer"}),
            httpx.Response(200, json=call_body),
        ]
    )
    manifest = {
        "app_id": "owner-kpis",
        "summary": "Shows owner KPIs.",
        "data_access_summary": "Reads aggregated reservations.",
    }
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["cloud_run_deployer"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch("mcp_proxy.upsert_managed_app") as upsert_app:
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {"name": "cloud_run_deployer_deploy_app", "arguments": {"manifest": manifest}},
                        },
                    )
    assert tr.status_code == 200
    upsert_app.assert_not_called()


@respx.mock
def test_mcp_async_build_submission_persists_managed_app_pending_until_complete(client):
    u = sample_user(user_id="user-1", email="user@example.com")
    url = "https://deployer.test/mcp"
    srv = ServerConfig(id="cloud_run_deployer", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "deployer"}},
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {
            "structuredContent": {
                "ok": True,
                "executed": True,
                "async_build": True,
                "service_deployed": False,
                "build_submitted": True,
                "build_id": "11111111-1111-1111-1111-111111111111",
                "app": {
                    "app_id": "owner-kpis",
                    "summary": "Shows owner KPIs.",
                    "data_access_summary": "Reads aggregated reservations.",
                    "service_name": "app-owner-kpis",
                    "project_id": "test-gcp-project",
                    "region": "europe-west1",
                },
            }
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-deployer"}),
            httpx.Response(200, json=call_body),
        ]
    )
    manifest = {
        "app_id": "owner-kpis",
        "summary": "Shows owner KPIs.",
        "data_access_summary": "Reads aggregated reservations.",
    }
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["cloud_run_deployer"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch("mcp_proxy.upsert_managed_app") as upsert_app:
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {
                                "name": "cloud_run_deployer_deploy_app",
                                "arguments": {"manifest": manifest, "execute": True, "async_build": True},
                            },
                        },
                    )
    assert tr.status_code == 200
    upsert_app.assert_called_once()
    call_kw = upsert_app.call_args.kwargs
    assert call_kw["app_id"] == "owner-kpis"
    assert call_kw["build_id"] == "11111111-1111-1111-1111-111111111111"
    assert call_kw["service_name"] == "app-owner-kpis"


@respx.mock
def test_mcp_partial_deploy_failure_still_persists_managed_app_metadata(client):
    u = sample_user(user_id="user-1", email="user@example.com")
    url = "https://deployer.test/mcp"
    srv = ServerConfig(id="cloud_run_deployer", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "deployer"}},
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {
            "structuredContent": {
                "ok": False,
                "executed": True,
                "service_deployed": True,
                "partial_failure": True,
                "error": "command_failed",
                "details": "PERMISSION_DENIED: run.services.getIamPolicy",
                "app": {
                    "app_id": "owner-kpis",
                    "name": "Owner KPIs",
                    "summary": "Shows owner KPIs.",
                    "data_access_summary": "Reads aggregated reservations.",
                    "data_connections": [{"id": "warehouse", "type": "none"}],
                    "service_name": "app-owner-kpis",
                    "service_url": "https://app-owner-kpis.run.app",
                    "project_id": "test-gcp-project",
                    "region": "europe-west1",
                    "runtime_service_account": "dashboard-runtime-sa@test-gcp-project.iam.gserviceaccount.com",
                },
            }
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-deployer"}),
            httpx.Response(200, json=call_body),
        ]
    )
    manifest = {
        "app_id": "owner-kpis",
        "name": "Owner KPIs",
        "summary": "Shows owner KPIs.",
        "data_access_summary": "Reads aggregated reservations.",
    }
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["cloud_run_deployer"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch("mcp_proxy.upsert_managed_app") as upsert_app:
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {
                                "name": "cloud_run_deployer_complete_deployment",
                                "arguments": {"manifest": manifest, "execute": True},
                            },
                        },
                    )
    assert tr.status_code == 200
    upsert_app.assert_called_once()
    assert upsert_app.call_args.kwargs["app_id"] == "owner-kpis"


@respx.mock
def test_mcp_update_workflow_rejects_schedule_under_two_hours_before_upstream(client):
    u = sample_user(role="admin")
    url = "https://upstream-n8n.test/mcp"
    srv = ServerConfig(id="n8n", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "n8n"}},
    }
    route = respx.post(url).mock(return_value=httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}))
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                tr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "id": 5,
                        "params": {
                            "name": "n8n_update_workflow",
                            "arguments": {
                                "workflow_id": "wf-1",
                                "nodes": [
                                    {
                                        "id": "sched-1",
                                        "name": "Schedule",
                                        "type": "n8n-nodes-base.scheduleTrigger",
                                        "typeVersion": 1,
                                        "position": [0, 0],
                                        "parameters": {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}},
                                    }
                                ],
                            },
                        },
                    },
                )
    assert tr.status_code == 200
    assert tr.json()["error"]["code"] == -32602
    assert "under 2 hours" in tr.json()["error"]["message"]
    assert len(route.calls) == 1


def test_mcp_tools_call_admin_bypasses_write_perm_check(client):
    u = sample_user(role="admin")
    url = "https://upstream-n8n.test/mcp"
    srv = ServerConfig(id="n8n", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "n8n"}},
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {"content": [{"type": "text", "text": "ok"}]},
    }
    with respx.mock:
        respx.post(url).mock(
            side_effect=[
                httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-n8n"}),
                httpx.Response(200, json=call_body),
            ]
        )
        with patch("mcp_proxy.resolve_user_by_key", return_value=u):
            with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["n8n"]):
                with patch("mcp_proxy.list_servers", return_value=[srv]):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {
                                "name": "n8n_create_workflow",
                                "arguments": {"name": "Demo", "summary": "Short summary"},
                            },
                        },
                    )
    assert tr.status_code == 200
    assert "result" in tr.json()


@respx.mock
def test_mcp_tools_list_hides_breezeway_write_tools_without_write_perm(client):
    u = sample_user()
    url = "https://upstream-breezeway.test/mcp"
    srv = ServerConfig(id="breezeway", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "breezeway"}},
    }
    tools_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "tools": [
                {"name": "breezeway_list_tasks", "description": "r"},
                {"name": "breezeway_move_task", "description": "w"},
            ]
        },
    }
    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-bz"}),
            httpx.Response(200, json=tools_body, headers={"Mcp-Session-Id": "s-bz"}),
        ]
    )
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["breezeway"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch(
                    "mcp_proxy.get_user_permissions",
                    return_value=[PermissionItem(server_id="breezeway", read=True, write=False)],
                ):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    lr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
                    )
    assert lr.status_code == 200
    names = [t["name"] for t in lr.json()["result"]["tools"]]
    assert names == [*LOCAL_PROXY_TOOLS, "breezeway_list_tasks"]


@respx.mock
def test_mcp_tools_call_denies_breezeway_write_without_write_perm(client):
    u = sample_user()
    url = "https://upstream-breezeway.test/mcp"
    srv = ServerConfig(id="breezeway", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "breezeway"}},
    }
    respx.post(url).mock(return_value=httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s-bz"}))
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["breezeway"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                with patch(
                    "mcp_proxy.get_user_permissions",
                    return_value=[PermissionItem(server_id="breezeway", read=True, write=False)],
                ):
                    ir = _post_mcp(
                        client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS}
                    )
                    sid = ir.headers["mcp-session-id"]
                    tr = _post_mcp(
                        client,
                        {"mcp-session-id": sid},
                        {
                            "jsonrpc": "2.0",
                            "method": "tools/call",
                            "id": 5,
                            "params": {"name": "breezeway_move_task", "arguments": {"task_id": 1, "action": "close"}},
                        },
                    )
    assert tr.status_code == 200
    err = tr.json()["error"]
    assert err["code"] == -32003


@respx.mock
def test_mcp_initialize_strips_resource_subscription_capabilities(client):
    u = sample_user()
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="c1", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"resources": {"subscribe": True, "listChanged": True, "other": True}},
            "serverInfo": {"name": "u"},
        },
    }
    respx.post(url).mock(return_value=httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s1"}))
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["c1"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                r = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
    caps = r.json()["result"]["capabilities"]
    assert caps.get("resources") == {"other": True}


@respx.mock
def test_mcp_resources_subscribe_forwards_stripped_uri(client):
    u = sample_user()
    url = "https://upstream-mcp.test/mcp"
    srv = ServerConfig(id="r1", url=url, enabled=True)
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {}},
    }
    sub_ok = {"jsonrpc": "2.0", "id": 3, "result": {}}
    captured = {}

    def on_request(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "resources/subscribe":
            captured["uri"] = body.get("params", {}).get("uri")
        if body.get("method") == "initialize":
            return httpx.Response(200, json=init_body, headers={"Mcp-Session-Id": "s1"})
        if body.get("method") == "resources/subscribe":
            return httpx.Response(200, json=sub_ok)
        return httpx.Response(400, text="unexpected")

    respx.post(url).mock(side_effect=on_request)
    with patch("mcp_proxy.resolve_user_by_key", return_value=u):
        with patch("mcp_proxy.resolve_allowed_server_ids", return_value=["r1"]):
            with patch("mcp_proxy.list_servers", return_value=[srv]):
                ir = _post_mcp(client, {}, {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": INIT_PARAMS})
                sid = ir.headers["mcp-session-id"]
                sr = _post_mcp(
                    client,
                    {"mcp-session-id": sid},
                    {
                        "jsonrpc": "2.0",
                        "method": "resources/subscribe",
                        "id": 3,
                        "params": {"uri": "r1://file//a"},
                    },
                )
    assert sr.status_code == 200
    assert sr.json()["result"] == {}
    assert captured.get("uri") == "file:///a"
