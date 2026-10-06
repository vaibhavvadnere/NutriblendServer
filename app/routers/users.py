"""
routers/users.py — Profile endpoints (HTTP only; rules live in services/user_service.py).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, status

from app.core.config import settings
from app.core.deps import get_current_user, require_admin
from app.schemas.admin import AdminUserList
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.user import UserCreate, UserOut, UserUpdate
from app.services import admin_service, user_service

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/users", tags=["users"], responses=ERROR_RESPONSES
)


@router.get(
    "",
    response_model=ApiResponse[AdminUserList],
    dependencies=[Depends(require_admin)],
    summary="List all users (admins excluded)",
)
async def list_users(
    q: Optional[str] = Query(default=None, max_length=60, description="Part of a name, email or mobile number"),
    status: Optional[Literal["pending", "active", "blocked"]] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=admin_service.MAX_PAGE_SIZE),
):
    """
    All app users, newest first, paginated. **Admin accounts are never listed**:
    the database query only matches documents whose `role` is not `"admin"`
    (old documents with no `role` count as users and are included).

    Requires an admin access token (it returns every user's mobile and email).

    - `q` — search part of a name, email or mobile number.
    - `status` — only `pending`, `active` or `blocked` accounts.
    - `page`, `page_size` — pagination (max 100 per page).
    """
    return ok(await admin_service.list_users(q, status, page, page_size), "Users fetched successfully")


@router.get("/me", response_model=ApiResponse[UserOut])
async def get_me(current_user: dict = Depends(get_current_user)):
    """The signed-in user's own profile."""
    return ok(user_service.to_user_out(current_user), "Profile fetched successfully")


@router.patch("/me", response_model=ApiResponse[UserOut])
async def update_me(payload: UserUpdate, current_user: dict = Depends(get_current_user)):
    """
    Update your own name, email and/or state. Only the fields you send are changed.

    The mobile number is deliberately not editable here — changing it is a
    re-verification flow (prove the new number by OTP), not a profile edit.
    """
    updated = await user_service.update_profile(
        current_user["mobile_number"], payload.name, payload.email, payload.state
    )
    return ok(user_service.to_user_out(updated), "Profile updated successfully")


@router.post(
    "",
    response_model=ApiResponse[UserOut],
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
    summary="Create a user directly (admin/seeding only)",
)
async def create_user(payload: UserCreate):
    """
    **Deprecated — use `POST /auth/signup` instead.**

    Kept for seeding and admin tooling. Creates a *pending* account only: it
    cannot mint a usable login, so it can't be used to reserve someone else's
    mobile number. The account still has to pass OTP verification to become active.
    """
    doc = await user_service.create_or_refresh_pending(
        payload.mobile_number, payload.name, payload.email, payload.state
    )
    return ok(user_service.to_user_out(doc), "User created successfully")


@router.get("/{user_id}", response_model=ApiResponse[UserOut])
async def get_user(user_id: str, current_user: dict = Depends(get_current_user)):
    """
    Fetch a user by id. Requires authentication — an open endpoint here would
    let anyone walk the user table by guessing ObjectIds.
    """
    user = await user_service.get_by_id(user_id)
    return ok(user_service.to_user_out(user), "User fetched successfully")
