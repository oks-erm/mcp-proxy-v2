from datetime import datetime, timezone

from users.schemas import UserInDB
from users.usage_summary import (
    summarize_usage_ranking_records,
    summarize_user_usage_records,
)


def test_summarize_user_usage_records_aggregates_daily_totals_and_breakdowns():
    totals = [
        {
            "date": "2026-04-21",
            "authorized_request_count": 2,
            "tool_call_count": 3,
            "result_counts": {"success": 2, "error": 1},
            "last_activity_at": datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
        },
        {
            "date": "2026-04-22",
            "authorized_request_count": 1,
            "tool_call_count": 2,
            "result_counts": {"success": 1, "denied": 1},
            "last_activity_at": datetime(2026, 4, 22, 9, 0, tzinfo=timezone.utc),
        },
    ]
    servers = [
        {
            "date": "2026-04-21",
            "server_id": "guesty",
            "tool_call_count": 2,
            "result_counts": {"success": 1, "error": 1},
        },
        {
            "date": "2026-04-22",
            "server_id": "guesty",
            "tool_call_count": 1,
            "result_counts": {"success": 1},
        },
        {
            "date": "2026-04-22",
            "server_id": "n8n",
            "tool_call_count": 2,
            "result_counts": {"success": 1, "denied": 1},
        },
    ]
    tools = [
        {
            "date": "2026-04-21",
            "tool_name": "guesty_find_listing",
            "tool_call_count": 2,
            "result_counts": {"success": 1, "error": 1},
        },
        {
            "date": "2026-04-22",
            "tool_name": "n8n_create_workflow",
            "tool_call_count": 1,
            "result_counts": {"denied": 1},
        },
        {
            "date": "2026-04-22",
            "tool_name": "n8n_get_workflow",
            "tool_call_count": 1,
            "result_counts": {"success": 1},
        },
        {
            "date": "2026-04-22",
            "tool_name": "guesty_find_listing",
            "tool_call_count": 1,
            "result_counts": {"success": 1},
        },
    ]

    summary = summarize_user_usage_records(
        user_id="user-1",
        totals=totals,
        servers=servers,
        tools=tools,
        window_days=30,
    )

    assert summary.window_days == 30
    assert summary.authorized_request_count == 3
    assert summary.tool_call_count == 5
    assert summary.result_counts.success == 3
    assert summary.result_counts.error == 1
    assert summary.result_counts.denied == 1
    assert summary.unique_servers_count == 2
    assert summary.unique_tools_count == 3
    assert summary.last_activity_at == datetime(2026, 4, 22, 9, 0, tzinfo=timezone.utc)
    assert [item.name for item in summary.top_servers] == ["guesty", "n8n"]
    assert summary.top_servers[0].total == 3
    assert summary.top_tools[0].name == "guesty_find_listing"
    assert summary.top_tools[0].total == 3
    assert summary.truncated is False
    assert "user-1" in summary.log_explorer_url


def test_summarize_user_usage_records_handles_empty_data():
    summary = summarize_user_usage_records(
        user_id="user-1",
        totals=[],
        servers=[],
        tools=[],
    )

    assert summary.authorized_request_count == 0
    assert summary.tool_call_count == 0
    assert summary.unique_servers_count == 0
    assert summary.unique_tools_count == 0
    assert summary.last_activity_at is None
    assert summary.top_servers == []
    assert summary.top_tools == []


def test_summarize_user_usage_records_reads_legacy_flat_result_count_keys():
    summary = summarize_user_usage_records(
        user_id="user-1",
        totals=[
            {
                "date": "2026-04-22",
                "authorized_request_count": 1,
                "tool_call_count": 2,
                "result_counts.success": 1,
                "result_counts.error": 1,
            }
        ],
        servers=[
            {
                "date": "2026-04-22",
                "server_id": "guesty",
                "tool_call_count": 2,
                "result_counts.success": 1,
                "result_counts.error": 1,
            }
        ],
        tools=[
            {
                "date": "2026-04-22",
                "tool_name": "guesty_find_listing",
                "tool_call_count": 2,
                "result_counts.success": 1,
                "result_counts.error": 1,
            }
        ],
    )

    assert summary.result_counts.success == 1
    assert summary.result_counts.error == 1
    assert summary.top_servers[0].result_counts.success == 1
    assert summary.top_tools[0].result_counts.error == 1


def test_summarize_usage_ranking_records_aggregates_per_user():
    users = [
        UserInDB(
            id="human-1",
            email="person@example.com",
            kind="human",
            role="user",
            status="active",
        ),
        UserInDB(
            id="agent-1",
            email="",
            kind="agent",
            agent_name="Sync Bot",
            role="user",
            status="active",
        ),
    ]
    rows = [
        {
            "date": "2026-04-21",
            "user_id": "human-1",
            "authorized_request_count": 2,
            "tool_call_count": 1,
            "result_counts": {"success": 1},
            "last_activity_at": datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
        },
        {
            "date": "2026-04-22",
            "user_id": "agent-1",
            "authorized_request_count": 1,
            "tool_call_count": 2,
            "result_counts": {"success": 1, "error": 1},
            "last_activity_at": datetime(2026, 4, 22, 9, 0, tzinfo=timezone.utc),
        },
        {
            "date": "2026-04-22",
            "user_id": "human-1",
            "authorized_request_count": 1,
            "tool_call_count": 1,
            "result_counts": {"denied": 1},
            "last_activity_at": datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc),
        },
    ]

    leaderboard = summarize_usage_ranking_records(rows, users=users, window_days=30)

    assert leaderboard.window_days == 30
    assert leaderboard.truncated is False
    assert [row.user_id for row in leaderboard.users] == ["human-1", "agent-1"]
    assert leaderboard.users[0].identity_label == "person@example.com"
    assert leaderboard.users[0].authorized_request_count == 3
    assert leaderboard.users[0].result_counts.denied == 1
    assert leaderboard.users[1].identity_label == "Sync Bot"
    assert leaderboard.users[1].tool_call_count == 2
    assert leaderboard.users[1].result_counts.success == 1
    assert leaderboard.users[1].result_counts.error == 1
