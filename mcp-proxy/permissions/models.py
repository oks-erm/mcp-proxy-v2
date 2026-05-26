"""Permission DTOs for per-user MCP server access."""

from pydantic import BaseModel, Field


class PermissionItem(BaseModel):
    server_id: str = Field(..., min_length=1)
    read: bool = True
    write: bool = False
