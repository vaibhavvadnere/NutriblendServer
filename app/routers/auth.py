"""
routers/auth.py — HTTP endpoints for sign up, sign in, and the token lifecycle.

    Sign up screen      POST /auth/signup     { name, mobile_number, email?, state? } -> OTP sent
    Sign in screen      POST /auth/signin     { mobile_number }               -> OTP sent
    "Resend" button     POST /auth/resend-otp { mobile_number }               -> OTP sent
    OTP screen          POST /auth/verify-otp { mobile_number, otp }          -> tokens
    Token expired       POST /auth/refresh    { refresh_token }               -> new tokens
    Log out             POST /auth/logout     { refresh_token }
    Log out everywhere  POST /auth/logout-all (Bearer access token)

Both signup and signin end at the same verify-otp call, so the app has one
OTP screen and one code path for "I am now logged in".

This module is HTTP only. The flows themselves live in services/auth_service.py.
"""

from fastapi import APIRouter, Depends, Request, status

from app.core.config import settings
from app.core.deps import client_ip, get_current_user
from app.schemas.auth import (
    AccessTokenResponse,
    LogoutAllResponse,
    OTPSentResponse,
    OTPVerify,
    RefreshRequest,
    SigninRequest,
    SignupRequest,
    TokenResponse,
)
from app.schemas.common import ERROR_RESPONSES, ApiResponse, EmptyData, ok
from app.services import auth_service

OTP_SENT = "OTP sent successfully"

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/auth", tags=["auth"], responses=ERROR_RESPONSES
)


@router.post("/signup", response_model=ApiResponse[OTPSentResponse], status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, request: Request):
    """
    Start registration. Creates a **pending** account and sends an OTP.

    The account only becomes usable once the OTP is verified, so nobody can
    reserve a mobile number they don't control. An abandoned signup is
    overwritten by the next attempt, and auto-deleted after
    `PENDING_USER_TTL_HOURS`.

    - `409 ACCOUNT_EXISTS` — already registered; send them to the sign-in screen.
    - `409 EMAIL_IN_USE` — that email belongs to another account.
    - `403 ACCOUNT_BLOCKED` — barred account.
    - `429 RATE_LIMITED` — too many OTP requests; `Retry-After` says when.
    """
    data = await auth_service.start_signup(
        payload.mobile_number, payload.name, payload.email, payload.state, client_ip(request)
    )
    return ok(data, OTP_SENT)


@router.post("/signin", response_model=ApiResponse[OTPSentResponse])
async def signin(payload: SigninRequest, request: Request):
    """
    Start login for an existing account and send an OTP.

    A *pending* account may sign in — someone who abandoned signup and later
    taps "Sign In" shouldn't hit a dead end; verifying activates them.

    - `404 ACCOUNT_NOT_FOUND` — send them to the sign-up screen.
    - `403 ACCOUNT_BLOCKED` / `429 RATE_LIMITED` as above.
    """
    return ok(await auth_service.start_signin(payload.mobile_number, client_ip(request)), OTP_SENT)


@router.post("/resend-otp", response_model=ApiResponse[OTPSentResponse])
async def resend_otp(payload: SigninRequest, request: Request):
    """
    Resend an OTP to an account that already exists (pending or active).

    Subject to the same cooldown as everything else — this is the single most
    abused endpoint in any OTP system, so it gets no special treatment.
    """
    return ok(await auth_service.start_signin(payload.mobile_number, client_ip(request)), OTP_SENT)


@router.post("/verify-otp", response_model=ApiResponse[TokenResponse])
async def verify_otp(payload: OTPVerify, request: Request):
    """
    Verify the OTP and log the user in. Used by **both** signup and signin.

    On success the account is activated, the used OTP is destroyed, and an
    access + refresh token pair is issued.
    """
    data = await auth_service.verify_otp(payload.mobile_number, payload.otp, client_ip(request))
    return ok(data, "OTP verified successfully")


@router.post("/refresh", response_model=ApiResponse[AccessTokenResponse])
async def refresh_tokens(payload: RefreshRequest):
    """
    Exchange a refresh token for a new access token.

    The refresh token is **rotated**: the one you send is revoked and a new one
    comes back. Store the new one. Replaying an old one is treated as theft and
    revokes the whole session.
    """
    return ok(await auth_service.refresh(payload.refresh_token), "Token refreshed successfully")


@router.post("/logout", response_model=ApiResponse[EmptyData])
async def logout(payload: RefreshRequest):
    """End this session. The access token stays valid until it expires (at most
    ACCESS_TOKEN_EXPIRE_MINUTES), but it can no longer be renewed."""
    await auth_service.logout(payload.refresh_token)
    return ok(EmptyData(), "Logged out")


@router.post("/logout-all", response_model=ApiResponse[LogoutAllResponse])
async def logout_all(current_user: dict = Depends(get_current_user)):
    """Log out of every device. Requires a valid access token."""
    revoked = await auth_service.logout_all(current_user["mobile_number"])
    return ok(LogoutAllResponse(revoked_sessions=revoked), f"Logged out of {revoked} session(s)")
