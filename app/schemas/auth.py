"""
schemas/auth.py — Request/response models for signup, signin and tokens.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.user import (
    UserOut,
    validate_mobile_field,
    validate_name_field,
    validate_state_field,
)


class SignupRequest(BaseModel):
    """POST /auth/signup — creates a pending account and sends an OTP."""
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


class SigninRequest(BaseModel):
    """POST /auth/signin and /auth/resend-otp — sends an OTP to an existing account."""
    mobile_number: str

    @field_validator("mobile_number")
    @classmethod
    def _mobile(cls, v: str) -> str:
        return validate_mobile_field(v)


class OTPVerify(BaseModel):
    mobile_number: str
    otp: str = Field(min_length=4, max_length=10)

    @field_validator("mobile_number")
    @classmethod
    def _mobile(cls, v: str) -> str:
        return validate_mobile_field(v)

    @field_validator("otp")
    @classmethod
    def _otp(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError("OTP must contain digits only")
        return v


class OTPSentResponse(BaseModel):
    #: Masked, so the app can show "OTP sent to 98XXXXXX10" without echoing
    #: the full number back onto a shared screen.
    mobile_number: str
    expires_in_minutes: int
    #: Seconds until another OTP may be requested — drives the "Resend in 47s" timer.
    resend_available_in_seconds: int
    #: Tells the app whether to go to the "create profile" or "welcome back" screen.
    is_new_account: bool
    #: Populated only when ENV != production, so you can test without real SMS.
    dev_otp: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    #: "app" (mobile app session) or "admin" (dashboard session). Decided by the
    #: sign-in endpoint that sent the OTP, never by the verify-otp request.
    scope: Literal["app", "admin"] = "app"
    user: UserOut


class AccessTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    scope: Literal["app", "admin"] = "app"


class LogoutAllResponse(BaseModel):
    revoked_sessions: int


# Kept so any existing caller of the old endpoint keeps working.
OTPRequest = SigninRequest


class DeleteAccountRequest(BaseModel):
    """
    POST /auth/delete-account — one body, two phases.

    Send just the number to start (an OTP goes out). Send the number *and* the
    OTP to actually delete. Deliberately one endpoint so the app has one call
    to wire up.
    """
    mobile_number: str
    #: Omit to request the OTP; include it to confirm the deletion.
    otp: Optional[str] = Field(default=None, min_length=4, max_length=10)

    @field_validator("mobile_number")
    @classmethod
    def _mobile(cls, v: str) -> str:
        return validate_mobile_field(v)

    @field_validator("otp")
    @classmethod
    def _otp(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v.isdigit():
            raise ValueError("OTP must contain digits only")
        return v


class DeleteAccountResponse(BaseModel):
    """Covers both phases; `deleted` tells the app which one just happened."""
    #: True when this call sent an OTP and changed nothing.
    otp_sent: bool
    #: True when the account is now gone.
    deleted: bool
    #: Masked, e.g. "98XXXXXX10".
    mobile_number: str
    expires_in_minutes: Optional[int] = None
    resend_available_in_seconds: Optional[int] = None
    #: Only while ENV != production.
    dev_otp: Optional[str] = None
    deleted_at: Optional[datetime] = None
