"""Admin user endpoints: list, pending, approve, reject, role, permissions."""

import logging
import re
from typing import List, Optional
from urllib.parse import urlparse

from api.deps import require_admin, verify_csrf_for_unsafe_methods
from audit.logger import log_audit
from credentials.generator import generate_user_key, get_proxy_url
from fastapi import APIRouter, Depends, HTTPException, status
from permissions.models import PermissionItem
from permissions.store import get_user_permissions, set_user_permissions
from pydantic import BaseModel, Field, field_validator
from users import service as user_service
from users import store
from users.schemas import UserInDB, UserProfile, UserUsageLeaderboard, UserUsageSummary
from users.usage_summary import (
    UserUsageSummaryUnavailableError,
    get_usage_leaderboard,
    get_user_usage_summary,
)

logger = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _optional_http_agent_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("agent_url must be a valid http(s) URL with a host")
    return u


def user_to_profile(u: UserInDB) -> UserProfile:
    return UserProfile(
        id=u.id,
        email=u.email,
        kind=u.kind,
        agent_name=u.agent_name or "",
        agent_url=u.agent_url or "",
        role=u.role,
        status=u.status,
        created_at=u.created_at,
        updated_at=u.updated_at,
    )


router = APIRouter(
    prefix="/users",
    tags=["admin-users"],
    dependencies=[Depends(verify_csrf_for_unsafe_methods)],
)


class UpdateRoleRequest(BaseModel):
    role: str = Field(..., pattern="^(admin|power_user|user)$")


class UpdatePermissionsRequest(BaseModel):
    permissions: List[PermissionItem]


class CreateAgentRequest(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=200)
    agent_url: str = Field(default="", max_length=2000)

    @field_validator("agent_name")
    @classmethod
    def name_strip(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("agent_name cannot be empty")
        return s

    @field_validator("agent_url")
    @classmethod
    def url_strip(cls, v: str) -> str:
        return (v or "").strip()[:2000]


class CreateMcpCredentialResponse(BaseModel):
    user: UserProfile
    user_key: str
    proxy_url: str


class CreateAdHocUserRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def email_strip_validate(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if not s or not _EMAIL_RE.match(s):
            raise ValueError("email must be a valid email address")
        return s


class UpdateAgentRequest(BaseModel):
    agent_name: Optional[str] = Field(None, min_length=1, max_length=200)
    agent_url: Optional[str] = Field(None, max_length=2000)

    @field_validator("agent_name")
    @classmethod
    def name_strip_opt(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("agent_name cannot be empty")
        return s

    @field_validator("agent_url")
    @classmethod
    def url_strip_opt(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip()[:2000]


@router.post("/agents", response_model=CreateMcpCredentialResponse)
async def create_agent_user(
    body: CreateAgentRequest,
    admin: UserInDB = Depends(require_admin),
):
    """Create a service agent (no Google login). Role is always user; returns MCP key once."""
    try:
        url_norm = _optional_http_agent_url(body.agent_url)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    user = store.create_agent(body.agent_name, url_norm)
    raw = generate_user_key(user.id)
    proxy_url = get_proxy_url()
    log_audit(
        "agent_created",
        user_id=admin.id,
        role=admin.role,
        result="success",
        details={"target_user_id": user.id, "agent_name": user.agent_name},
    )
    return CreateMcpCredentialResponse(user=user_to_profile(user), user_key=raw, proxy_url=proxy_url)


@router.post("/ad-hoc", response_model=CreateMcpCredentialResponse)
async def create_ad_hoc_human_user(
    body: CreateAdHocUserRequest,
    admin: UserInDB = Depends(require_admin),
):
    """Create an active human user directly from email and issue an MCP key once."""
    try:
        user = user_service.create_ad_hoc_user(body.email)
    except ValueError as e:
        detail = str(e) or "Could not create ad-hoc user"
        status_code = status.HTTP_409_CONFLICT if "already exists" in detail.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=detail) from e
    raw = generate_user_key(user.id)
    proxy_url = get_proxy_url()
    log_audit(
        "ad_hoc_user_created",
        user_id=admin.id,
        role=admin.role,
        result="success",
        details={"target_user_id": user.id, "email": user.email},
    )
    return CreateMcpCredentialResponse(user=user_to_profile(user), user_key=raw, proxy_url=proxy_url)


@router.patch("/{user_id}/agent", response_model=UserProfile)
async def patch_agent_profile(
    user_id: str,
    body: UpdateAgentRequest,
    admin: UserInDB = Depends(require_admin),
):
    """Update display name / page URL for a service agent only."""
    if body.agent_name is None and body.agent_url is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide agent_name and/or agent_url",
        )
    try:
        url_norm = body.agent_url
        if url_norm is not None:
            url_norm = _optional_http_agent_url(url_norm)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    updated = store.update_agent_profile(
        user_id,
        agent_name=body.agent_name,
        agent_url=url_norm,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found or not a service agent")
    log_audit(
        "agent_updated",
        user_id=admin.id,
        role=admin.role,
        details={"target_user_id": user_id},
    )
    return user_to_profile(updated)


@router.get("", response_model=List[UserProfile])
async def list_users(admin: UserInDB = Depends(require_admin)):
    """List all users."""
    users = store.list_users()
    return [user_to_profile(u) for u in users]


@router.get("/usage-ranking", response_model=UserUsageLeaderboard)
async def get_usage_ranking_route(admin: UserInDB = Depends(require_admin)):
    """Return a ranked recent usage leaderboard across proxy users."""
    try:
        return get_usage_leaderboard()
    except UserUsageSummaryUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc) or "Usage ranking is temporarily unavailable",
        ) from exc


@router.get("/pending", response_model=List[UserProfile])
async def list_pending_users(admin: UserInDB = Depends(require_admin)):
    """List users with status pending (awaiting approval)."""
    users = store.get_pending_users()
    return [user_to_profile(u) for u in users]


@router.post("/{user_id}/approve")
async def approve_user(user_id: str, admin: UserInDB = Depends(require_admin)):
    """Approve a user; set status to active. Update latest pending access_request."""
    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is not pending")
    store.update_user(user_id, status="active")
    # Update any pending access request for this user
    for req in store.get_access_requests_by_user(user_id):
        if req.status == "pending":
            store.update_access_request(req.id, status="approved", reviewed_by=admin.id)
    log_audit("user_approved", user_id=admin.id, role=admin.role, details={"target_user_id": user_id})
    return {"status": "approved", "user_id": user_id}


@router.post("/{user_id}/reject")
async def reject_user(user_id: str, admin: UserInDB = Depends(require_admin)):
    """Reject a user; set status to rejected. Update latest pending access_request."""
    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is not pending")
    store.update_user(user_id, status="rejected")
    for req in store.get_access_requests_by_user(user_id):
        if req.status == "pending":
            store.update_access_request(req.id, status="rejected", reviewed_by=admin.id)
    log_audit("user_rejected", user_id=admin.id, role=admin.role, details={"target_user_id": user_id})
    return {"status": "rejected", "user_id": user_id}


@router.patch("/{user_id}/role")
async def patch_user_role(
    user_id: str,
    body: UpdateRoleRequest,
    admin: UserInDB = Depends(require_admin),
):
    """Set user role (admin, power_user, user)."""
    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.kind == "agent" and body.role != "user":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Service agents can only have the user role",
        )
    store.update_user(user_id, role=body.role)
    log_audit(
        "role_changed", user_id=admin.id, role=admin.role, details={"target_user_id": user_id, "new_role": body.role}
    )
    return {"user_id": user_id, "role": body.role}


