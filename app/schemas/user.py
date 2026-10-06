"""
schemas/user.py — Pydantic request/response models for User.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.utils.phone import MOBILE_REGEX, InvalidMobileNumber, normalize_mobile

# Account lifecycle:
#   pending — signed up, mobile number not yet proven. Overwritable, auto-expires.
#   active  — verified. Can sign in.
#   blocked — barred by an admin. Cannot sign in.
UserStatus = Literal["pending", "active", "blocked"]

__all__ = [
    "MOBILE_REGEX",
    "UserCreate",
    "UserOut",
    "UserStatus",
    "validate_mobile_field",
    "validate_name_field",
    "validate_state_field",
]


def validate_mobile_field(v: str) -> str:
    """Shared validator — normalises, then validates. Used by every schema
    that accepts a mobile number, so all of them behave identically."""
    try:
        return normalize_mobile(v)
    except InvalidMobileNumber as exc:
        raise ValueError(str(exc)) from exc


def validate_name_field(v: str) -> str:
    v = " ".join(str(v).split())  # collapse runs of whitespace
    if len(v) < 1:
        raise ValueError("Name cannot be empty")
    if len(v) > 60:
        raise ValueError("Name cannot be longer than 60 characters")
    # Deliberately permissive: names legitimately contain apostrophes, hyphens,
    # dots and non-Latin scripts. We only reject input with no letters at all.
    if not any(ch.isalpha() for ch in v):
        raise ValueError("Name must contain at least one letter")
    return v


def validate_state_field(v: Optional[str]) -> Optional[str]:
    """Optional free-text state, e.g. "Maharashtra". Whitespace is tidied;
    an empty string is treated as "not provided"."""
    if v is None:
        return None
    v = " ".join(str(v).split())
    if not v:
        return None
    if len(v) > 50:
        raise ValueError("State cannot be longer than 50 characters")
    if not any(ch.isalpha() for ch in v):
        raise ValueError("State must contain at least one letter")
    return v


class UserCreate(BaseModel):
    mobile_number: str
    name: str = Field(min_length=1, max_length=60)
    email: Optional[EmailStr] = None
    state: Optional[str] = None

    @field_validator("mobile_number")
    @classmethod
    def _mobile(cls, v: str) -> str:
        return validate_mobile_field(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_name_field(v)

    @field_validator("state")
    @classmethod
    def _state(cls, v: Optional[str]) -> Optional[str]:
        return validate_state_field(v)


class UserUpdate(BaseModel):
    """PATCH /users/me — everything optional, only supplied fields change."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    email: Optional[EmailStr] = None
    state: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return validate_name_field(v) if v is not None else v

    @field_validator("state")
    @classmethod
    def _state(cls, v: Optional[str]) -> Optional[str]:
        return validate_state_field(v)


class UserOut(BaseModel):
    id: str
    mobile_number: str
    name: str
    email: Optional[EmailStr] = None
    state: Optional[str] = None
    status: UserStatus = "pending"
    is_verified: bool
    created_at: datetime
    verified_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
