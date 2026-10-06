"""
routers/admin_accounts.py — Creating admin accounts.

This is the ONLY way an admin account comes into existence. Signup, the
deprecated POST /users and PATCH /users/me all write role "user" or leave the
role alone, and none of them accept a role from the caller.

Access: currently OPEN (no key, no login) so admins can be created from
Swagger during development. The only switch is ADMIN_CREATE_ENABLED in .env
(false -> 403 ADMIN_CREATE_DISABLED). Decide who may call it before go-live.
"""

import logging

from fastapi import APIRouter, Depends, Request, status

from app.core.config import settings
from app.core.deps import client_ip, require_admin_create_enabled
from app.schemas.admin import AdminAccountOut, AdminCreateRequest
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.services import user_service
from app.utils.phone import mask

logger = logging.getLogger("nutriblend.admin")

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/admin/admins",
    tags=["admin accounts"],
    responses=ERROR_RESPONSES,
)


@router.post(
    "",
    response_model=ApiResponse[AdminAccountOut],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_create_enabled)],
    summary="Create an admin account",
)
async def create_admin(payload: AdminCreateRequest, request: Request):
    """
    Create a **new** admin account. No key or login needed (development setup).

    The account starts `pending` and becomes `active` the first time its owner
    signs in with the normal OTP flow (`/auth/signin` → `/auth/verify-otp`),
    which proves they hold the number. It does not expire while pending.

    - `201` — created.
    - `409 ACCOUNT_EXISTS` — this number already has an account (user or admin);
      admins are never created by promoting an existing account.
    - `409 EMAIL_IN_USE` — the email belongs to another account.
    - `422` — invalid input, or an unexpected field such as `role`.
    - `403 ADMIN_CREATE_DISABLED` — ADMIN_CREATE_ENABLED=false on the server.
    """
    ip = client_ip(request)
    doc = await user_service.create_admin(payload.mobile_number, payload.name, payload.email, ip)
    logger.warning("Admin account created for %s from %s", mask(payload.mobile_number), ip)
    out = AdminAccountOut(**user_service.to_user_out(doc).model_dump(), role=user_service.ROLE_ADMIN)
    return ok(out, "Admin account created. Sign in with OTP to activate it.")
