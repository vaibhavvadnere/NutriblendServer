"""
schemas/admin.py — Request/response models for the admin (dashboard) endpoints.
"""

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.user import UserOut, validate_mobile_field, validate_name_field

Role = Literal["user", "admin"]


class AdminCreateRequest(BaseModel):
    """POST /admin/admins. Unknown fields (e.g. "role", "status") are rejected,
    not silently ignored, so a caller can't believe they set something."""
    model_config = ConfigDict(extra="forbid")

    mobile_number: str
    name: str = Field(min_length=1, max_length=60)
    email: Optional[EmailStr] = None

    @field_validator("mobile_number")
    @classmethod
    def _mobile(cls, v: str) -> str:
        return validate_mobile_field(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_name_field(v)


class AdminAccountOut(UserOut):
    """An account plus its role (only admin endpoints expose the role)."""
    role: Role


class AdminMe(BaseModel):
    """GET /admin/me — lets the dashboard confirm the signed-in number is an admin."""
    user: UserOut
    is_admin: bool = True


class UsersByStatus(BaseModel):
    active: int = 0
    pending: int = 0
    blocked: int = 0


class DailyCount(BaseModel):
    date: date
    count: int


class AdminStats(BaseModel):
    total_users: int
    users_by_status: UsersByStatus
    signups_today: int
    signups_last_7_days: int
    #: Distinct users who signed in during the last 7 days.
    active_last_7_days: int
    #: One entry per day, oldest first, zero-filled, in ADMIN_TIMEZONE.
    signups_by_day: list[DailyCount]
    timezone: str
    #: Videos by status: {"uploading": n, "draft": n, "published": n}.
    videos: dict[str, int] = {}


class AdminUserList(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int
    pages: int


class AdminUserDetail(UserOut):
    role: Role = "user"
    #: Devices currently signed in (unrevoked, unexpired refresh tokens).
    active_sessions: int
    is_admin: bool


class StatusChangeRequest(BaseModel):
    """PATCH /admin/users/{id}/status. Only block / unblock — pending is set by signup."""
    status: Literal["active", "blocked"] = Field(
        description='"blocked" bars the account and signs it out everywhere; "active" unblocks it.'
    )
