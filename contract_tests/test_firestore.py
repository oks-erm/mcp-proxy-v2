from __future__ import annotations

from unittest.mock import patch

from util import assert_error_dict, load_mcp_server


def test_query_documents_denied_collection():
    mod = load_mcp_server("firestore-gateway")
    with patch.object(mod, "collection_access_error", return_value="blocked-by-policy"):
        r = mod.query_documents(collection="denied-test-coll")
    payload = assert_error_dict(r.structuredContent)
    assert payload["error"] == "access_denied"
