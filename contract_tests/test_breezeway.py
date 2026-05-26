from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, call, patch

import pytest
from util import assert_error_dict, assert_success_dict, load_mcp_server


@pytest.fixture
def breezeway_mod():
    return load_mcp_server("breezeway-mcp")


@pytest.mark.asyncio
async def test_find_property_requires_query(breezeway_mod):
    r = await breezeway_mod.breezeway_find_property_by_name_or_external_id("", 10, 1, 100)
    assert_error_dict(r.structuredContent)
    assert r.structuredContent["error"] == "validation_error"


@pytest.mark.asyncio
async def test_get_reservation_by_external_id_success(breezeway_mod):
    """Happy-path: client returns a reservation dict — response must be a plain dict with no error key."""
    stub_reservation = {
        "id": 42,
        "external_reservation_id": "RES-TEST-001",
        "status": "confirmed",
        "check_in": "2026-06-01",
        "check_out": "2026-06-07",
        "property_id": 9,
    }

    with patch.object(breezeway_mod, "_get_client") as mock_gc:
        client = MagicMock()
        client.get_reservation_by_external_id.return_value = stub_reservation
        mock_gc.return_value = client
        r = await breezeway_mod.breezeway_get_reservation_by_external_id(
            external_reservation_id="RES-TEST-001",
            allow_multiple=False,
        )

    p = assert_success_dict(r.structuredContent)
    assert p.get("id") == 42
    assert p.get("external_reservation_id") == "RES-TEST-001"
    assert p.get("status") == "confirmed"
    client.get_reservation_by_external_id.assert_called_once_with("RES-TEST-001", False)


@pytest.mark.asyncio
async def test_get_reservation_by_external_id_allow_multiple(breezeway_mod):
    """allow_multiple flag is forwarded to the API client."""
    stub = {"id": 7, "external_reservation_id": "RES-MULTI-002", "status": "confirmed"}

    with patch.object(breezeway_mod, "_get_client") as mock_gc:
        client = MagicMock()
        client.get_reservation_by_external_id.return_value = stub
        mock_gc.return_value = client
        r = await breezeway_mod.breezeway_get_reservation_by_external_id(
            external_reservation_id="RES-MULTI-002",
            allow_multiple=True,
        )

    assert_success_dict(r.structuredContent)
    client.get_reservation_by_external_id.assert_called_once_with("RES-MULTI-002", True)


@pytest.mark.asyncio
async def test_list_tasks_requires_home_or_reference_property_id(breezeway_mod):
    r = await breezeway_mod.breezeway_list_tasks(page=1, limit=10)
    assert_error_dict(r.structuredContent)
    assert r.structuredContent["error"] == "validation_error"


@pytest.mark.asyncio
async def test_list_tasks_include_comments_hydrates_rows(breezeway_mod):
    page = {
        "results": [
            {"id": 101, "name": "Clean kitchen"},
            {"id": 202, "name": "Replace bulb"},
        ],
        "total_pages": 1,
        "total_results": 2,
    }
    with patch.object(breezeway_mod, "_get_client") as mock_gc:
        client = MagicMock()
        client.list_tasks_page.return_value = page
        client.get_task_comments.side_effect = lambda task_id: (
            [{"id": 1, "comment": "Started"}] if task_id == 101 else [{"id": 2, "comment": "Done"}]
        )
        mock_gc.return_value = client
        r = await breezeway_mod.breezeway_list_tasks(home_id=88, include_comments=True)

    p = assert_success_dict(r.structuredContent)
    assert p["count"] == 2
    assert p["include_comments"] is True
    assert p["data"][0]["comments"][0]["comment"] == "Started"
    assert p["data"][1]["comments"][0]["comment"] == "Done"
    client.list_tasks_page.assert_called_once()
    assert sorted(client.get_task_comments.call_args_list, key=str) == sorted([call(101), call(202)], key=str)


