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
from app.schemas.video import UserStats
from app.services import admin_service, favourite_service, user_service, watch_service

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


@router.get("/me/stats", response_model=ApiResponse[UserStats])
async def my_stats(current_user: dict = Depends(get_current_user)):
    """
    Everything the profile screen needs, in one call: the ♡ count and the
    watched count.

    ```
    {
      "favourites": { "count": 12, "visible_count": 10, "limit": 500 },
      "watched":    { "videos_count": 14, "total_plays": 37 }
    }
    ```

    - `favourites.count` — every video the user hearted; what `limit` applies to.
    - `favourites.visible_count` — how many of those the app can show right now
      (an unpublished video stays hearted but drops out of the list). Use this
      for the tab badge so the number matches what they will see.
    - `watched.videos_count` — **different** videos played; replaying one does
      not raise it.
    - `watched.total_plays` — every play added up.

    No subscription required: these are numbers, not videos, so a user who is
    locked out still sees their counts.
    """
    return ok(
        UserStats(
            favourites=await favourite_service.stats_for_user(current_user["_id"]),
            watched=await watch_service.stats_for_user(current_user["_id"]),
        ),
        "Stats fetched successfully",
    )


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
