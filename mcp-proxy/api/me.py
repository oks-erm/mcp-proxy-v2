"""User endpoints: GET /me, GET /me/status, GET /me/permissions, GET /me/mcp-credentials (Phase 4)."""

from typing import Dict

from api.audit_urls import log_explorer_user_activity_url
from api.deps import get_current_user, verify_csrf_for_unsafe_methods
from audit.logger import log_audit
from credentials.generator import (
    decrypt_user_key_from_storage,
    generate_user_key,
    get_proxy_url,
)
from fastapi import APIRouter, Depends, HTTPException, status
from managed_apps_cloud_run import prune_stale_managed_apps
from managed_apps_store import list_managed_apps
from models import ManagedAppRecord
from permissions.models import PermissionItem
from permissions.store import get_user_permissions
from users import store as users_store
from users.schemas import UserInDB, UserProfile
from users.service import get_user_status_for_response

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=UserProfile)
async def get_me(user: UserInDB = Depends(get_current_user)):
    """Current user profile (id, email, role, status)."""
    return UserProfile(
        id=user.id,
        email=user.email,
        kind=user.kind,
        agent_name=user.agent_name or "",
        agent_url=user.agent_url or "",
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.get("/status")
async def get_me_status(user: UserInDB = Depends(get_current_user)):
    """Approval status: active, waiting_for_approval, or rejected."""
    return {"status": get_user_status_for_response(user)}


@router.get("/permissions", response_model=list[PermissionItem])
async def get_me_permissions(user: UserInDB = Depends(get_current_user)):
    """List permissions (server_id, read, write) for current user. Empty if admin/power_user (access all)."""
    if user.role in ("admin", "power_user"):
        return []
    return get_user_permissions(user.id)


@router.get("/apps", response_model=list[ManagedAppRecord])
async def get_my_managed_apps(user: UserInDB = Depends(get_current_user)):
    """Managed dashboard apps created by the current user or agent (not the global admin list)."""
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not approved")
    prune_stale_managed_apps()
    return list_managed_apps(include_deleted=True, creator_user_id=user.id)


@router.get("/activity/log-explorer", response_model=Dict[str, str])
async def get_my_activity_log_explorer(user: UserInDB = Depends(get_current_user)):
    """Read-only Log Explorer link filtered to this user's MCP/audit-related log lines."""
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not approved")
    return {"url": log_explorer_user_activity_url(user.id)}


@router.get("/mcp-credentials")
async def get_me_mcp_credentials(user: UserInDB = Depends(get_current_user)):
    """Returns user_key and proxy_url for MCP clients. 403 if status not active.

    The raw key is returned on first issue, after regenerate, and on later visits if stored encrypted
    (user_key_enc). Legacy rows with only user_key_hash cannot be recovered; those users must regenerate.
    """
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not approved")
    data = (users_store._users_coll().document(user.id).get().to_dict()) or {}
    has_key = bool(data.get("user_key_hash"))
    proxy_url = get_proxy_url()
    if not has_key:
        user_key = generate_user_key(user.id)
        log_audit("mcp_credentials_generated", user_id=user.id, role=user.role, result="success")
        return {"user_key": user_key, "proxy_url": proxy_url}
    revealed = decrypt_user_key_from_storage(data.get("user_key_enc") or "")
    if revealed:
        return {"user_key": revealed, "proxy_url": proxy_url}
    return {
        "proxy_url": proxy_url,
        "message": "Your API key was issued before reveal support, or it could not be decrypted. Regenerate to get a new key you can copy anytime.",
    }


@router.post("/mcp-credentials/regenerate")
async def regenerate_mcp_credentials(
    _: None = Depends(verify_csrf_for_unsafe_methods),
    user: UserInDB = Depends(get_current_user),
):
    """Generate a new user_key (old one stops working). Returns user_key and proxy_url once."""
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not approved")
    user_key = generate_user_key(user.id)
    proxy_url = get_proxy_url()
    log_audit(
        "mcp_credentials_generated", user_id=user.id, role=user.role, result="success", details={"regenerate": True}
    )
    return {"user_key": user_key, "proxy_url": proxy_url}