@pytest.mark.asyncio
async def test_triage_tasks_scans_portfolio_and_ranks_now_queue(breezeway_mod):
    properties_page = {
        "results": [
            {"id": 11, "name": "River Loft", "reference_property_id": "R-11", "city": "Porto", "status": "active"},
            {"id": 22, "name": "City Nest", "reference_property_id": "C-22", "city": "Porto", "status": "active"},
        ],
        "total_pages": 1,
        "total_results": 2,
    }
    tasks_by_home = {
        11: {
            "results": [
                {
                    "id": 101,
                    "name": "Fix leaking sink",
                    "home_id": 11,
                    "type_priority": "high",
                    "type_department": "maintenance",
                    "scheduled_date": "2026-04-19",
                    "created_at": "2026-04-18T08:00:00Z",
                    "updated_at": "2026-04-20T07:00:00Z",
                    "assignments": [],
                },
                {
                    "id": 102,
                    "name": "Check hallway lights",
                    "home_id": 11,
                    "type_priority": "medium",
                    "type_department": "inspection",
                    "scheduled_date": "2026-04-22",
                    "created_at": "2026-04-19T08:00:00Z",
                    "updated_at": "2026-04-20T07:00:00Z",
                    "assignments": [{"assignee_id": 7}],
                },
            ],
            "total_pages": 1,
            "total_results": 2,
        },
        22: {
            "results": [
                {
                    "id": 201,
                    "name": "Test smoke alarm",
                    "home_id": 22,
                    "type_priority": "urgent",
                    "type_department": "safety",
                    "scheduled_date": "2026-04-20",
                    "created_at": "2026-04-18T10:00:00Z",
                    "updated_at": "2026-04-20T08:30:00Z",
                    "assignments": [{"assignee_id": 7}],
                    "stage": "created",
                },
                {
                    "id": 202,
                    "name": "Completed deep clean",
                    "home_id": 22,
                    "type_priority": "high",
                    "type_department": "housekeeping",
                    "scheduled_date": "2026-04-20",
                    "finished_at": "2026-04-20T09:00:00Z",
                    "assignments": [{"assignee_id": 7}],
                },
            ],
            "total_pages": 1,
            "total_results": 2,
        },
    }

    with patch.object(breezeway_mod, "_get_client") as mock_gc:
        with patch.object(breezeway_mod, "_today", return_value=date(2026, 4, 20)):
            client = MagicMock()
            client.list_properties_page.return_value = properties_page
            client.list_all_users.return_value = [{"id": 7, "name": "Alice"}]
            client.list_tasks_page.side_effect = lambda *, page, limit, home_id=None, **kwargs: tasks_by_home[
                int(home_id)
            ]
            client.get_task_comments.side_effect = lambda task_id: [{"id": task_id, "comment": f"note {task_id}"}]
            mock_gc.return_value = client
            r = await breezeway_mod.breezeway_triage_tasks(limit=3, include_comments=True)

    p = assert_success_dict(r.structuredContent)
    assert p["count"] == 3
    assert [row["id"] for row in p["data"]] == [201, 101, 102]
    assert [row["id"] for row in p["recommended_now"]] == [201, 101]
    assert p["groups"][0]["key"] == "act_now"
    assert p["groups"][0]["task_ids"] == [201, 101]
    assert p["scan_summary"]["properties_scanned"] == 2
    assert p["scan_summary"]["tasks_scanned"] == 4
    assert p["scan_summary"]["open_tasks_considered"] == 3
    assert p["data"][0]["assignments"] == [{"id": 7, "name": "Alice"}]
    assert p["data"][1]["assignments"] == []
    assert p["data"][0]["latest_comment"] == "note 201"
    assert p["data"][1]["latest_comment"] == "note 101"
    client.list_properties_page.assert_called_once_with(1, 100)
    assert sorted(client.list_tasks_page.call_args_list, key=str) == sorted(
        [call(page=1, limit=100, home_id=11), call(page=1, limit=100, home_id=22)],
        key=str,
    )
    assert sorted(client.get_task_comments.call_args_list, key=str) == sorted(
        [call(101), call(201), call(102)], key=str
    )


@pytest.mark.asyncio
async def test_move_task_forwards_action(breezeway_mod):
    with patch.object(breezeway_mod, "_get_client") as mock_gc:
        client = MagicMock()
        client.move_task.return_value = {"status": "ok"}
        mock_gc.return_value = client
        r = await breezeway_mod.breezeway_move_task(task_id=77, action="approve")

    p = assert_success_dict(r.structuredContent)
    assert p["task_id"] == 77
    assert p["action"] == "approve"
    client.move_task.assert_called_once_with(77, "approve")


@pytest.mark.asyncio
async def test_update_task_forwards_payload(breezeway_mod):
    updates = breezeway_mod.BreezewayTaskUpdatePayload(name="Updated title", type_priority="high")
    with patch.object(breezeway_mod, "_get_client") as mock_gc:
        client = MagicMock()
        client.update_task.return_value = {"id": 12, "title": "Updated title"}
        mock_gc.return_value = client
        r = await breezeway_mod.breezeway_update_task(task_id=12, updates=updates)

    p = assert_success_dict(r.structuredContent)
    assert p["task_id"] == 12
    assert p["result"]["title"] == "Updated title"
    client.update_task.assert_called_once_with(12, {"name": "Updated title", "type_priority": "high"})
