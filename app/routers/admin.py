"""
routers/admin.py — Endpoints for the admin dashboard (HTTP only; rules live in
services/admin_service.py).

Every route requires a signed-in admin account (role "admin"; see
core/deps.require_admin); anyone else gets 403 FORBIDDEN, and a
missing/expired token gets 401 as usual.

Creating admins lives in routers/admin_accounts.py; the user list is
GET /api/v1/users (routers/users.py), also admin-only.
"""

from fastapi import APIRouter, Depends

from app.core.config import settings
from app.core.deps import require_admin
from app.schemas.admin import (
    AdminMe,
    AdminStats,
    AdminUserDetail,
    StatusChangeRequest,
)
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.services import admin_service, user_service

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/admin",
    tags=["admin"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_admin)],
)


@router.get("/me", response_model=ApiResponse[AdminMe])
async def admin_me(admin: dict = Depends(require_admin)):
    """Confirms the signed-in account is an admin. The dashboard calls this right after sign-in."""
    return ok(AdminMe(user=user_service.to_user_out(admin)), "Admin access confirmed")


@router.get("/stats", response_model=ApiResponse[AdminStats])
async def stats():
    """Headline numbers and a 30-day signups series for the dashboard home screen."""
    return ok(await admin_service.get_stats(), "Stats fetched successfully")


@router.get("/users/{user_id}", response_model=ApiResponse[AdminUserDetail])
async def get_user(user_id: str):
    """One account, plus how many devices it is signed in on."""
    return ok(await admin_service.get_user_detail(user_id), "User fetched successfully")


@router.patch("/users/{user_id}/status", response_model=ApiResponse[AdminUserDetail])
async def set_user_status(
    user_id: str, payload: StatusChangeRequest, admin: dict = Depends(require_admin)
):
    """
    Block or unblock an account.

    - `blocked` — cannot sign in; all its sessions are revoked immediately.
    - `active` — unblocks (an account that never verified goes back to pending).
    - `409 INVALID_STATUS_CHANGE` — e.g. blocking an admin, or activating a pending account.
    """
    detail = await admin_service.set_status(user_id, payload.status, admin)
    return ok(detail, f"User is now {detail.status}")
