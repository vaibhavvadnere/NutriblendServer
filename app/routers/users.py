"""
routers/users.py — Profile endpoints (HTTP only; rules live in services/user_service.py).
"""

from fastapi import APIRouter, Depends, status

from app.core.config import settings
from app.core.deps import get_current_user
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.user import UserCreate, UserOut, UserUpdate
from app.services import user_service

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/users", tags=["users"], responses=ERROR_RESPONSES
)


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
