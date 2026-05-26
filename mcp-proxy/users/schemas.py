"""Pydantic schemas for users and access requests."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

UserRole = Literal["admin", "power_user", "user"]
UserStatus = Literal["active", "pending", "rejected"]
AccessRequestStatus = Literal["pending", "approved", "rejected"]
UserKind = Literal["human", "agent"]


class UserProfile(BaseModel):
    """Current user profile (API response)."""

    id: str
    email: str = ""
    kind: UserKind = "human"
    agent_name: str = ""
    agent_url: str = ""
    role: UserRole
    status: UserStatus
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UserInDB(BaseModel):
    """User as stored in Firestore."""

    id: str
    google_id: str = ""
    email: str = ""
    kind: UserKind = "human"
    agent_name: str = ""
    agent_url: str = ""
    role: UserRole = "user"
    status: UserStatus = "pending"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AccessRequestInDB(BaseModel):
    """Access request as stored in Firestore."""

    id: str
    user_id: str
    email: str
    requested_at: datetime
    status: AccessRequestStatus = "pending"
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None


class LoginResponseTokens(BaseModel):
    """Access JWT in body; refresh token is httpOnly cookie only."""

    tokens: dict  # {"access": str}


class LoginResponsePending(BaseModel):
    """Response when user is pending approval."""

    status: Literal["waiting_for_approval"] = "waiting_for_approval"


class LoginResponseRejected(BaseModel):
    """Response when user was rejected."""

    status: Literal["rejected"] = "rejected"


class UsageResultCounts(BaseModel):
    """High-level outcome counts for proxied tool calls."""

    success: int = 0
    error: int = 0
    denied: int = 0


class UsageBreakdownItem(BaseModel):
    """Ranked usage entry for a server or tool."""

    name: str
    total: int = 0
    result_counts: UsageResultCounts = Field(default_factory=UsageResultCounts)


class UserUsageSummary(BaseModel):
    """Aggregated proxy usage summary for one user."""

    window_days: int
    authorized_request_count: int = 0
    tool_call_count: int = 0
    result_counts: UsageResultCounts = Field(default_factory=UsageResultCounts)
    last_activity_at: Optional[datetime] = None
    unique_servers_count: int = 0
    unique_tools_count: int = 0
    top_servers: list[UsageBreakdownItem] = Field(default_factory=list)
    top_tools: list[UsageBreakdownItem] = Field(default_factory=list)
    log_explorer_url: str = ""
    truncated: bool = False


class UserUsageRankingItem(BaseModel):
    """Dashboard ranking row for one proxy user."""

    user_id: str
    identity_label: str
    kind: UserKind = "human"
    email: str = ""
    tool_call_count: int = 0
    authorized_request_count: int = 0
    result_counts: UsageResultCounts = Field(default_factory=UsageResultCounts)
    last_activity_at: Optional[datetime] = None
    log_explorer_url: str = ""


class UserUsageLeaderboard(BaseModel):
    """Ranked recent usage across proxy users."""

    window_days: int
    truncated: bool = False
    users: list[UserUsageRankingItem] = Field(default_factory=list)
