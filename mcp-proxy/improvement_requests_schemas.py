"""Schemas for MCP proxy improvement requests."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

ImprovementRequestStatus = Literal["pending", "approved"]


def _clean_list(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = (raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


class ImprovementRequestErrorContext(BaseModel):
    """Optional failure context attached to a product improvement request."""

    failed_tool_name: Optional[str] = Field(None, min_length=1, max_length=200)
    error_message: Optional[str] = Field(None, min_length=1, max_length=4000)
    error_code: Optional[int] = None

    @field_validator("failed_tool_name", "error_message")
    @classmethod
    def strip_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ImprovementRequestCreate(BaseModel):
    """Payload for the built-in MCP improvement request tool."""

    summary: str = Field(..., min_length=1, max_length=300)
    details: Optional[str] = Field(None, max_length=4000)
    tool_names: list[str] = Field(default_factory=list)
    server_ids: list[str] = Field(default_factory=list)
    error_context: Optional[ImprovementRequestErrorContext] = None

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("summary cannot be empty")
        return cleaned

    @field_validator("details")
    @classmethod
    def strip_details(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("tool_names", "server_ids")
    @classmethod
    def clean_targets(cls, values: list[str]) -> list[str]:
        return _clean_list(values)

    @model_validator(mode="after")
    def require_targets(self):
        if not self.tool_names and not self.server_ids:
            raise ValueError("Provide at least one target tool or server")
        return self


class ImprovementRequestInDB(BaseModel):
    """Improvement request as stored in Firestore."""

    id: str
    requester_user_id: str
    requester_email: str = ""
    requester_kind: Literal["human", "agent"] = "human"
    requester_label: str = ""
    summary: str
    details: str = ""
    tool_names: list[str] = Field(default_factory=list)
    server_ids: list[str] = Field(default_factory=list)
    status: ImprovementRequestStatus = "pending"
    error_context: Optional[ImprovementRequestErrorContext] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
