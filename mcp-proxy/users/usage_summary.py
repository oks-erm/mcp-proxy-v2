"""Build recent usage summaries from Firestore-backed proxy aggregates."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from api.audit_urls import log_explorer_user_activity_url
from users import store as users_store
from users.schemas import (
    UsageBreakdownItem,
    UsageResultCounts,
    UserInDB,
    UserUsageLeaderboard,
    UserUsageRankingItem,
    UserUsageSummary,
)
from users.usage_store import (
    list_usage_ranking_rows,
    list_user_usage_servers,
    list_user_usage_tools,
    list_user_usage_totals,
)

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
TOP_N_BREAKDOWNS = 5
TOP_N_USERS = 10
_TOOL_RESULTS = frozenset({"success", "error", "denied"})


class UserUsageSummaryUnavailableError(RuntimeError):
    """Raised when recent usage data cannot be queried."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(timestamp: datetime) -> str:
    return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _window_bounds(*, now: datetime, window_days: int) -> tuple[str, str]:
    end = _day_key(now)
    start = _day_key(now - timedelta(days=window_days - 1))
    return start, end


def _result_counts_dict() -> dict[str, int]:
    return {"success": 0, "error": 0, "denied": 0}


def _record_dict(record: Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    to_dict = getattr(record, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, Mapping):
            return data
    return {}


def _coerce_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _merge_last_activity(current: Optional[datetime], candidate: Any) -> Optional[datetime]:
    dt = _coerce_timestamp(candidate)
    if dt is None:
        return current
    if current is None or dt > current:
        return dt
    return current


def _extract_result_counts(data: Mapping[str, Any]) -> dict[str, int]:
    counts = _result_counts_dict()
    nested = data.get("result_counts")
    if isinstance(nested, Mapping):
        for key in _TOOL_RESULTS:
            counts[key] += int(nested.get(key) or 0)
    for key in _TOOL_RESULTS:
        legacy_key = f"result_counts.{key}"
        if legacy_key in data:
            counts[key] += int(data.get(legacy_key) or 0)
    return counts


def _rank_breakdowns(
    rows: Mapping[str, dict[str, Any]],
    *,
    top_n: int,
) -> list[UsageBreakdownItem]:
    ranked = sorted(rows.items(), key=lambda item: (-int(item[1]["total"]), item[0].lower()))[:top_n]
    return [
        UsageBreakdownItem(
            name=name,
            total=int(payload["total"]),
            result_counts=UsageResultCounts(**payload["result_counts"]),
        )
        for name, payload in ranked
    ]


def summarize_user_usage_records(
    *,
    user_id: str,
    totals: Iterable[Any],
    servers: Iterable[Any],
    tools: Iterable[Any],
    window_days: int = WINDOW_DAYS,
    top_n: int = TOP_N_BREAKDOWNS,
) -> UserUsageSummary:
    """Aggregate daily Firestore usage rows into one recent per-user summary."""
    authorized_request_count = 0
    tool_call_count = 0
    result_counts = _result_counts_dict()
    last_activity_at: Optional[datetime] = None

    for row in totals:
        data = _record_dict(row)
        authorized_request_count += int(data.get("authorized_request_count") or 0)
        tool_call_count += int(data.get("tool_call_count") or 0)
        raw_counts = _extract_result_counts(data)
        for key in _TOOL_RESULTS:
            result_counts[key] += int(raw_counts.get(key) or 0)
        last_activity_at = _merge_last_activity(last_activity_at, data.get("last_activity_at"))

    server_totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "result_counts": _result_counts_dict()})
    for row in servers:
        data = _record_dict(row)
        name = str(data.get("server_id") or "").strip()
        if not name:
            continue
        server_totals[name]["total"] += int(data.get("tool_call_count") or 0)
        raw_counts = _extract_result_counts(data)
        for key in _TOOL_RESULTS:
            server_totals[name]["result_counts"][key] += int(raw_counts.get(key) or 0)
        last_activity_at = _merge_last_activity(last_activity_at, data.get("last_activity_at"))

    tool_totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "result_counts": _result_counts_dict()})
    for row in tools:
        data = _record_dict(row)
        name = str(data.get("tool_name") or "").strip()
        if not name:
            continue
        tool_totals[name]["total"] += int(data.get("tool_call_count") or 0)
        raw_counts = _extract_result_counts(data)
        for key in _TOOL_RESULTS:
            tool_totals[name]["result_counts"][key] += int(raw_counts.get(key) or 0)
        last_activity_at = _merge_last_activity(last_activity_at, data.get("last_activity_at"))

    return UserUsageSummary(
        window_days=window_days,
        authorized_request_count=authorized_request_count,
        tool_call_count=tool_call_count,
        result_counts=UsageResultCounts(**result_counts),
        last_activity_at=last_activity_at,
        unique_servers_count=len(server_totals),
        unique_tools_count=len(tool_totals),
        top_servers=_rank_breakdowns(server_totals, top_n=top_n),
        top_tools=_rank_breakdowns(tool_totals, top_n=top_n),
        log_explorer_url=log_explorer_user_activity_url(user_id),
        truncated=False,
    )


