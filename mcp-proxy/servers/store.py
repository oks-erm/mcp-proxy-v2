"""MCP server config persistence (Firestore). Re-exports shared implementation."""

from firestore_store import (
    create_server,
    delete_server,
    get_server,
    list_servers,
    update_server,
)

__all__ = [
    "create_server",
    "delete_server",
    "get_server",
    "list_servers",
    "update_server",
]
