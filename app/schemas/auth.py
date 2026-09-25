"""
schemas/auth.py — Request/response models for signup, signin and tokens.
"""

from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.user import UserOut, validate_mobile_field, validate_name_field


class SignupRequest(BaseModel):
    """POST /auth/signup — creates a pending account and sends an OTP."""
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
    message: str
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
    user: UserOut


class AccessTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class MessageResponse(BaseModel):
    message: str


# Kept so any existing caller of the old endpoint keeps working.
OTPRequest = SigninRequest
