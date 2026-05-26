"""Schemas for proxy-hosted skill update requests."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

SkillUpdateRequestStatus = Literal["pending", "approved"]


class SkillUpdateRequestCreate(BaseModel):
    """Payload for a request to update a published proxy skill."""

    skill_name: str = Field(..., min_length=1, max_length=120)
    summary: str = Field(..., min_length=1, max_length=300)
    details: Optional[str] = Field(None, max_length=4000)
    desired_outcome: Optional[str] = Field(None, max_length=1000)

    @field_validator("skill_name", "summary", "details", "desired_outcome")
    @classmethod
    def strip_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if value == cleaned or cleaned:
            return cleaned
        return None


class SkillUpdateRequestInDB(BaseModel):
    """Stored skill update request."""

    id: str
    skill_name: str
    requester_user_id: str
    requester_email: str = ""
    requester_kind: Literal["human", "agent"] = "human"
    requester_label: str = ""
    summary: str
    details: str = ""
    desired_outcome: str = ""
    status: SkillUpdateRequestStatus = "pending"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