def summarize_usage_ranking_records(
    rows: Iterable[Any],
    *,
    users: Iterable[UserInDB],
    window_days: int = WINDOW_DAYS,
    top_n: int = TOP_N_USERS,
) -> UserUsageLeaderboard:
    """Aggregate daily ranking rows into a recent per-user leaderboard."""
    by_user: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "tool_call_count": 0,
            "authorized_request_count": 0,
            "result_counts": _result_counts_dict(),
            "last_activity_at": None,
        }
    )
    for row in rows:
        data = _record_dict(row)
        user_id = str(data.get("user_id") or "").strip()
        if not user_id:
            continue
        acc = by_user[user_id]
        acc["tool_call_count"] += int(data.get("tool_call_count") or 0)
        acc["authorized_request_count"] += int(data.get("authorized_request_count") or 0)
        raw_counts = _extract_result_counts(data)
        for key in _TOOL_RESULTS:
            acc["result_counts"][key] += int(raw_counts.get(key) or 0)
        acc["last_activity_at"] = _merge_last_activity(acc["last_activity_at"], data.get("last_activity_at"))

    known_users = {user.id: user for user in users}
    ranked = sorted(
        by_user.items(),
        key=lambda item: (-int(item[1]["tool_call_count"]), -int(item[1]["authorized_request_count"]), item[0]),
    )[:top_n]
    out: list[UserUsageRankingItem] = []
    for user_id, acc in ranked:
        user = known_users.get(user_id)
        if user and user.kind == "agent":
            identity_label = user.agent_name or "Service agent"
        elif user and user.email:
            identity_label = user.email
        else:
            identity_label = user_id
        out.append(
            UserUsageRankingItem(
                user_id=user_id,
                identity_label=identity_label,
                kind=(user.kind if user else "human"),
                email=(user.email if user else ""),
                tool_call_count=int(acc["tool_call_count"]),
                authorized_request_count=int(acc["authorized_request_count"]),
                result_counts=UsageResultCounts(**acc["result_counts"]),
                last_activity_at=acc["last_activity_at"],
                log_explorer_url=log_explorer_user_activity_url(user_id),
            )
        )
    return UserUsageLeaderboard(window_days=window_days, users=out, truncated=False)


def get_user_usage_summary(
    user_id: str,
    *,
    now: Optional[datetime] = None,
    window_days: int = WINDOW_DAYS,
    top_n: int = TOP_N_BREAKDOWNS,
) -> UserUsageSummary:
    """Return a recent per-user usage summary from Firestore aggregates."""
    current_time = now.astimezone(timezone.utc) if now else _utc_now()
    start_day, end_day = _window_bounds(now=current_time, window_days=window_days)
    try:
        return summarize_user_usage_records(
            user_id=user_id,
            totals=list_user_usage_totals(user_id, start_day=start_day, end_day=end_day),
            servers=list_user_usage_servers(user_id, start_day=start_day, end_day=end_day),
            tools=list_user_usage_tools(user_id, start_day=start_day, end_day=end_day),
            window_days=window_days,
            top_n=top_n,
        )
    except Exception as exc:
        logger.exception("Failed to build user usage summary user_id=%s", user_id)
        raise UserUsageSummaryUnavailableError(f"Usage summary query failed: {exc}") from exc


def get_usage_leaderboard(
    *,
    now: Optional[datetime] = None,
    window_days: int = WINDOW_DAYS,
    top_n: int = TOP_N_USERS,
    users: Optional[Iterable[UserInDB]] = None,
) -> UserUsageLeaderboard:
    """Return a ranked recent usage leaderboard from Firestore aggregates."""
    current_time = now.astimezone(timezone.utc) if now else _utc_now()
    start_day, end_day = _window_bounds(now=current_time, window_days=window_days)
    try:
        users_list = list(users) if users is not None else users_store.list_users()
        return summarize_usage_ranking_records(
            list_usage_ranking_rows(start_day=start_day, end_day=end_day),
            users=users_list,
            window_days=window_days,
            top_n=top_n,
        )
    except Exception as exc:
        logger.exception("Failed to build usage leaderboard")
        raise UserUsageSummaryUnavailableError(f"Usage ranking query failed: {exc}") from exc
