"""Firestore-backed usage aggregates for MCP proxy activity."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Iterable, Mapping

from gcp_firestore import get_client
from google.cloud import firestore

logger = logging.getLogger(__name__)

_TOTALS_SUBCOLL = "usage_daily_totals"
_SERVERS_SUBCOLL = "usage_daily_servers"
_TOOLS_SUBCOLL = "usage_daily_tools"
_RANKING_COLL = "usage_ranking_daily"
_TOOL_RESULTS = frozenset({"success", "error", "denied"})


def _tracking_enabled() -> bool:
    raw = os.getenv("MCP_PROXY_TRACK_USAGE_FIRESTORE", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return os.getenv("ENV", "") != "local"


def _db() -> firestore.Client:
    return get_client()


def _users_coll():
    return _db().collection("users")


def _ranking_coll():
    return _db().collection(_RANKING_COLL)


def _day_key(timestamp: datetime) -> str:
    return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _safe_suffix(value: str) -> str:
    text = (value or "").strip()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return digest


def _user_doc(user_id: str):
    return _users_coll().document(user_id)


def _apply_counter_set(
    batch: firestore.WriteBatch,
    doc_ref,
    *,
    base_fields: Mapping[str, object],
    timestamp: datetime,
    authorized_delta: int = 0,
    tool_delta: int = 0,
    result: str | None = None,
) -> None:
    batch.set(doc_ref, {**base_fields}, merge=True)
    updates: dict[str, object] = {
        "last_activity_at": timestamp,
    }
    if authorized_delta:
        updates["authorized_request_count"] = firestore.Increment(authorized_delta)
    if tool_delta:
        updates["tool_call_count"] = firestore.Increment(tool_delta)
    if result in _TOOL_RESULTS:
        updates[f"result_counts.{result}"] = firestore.Increment(1)
    batch.update(doc_ref, updates)


def record_authorized_request(user_id: str, *, timestamp: datetime | None = None) -> None:
    """Increment Firestore usage aggregates for an authorized MCP request."""
    if not _tracking_enabled():
        return
    ts = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = _day_key(ts)
    batch = _db().batch()
    _apply_counter_set(
        batch,
        _user_doc(user_id).collection(_TOTALS_SUBCOLL).document(day),
        base_fields={"date": day},
        timestamp=ts,
        authorized_delta=1,
    )
    _apply_counter_set(
        batch,
        _ranking_coll().document(f"{day}__{user_id}"),
        base_fields={"date": day, "user_id": user_id},
        timestamp=ts,
        authorized_delta=1,
    )
    try:
        batch.commit()
    except Exception as exc:
        logger.warning("Failed to record authorized usage for user_id=%s: %s", user_id, exc)


def record_tool_call(
    user_id: str,
    *,
    tool_name: str | None,
    server_id: str | None,
    result: str | None,
    timestamp: datetime | None = None,
) -> None:
    """Increment Firestore usage aggregates for one proxied tool call."""
    if not _tracking_enabled():
        return
    ts = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = _day_key(ts)
    batch = _db().batch()
    user_root = _user_doc(user_id)
    _apply_counter_set(
        batch,
        user_root.collection(_TOTALS_SUBCOLL).document(day),
        base_fields={"date": day},
        timestamp=ts,
        tool_delta=1,
        result=result,
    )
    _apply_counter_set(
        batch,
        _ranking_coll().document(f"{day}__{user_id}"),
        base_fields={"date": day, "user_id": user_id},
        timestamp=ts,
        tool_delta=1,
        result=result,
    )
    if server_id:
        _apply_counter_set(
            batch,
            user_root.collection(_SERVERS_SUBCOLL).document(f"{day}__{_safe_suffix(server_id)}"),
            base_fields={"date": day, "server_id": server_id},
            timestamp=ts,
            tool_delta=1,
            result=result,
        )
    if tool_name:
        _apply_counter_set(
            batch,
            user_root.collection(_TOOLS_SUBCOLL).document(f"{day}__{_safe_suffix(tool_name)}"),
            base_fields={"date": day, "tool_name": tool_name},
            timestamp=ts,
            tool_delta=1,
            result=result,
        )
    try:
        batch.commit()
    except Exception as exc:
        logger.warning(
            "Failed to record tool usage for user_id=%s tool=%s server=%s: %s",
            user_id,
            tool_name,
            server_id,
            exc,
        )


def _stream_to_dicts(stream: Iterable) -> list[dict]:
    rows: list[dict] = []
    for item in stream:
        if hasattr(item, "to_dict"):
            rows.append((item.to_dict() or {}))
        elif isinstance(item, Mapping):
            rows.append(dict(item))
    return rows


def list_user_usage_totals(user_id: str, *, start_day: str, end_day: str) -> list[dict]:
    query = _user_doc(user_id).collection(_TOTALS_SUBCOLL).where("date", ">=", start_day).where("date", "<=", end_day)
    return _stream_to_dicts(query.stream())


def list_user_usage_servers(user_id: str, *, start_day: str, end_day: str) -> list[dict]:
    query = _user_doc(user_id).collection(_SERVERS_SUBCOLL).where("date", ">=", start_day).where("date", "<=", end_day)
    return _stream_to_dicts(query.stream())


def list_user_usage_tools(user_id: str, *, start_day: str, end_day: str) -> list[dict]:
    query = _user_doc(user_id).collection(_TOOLS_SUBCOLL).where("date", ">=", start_day).where("date", "<=", end_day)
    return _stream_to_dicts(query.stream())


def list_usage_ranking_rows(*, start_day: str, end_day: str) -> list[dict]:
    query = _ranking_coll().where("date", ">=", start_day).where("date", "<=", end_day)
    return _stream_to_dicts(query.stream())
