"""Per-server MCP diagnostics: initialize + tools/resources/prompts list."""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from mcp_utils import parse_mcp_response
from models import ServerConfig
from upstream_headers import resolve_upstream_headers

logger = logging.getLogger(__name__)

INITIALIZE_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-proxy-diagnostics", "version": "0.1.0"},
    },
}


def _rpc(method: str, req_id: int, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        body["params"] = params
    return body


async def _post_mcp(
    url: str,
    base_headers: Dict[str, str],
    payload: Dict[str, Any],
    session_id: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Returns (parsed_jsonrpc_body, mcp_session_id_from_response, error_message)."""
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **base_headers,
    }
    if session_id:
        req_headers["Mcp-Session-Id"] = session_id
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=req_headers)
            data = parse_mcp_response(resp.text)
            new_sid = resp.headers.get("Mcp-Session-Id")
            if not resp.is_success:
                detail = (resp.text or "")[:500] or f"HTTP {resp.status_code}"
                return None, new_sid, detail
            if data is None:
                return None, new_sid, "Invalid or empty MCP response"
            if isinstance(data.get("error"), dict):
                err = data["error"]
                msg = err.get("message", str(err))
                return None, new_sid, str(msg)
            return data, new_sid, None
    except httpx.RequestError as e:
        logger.warning("Diagnostics request failed: %s", e)
        return None, None, str(e)


def _safe_tools(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not data or "result" not in data:
        return []
    tools = data["result"].get("tools") or []
    out: List[Dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "name": t.get("name", ""),
                "description": (t.get("description") or "")[:2000],
            }
        )
    return out


def _safe_resources(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not data or "result" not in data:
        return []
    resources = data["result"].get("resources") or []
    out: List[Dict[str, Any]] = []
    for r in resources:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "uri": (r.get("uri") or "")[:2000],
                "name": (r.get("name") or "")[:500],
                "description": ((r.get("description") or "")[:2000]),
            }
        )
    return out


def _safe_prompts(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not data or "result" not in data:
        return []
    prompts = data["result"].get("prompts") or []
    out: List[Dict[str, Any]] = []
    for p in prompts:
        if not isinstance(p, dict):
            continue
        out.append(
            {
                "name": (p.get("name") or "")[:500],
                "description": ((p.get("description") or "")[:2000]),
            }
        )
    return out


async def run_server_diagnostics(cfg: ServerConfig, *, user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Run initialize then tools/list, resources/list, prompts/list against cfg.url.
    Does not prefix names (single-server probe).
    For upstream_auth oauth2, pass user_id (Firestore user id) of a user who has linked OAuth.
    """
    if cfg.upstream_auth == "oauth2" and not user_id:
        return {
            "server_id": cfg.id,
            "url": cfg.url,
            "enabled": cfg.enabled,
            "note": "OAuth2 upstream: add query parameter user_id=<firestore user id> to run diagnostics with that user's tokens",
            "sections": {
                "initialize": {"ok": False, "error": "user_id required for oauth2", "latency_ms": None},
                "tools": {"ok": False, "error": None, "latency_ms": None, "items": []},
                "resources": {"ok": False, "error": None, "latency_ms": None, "items": []},
                "prompts": {"ok": False, "error": None, "latency_ms": None, "items": []},
            },
        }
    try:
        headers = await resolve_upstream_headers(cfg, user_id=user_id)
    except Exception as e:
        return {
            "server_id": cfg.id,
            "url": cfg.url,
            "enabled": cfg.enabled,
            "sections": {
                "initialize": {"ok": False, "error": str(e), "latency_ms": None},
                "tools": {"ok": False, "error": None, "latency_ms": None, "items": []},
                "resources": {"ok": False, "error": None, "latency_ms": None, "items": []},
                "prompts": {"ok": False, "error": None, "latency_ms": None, "items": []},
            },
        }

    sections: Dict[str, Any] = {
        "initialize": {"ok": False, "error": None, "latency_ms": None},
        "tools": {"ok": False, "error": None, "latency_ms": None, "items": []},
        "resources": {"ok": False, "error": None, "latency_ms": None, "items": []},
        "prompts": {"ok": False, "error": None, "latency_ms": None, "items": []},
    }

    t0 = time.perf_counter()
    init_data, session_id, init_err = await _post_mcp(cfg.url, headers, INITIALIZE_PAYLOAD, None)
    sections["initialize"]["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    if init_err or not init_data or "result" not in init_data:
        sections["initialize"]["error"] = init_err or "Initialize failed"
        return {"server_id": cfg.id, "url": cfg.url, "enabled": cfg.enabled, "sections": sections}

    sections["initialize"]["ok"] = True
    sid = session_id or ""

    for idx, (name, method, extractor) in enumerate(
        [
            ("tools", "tools/list", _safe_tools),
            ("resources", "resources/list", _safe_resources),
            ("prompts", "prompts/list", _safe_prompts),
        ],
        start=2,
    ):
        t1 = time.perf_counter()
        payload = _rpc(method, idx, {})
        data, new_sid, err = await _post_mcp(cfg.url, headers, payload, sid)
        if new_sid:
            sid = new_sid
        sections[name]["latency_ms"] = int((time.perf_counter() - t1) * 1000)
        if err:
            sections[name]["error"] = err
        else:
            sections[name]["ok"] = True
            sections[name]["items"] = extractor(data)

    return {"server_id": cfg.id, "url": cfg.url, "enabled": cfg.enabled, "sections": sections}
