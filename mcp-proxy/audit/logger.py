"""Structured audit logging (Cloud Logging picks up JSON / text)."""

import json
import logging
from typing import Any, Mapping, Optional, Union

logger = logging.getLogger("mcp_proxy.audit")


def log_audit(
    action: str,
    *,
    user_id: Optional[str] = None,
    role: Optional[str] = None,
    result: Optional[str] = None,
    details: Optional[Union[str, Mapping[str, Any]]] = None,
    request_id: Optional[str] = None,
    **extra: Any,
) -> None:
    """Emit one audit line as JSON for Log Explorer queries."""
    payload: dict[str, Any] = {"action": action, "audit": True}
    if user_id is not None:
        payload["user_id"] = user_id
    if role is not None:
        payload["role"] = role
    if result is not None:
        payload["result"] = result
    if request_id is not None:
        payload["request_id"] = request_id
    if details is not None:
        payload["details"] = details
    for k, v in extra.items():
        if v is not None:
            payload[k] = v
    # Keep the serialized payload in the message for plain-text fallbacks while also
    # attaching true structured fields for Cloud Logging queries on jsonPayload.*.
    logger.info(
        "audit:%s",
        json.dumps(payload, default=str),
        extra={"json_fields": payload},
    )
