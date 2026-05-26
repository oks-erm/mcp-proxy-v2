import unittest
from unittest.mock import AsyncMock, patch

from n8n_client import ensure_workflow_has_mcp_tag, tag_workflow


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, *, get_payloads):
        self._get_payloads = list(get_payloads)
        self.put_calls = []

    async def get(self, path):
        return _FakeResponse(self._get_payloads.pop(0))

    async def put(self, path, json):
        self.put_calls.append((path, json))
        return _FakeResponse({"ok": True})


class WorkflowTagPolicyTests(unittest.IsolatedAsyncioTestCase):
    @patch("n8n_client.ensure_tag_id", new_callable=AsyncMock)
    async def test_ensure_workflow_has_mcp_tag_adds_missing_tag(self, ensure_tag_id_mock):
        ensure_tag_id_mock.return_value = "42"
        client = _FakeClient(
            get_payloads=[
                {
                    "id": "wf1",
                    "tags": [
                        {"id": "7", "name": "existing"},
                        {"id": "42", "name": "mcp"},
                    ],
                }
            ]
        )

        refreshed = await ensure_workflow_has_mcp_tag(
            client,
            "wf1",
            workflow={"id": "wf1", "tags": [{"id": "7", "name": "existing"}]},
        )

        self.assertEqual("wf1", refreshed["id"])
        self.assertEqual("/workflows/wf1/tags", client.put_calls[0][0])
        self.assertEqual([{"id": "42"}, {"id": "7"}], client.put_calls[0][1])

    @patch("n8n_client.ensure_tag_id", new_callable=AsyncMock)
    async def test_tag_workflow_raises_if_mcp_missing_after_write(self, ensure_tag_id_mock):
        ensure_tag_id_mock.side_effect = ["42", "77"]
        client = _FakeClient(get_payloads=[{"id": "wf1", "tags": [{"id": "7", "name": "existing"}]}])

        with self.assertRaisesRegex(RuntimeError, "required 'mcp' tag"):
            await tag_workflow(
                client,
                "wf1",
                user_name="alice",
                is_update=False,
                workflow={"id": "wf1", "tags": [{"id": "7", "name": "existing"}]},
            )

    @patch("n8n_client.ensure_tag_id", new_callable=AsyncMock)
    async def test_tag_workflow_returns_refreshed_workflow_with_mcp(self, ensure_tag_id_mock):
        ensure_tag_id_mock.side_effect = ["42", "77"]
        client = _FakeClient(
            get_payloads=[
                {
                    "id": "wf1",
                    "tags": [
                        {"id": "7", "name": "existing"},
                        {"id": "42", "name": "mcp"},
                        {"id": "77", "name": "created_by:alice"},
                    ],
                }
            ]
        )

        refreshed = await tag_workflow(
            client,
            "wf1",
            user_name="alice",
            is_update=False,
            workflow={"id": "wf1", "tags": [{"id": "7", "name": "existing"}]},
        )

        self.assertEqual("wf1", refreshed["id"])
        self.assertEqual(
            [{"id": "42"}, {"id": "7"}, {"id": "77"}],
            client.put_calls[0][1],
        )


if __name__ == "__main__":
    unittest.main()
