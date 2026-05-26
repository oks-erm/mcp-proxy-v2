import unittest
from unittest.mock import AsyncMock, patch

import mcp_server
import n8n_client


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self):
        self.post_calls = []
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, path, json=None):
        self.post_calls.append((path, json))
        if path == "/workflows":
            return _FakeResponse(
                {
                    "id": "wf-1",
                    "name": json["name"],
                    "nodes": json["nodes"],
                    "connections": json["connections"],
                    "settings": json["settings"],
                }
            )
        return _FakeResponse({})

    async def get(self, path):
        self.get_calls.append(path)
        return _FakeResponse({"id": "wf-1"})


class WorkflowCreateTests(unittest.IsolatedAsyncioTestCase):
    @patch("mcp_server.tag_workflow", new_callable=AsyncMock)
    @patch("mcp_server.get_client")
    async def test_create_workflow_injects_summary_sticky_note(self, get_client_mock, tag_workflow_mock):
        fake_client = _FakeClient()
        get_client_mock.return_value = fake_client

        async def _tagged_workflow(*args, **kwargs):
            return {
                "id": "wf-1",
                "name": "Demo",
                "nodes": fake_client.post_calls[0][1]["nodes"],
                "connections": {},
                "settings": {"executionOrder": "v1"},
            }

        tag_workflow_mock.side_effect = _tagged_workflow

        original_base = n8n_client.N8N_BASE_URL
        n8n_client.N8N_BASE_URL = "https://n8n.example.com/api/v1"
        try:
            result = await mcp_server.n8n_create_workflow(
                name="Demo",
                summary="Sync new leads into HubSpot.",
                nodes=[
                    {
                        "id": "start",
                        "name": "Start",
                        "type": "n8n-nodes-base.manualTrigger",
                        "typeVersion": 1,
                        "position": [200, 120],
                        "parameters": {},
                    }
                ],
                connections={},
            )
        finally:
            n8n_client.N8N_BASE_URL = original_base

        payload = result.structuredContent
        self.assertEqual("wf-1", payload["id"])
        self.assertEqual("Sync new leads into HubSpot.", payload["summary"])
        self.assertEqual("https://n8n.example.com/workflow/wf-1", payload["editor_url"])
        created_nodes = fake_client.post_calls[0][1]["nodes"]
        self.assertEqual("n8n-nodes-base.stickyNote", created_nodes[0]["type"])
        self.assertIn("Workflow Summary", created_nodes[0]["name"])
        self.assertIn("Sync new leads into HubSpot.", created_nodes[0]["parameters"]["content"])


if __name__ == "__main__":
    unittest.main()