@router.get("/{user_id}/permissions", response_model=List[PermissionItem])
async def get_user_permissions_route(user_id: str, admin: UserInDB = Depends(require_admin)):
    """Get permissions for a user."""
    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return get_user_permissions(user_id)


@router.get("/{user_id}/usage-summary", response_model=UserUsageSummary)
async def get_user_usage_summary_route(user_id: str, admin: UserInDB = Depends(require_admin)):
    """Return a high-level 30-day proxy usage summary for one user."""
    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    try:
        return get_user_usage_summary(user_id)
    except UserUsageSummaryUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc) or "Usage summary is temporarily unavailable",
        ) from exc


@router.put("/{user_id}/permissions")
async def put_user_permissions(
    user_id: str,
    body: UpdatePermissionsRequest,
    admin: UserInDB = Depends(require_admin),
):
    """Replace permissions for a user (list of { server_id, read, write })."""
    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    set_user_permissions(user_id, body.permissions)
    log_audit(
        "permission_changed",
        user_id=admin.id,
        role=admin.role,
        details={"target_user_id": user_id, "count": len(body.permissions)},
    )
    return {"user_id": user_id, "permissions": [p.model_dump() for p in body.permissions]}


@router.post("/{user_id}/mcp-credentials/regenerate")
async def admin_regenerate_user_mcp_key(user_id: str, admin: UserInDB = Depends(require_admin)):
    """Rotate MCP API key for another user (support). Returns raw key once."""
    user = store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be active to receive MCP credentials",
        )
    raw = generate_user_key(user_id)
    proxy_url = get_proxy_url()
    log_audit(
        "mcp_credentials_generated",
        user_id=admin.id,
        role=admin.role,
        result="success",
        details={"target_user_id": user_id, "admin_regenerate": True},
    )
    return {"user_key": raw, "proxy_url": proxy_url}
