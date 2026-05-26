"""MCP tools for Breezeway inventory, users, tasks, and reservations."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, Optional

from breezeway_client import BreezewayClient
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

logger = logging.getLogger(__name__)

_breezeway_service: Optional[BreezewayClient] = None


def set_breezeway_service(service: BreezewayClient) -> None:
    global _breezeway_service
    _breezeway_service = service


@dataclass
class AppContext:
    """Context injected into each MCP request."""

    breezeway_service: BreezewayClient


def _get_service(ctx: Context[ServerSession, AppContext] | None) -> BreezewayClient:
    if ctx and ctx.request_context and ctx.request_context.lifespan_context:
        service = ctx.request_context.lifespan_context.breezeway_service
        if service:
            return service
    if _breezeway_service is not None:
        return _breezeway_service
    raise RuntimeError("Breezeway service not initialised")


@asynccontextmanager
async def mcp_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    service = _breezeway_service
    if service is None:
        try:
            import main as main_mod  # noqa: F401

            service = getattr(main_mod, "breezeway_service", None)
        except Exception:
            service = None
    if service is None:
        raise RuntimeError("Breezeway service not initialised — start the FastAPI app first")
    yield AppContext(breezeway_service=service)


mcp = FastMCP(
    "Breezeway",
    instructions=(
        "Use breezeway_get_properties to page through properties (limit up to 100 per request). "
        "Use breezeway_get_tasks to page through tasks (optional reference_property_id filter). "
        "Use breezeway_get_users to list all people, and breezeway_get_reservation to look up a reservation by external ID."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    lifespan=mcp_lifespan,
)


@mcp.tool()
async def breezeway_get_properties(
    page: int = 1, limit: int = 100, ctx: Context[ServerSession, AppContext] | None = None
) -> Dict[str, Any]:
    """List Breezeway properties for one page (read/list).

    Use when:
        You need a raw paginated slice of properties from the legacy Breezeway FastAPI integration.

    Args:
        page: 1-based page number.
        limit: Page size (≤ 100).

    Returns:
        Passthrough dict from ``get_properties_page`` or ``{\"error\": \"...\"}``.

    Pagination:
        Page index + limit.

    Sensitive data:
        This path returns the API payload unchanged — treat as potentially containing operational fields.

    Prefer:
        ``breezeway-mcp`` ``breezeway_list_properties_page`` with ``detail_level='summary'`` for safer summaries.

    """
    try:
        service = _get_service(ctx)
        return service.get_properties_page(page=page, limit=limit)
    except Exception as exc:
        logger.exception("breezeway_get_properties failed")
        return {"error": str(exc)}


@mcp.tool()
async def breezeway_get_tasks(
    page: int = 1,
    limit: int = 100,
    reference_property_id: Optional[str] = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> Dict[str, Any]:
    """List Breezeway tasks for one page (read/list).

    Use when:
        You need operational tasks, optionally scoped to a property id.

    Args:
        page, limit: Pagination (limit ≤ 100).
        reference_property_id: Optional Breezeway property reference filter.

    Returns:
        Passthrough from ``get_tasks_page`` or ``{\"error\": \"...\"}``.

    Pagination:
        Page index + limit.
    """
    try:
        service = _get_service(ctx)
        return service.get_tasks_page(page=page, limit=limit, reference_property_id=reference_property_id)
    except Exception as exc:
        logger.exception("breezeway_get_tasks failed")
        return {"error": str(exc)}


@mcp.tool()
async def breezeway_get_users(ctx: Context[ServerSession, AppContext] | None = None) -> Dict[str, Any]:
    """List all Breezeway users (read/list).

    Use when:
        You need the full people directory in one shot.

    Returns:
        ``{\"users\": [...]}`` or ``{\"error\": \"...\"}``.

    Pagination:
        None — single call.
    """
    try:
        service = _get_service(ctx)
        users = service.get_users()
        return {"users": users}
    except Exception as exc:
        logger.exception("breezeway_get_users failed")
        return {"error": str(exc)}


@mcp.tool()
async def breezeway_get_reservation(
    reservation_id: str,
    allow_multiple: bool = False,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> Dict[str, Any]:
    """Fetch a reservation by external id (read/detail).

    Use when:
        You have the external reservation id string from PMS/booking systems.

    Args:
        reservation_id: External id passed to ``get_reservation``.
        allow_multiple: Forwarded to service when multiple matches are possible.

    Returns:
        ``{\"reservation\": ...}`` or ``{\"error\": \"...\"}``.
    """
    try:
        service = _get_service(ctx)
        reservation = service.get_reservation(reservation_id, allow_multiple=allow_multiple)
        return {"reservation": reservation}
    except Exception as exc:
        logger.exception("breezeway_get_reservation failed")
        return {"error": str(exc)}
