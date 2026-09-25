"""
routers/users.py — Profile endpoints.
"""

from bson import ObjectId
from fastapi import APIRouter, Depends, status

from app.config import settings
from app.deps import get_current_user
from app.database import users_collection
from app.errors import APIError, ErrorCode
from app.schemas.user import UserCreate, UserOut, UserUpdate
from app.services import users as user_service

router = APIRouter(prefix=f"{settings.API_PREFIX}/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def get_me(current_user: dict = Depends(get_current_user)):
    """The signed-in user's own profile."""
    return user_service.to_user_out(current_user)


@router.patch("/me", response_model=UserOut)
async def update_me(payload: UserUpdate, current_user: dict = Depends(get_current_user)):
    """
    Update your own name and/or email. Only the fields you send are changed.

    The mobile number is deliberately not editable here — changing it is a
    re-verification flow (prove the new number by OTP), not a profile edit.
    """
    updated = await user_service.update_profile(
        current_user["mobile_number"], payload.name, payload.email
    )
    return user_service.to_user_out(updated)


@router.post(
    "",
    response_model=UserOut,
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
        payload.mobile_number, payload.name, payload.email
    )
    return user_service.to_user_out(doc)


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: str, current_user: dict = Depends(get_current_user)):
    """
    Fetch a user by id. Requires authentication — an open endpoint here would
    let anyone walk the user table by guessing ObjectIds.
    """
    if not ObjectId.is_valid(user_id):
        raise APIError(
            status.HTTP_400_BAD_REQUEST, ErrorCode.VALIDATION_ERROR, "Invalid user id"
        )
    doc = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not doc:
        raise APIError(
            status.HTTP_404_NOT_FOUND, ErrorCode.ACCOUNT_NOT_FOUND, "User not found"
        )
    return user_service.to_user_out(doc)
