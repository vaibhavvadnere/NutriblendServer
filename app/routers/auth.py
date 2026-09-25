"""
routers/auth.py — Sign up, sign in, and the token lifecycle.

    Sign up screen      POST /auth/signup     { name, mobile_number, email? } -> OTP sent
    Sign in screen      POST /auth/signin     { mobile_number }               -> OTP sent
    "Resend" button     POST /auth/resend-otp { mobile_number }               -> OTP sent
    OTP screen          POST /auth/verify-otp { mobile_number, otp }          -> tokens
    Token expired       POST /auth/refresh    { refresh_token }               -> new tokens
    Log out             POST /auth/logout     { refresh_token }
    Log out everywhere  POST /auth/logout-all (Bearer access token)

Both signup and signin end at the same verify-otp call, so the app has one
OTP screen and one code path for "I am now logged in".
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, status

from app.config import settings
from app.database import otps_collection
from app.deps import get_current_user
from app.errors import APIError, ErrorCode
from app.schemas.auth import (
    AccessTokenResponse,
    MessageResponse,
    OTPSentResponse,
    OTPVerify,
    RefreshRequest,
    SigninRequest,
    SignupRequest,
    TokenResponse,
)
from app.services import users as user_service
from app.utils import ratelimit
from app.utils.phone import mask
from app.utils.security import (
    access_token_expires_in_seconds,
    create_access_token,
    generate_otp,
    hash_otp,
    verify_otp_hash,
)
from app.utils.sms import SMSDeliveryError, send_otp_sms
from app.utils.tokens import (
    RefreshTokenError,
    issue_refresh_token,
    revoke_all_for_user,
    revoke_refresh_token,
    rotate_refresh_token,
)

logger = logging.getLogger("nutriblend.auth")

router = APIRouter(prefix=f"{settings.API_PREFIX}/auth", tags=["auth"])


async def _throttle_otp(mobile_number: str, request: Request) -> None:
    """
    Consume OTP quota. Called at the very top of every OTP-sending endpoint —
    **before** the account is even looked up — so a request that is going to
    fail anyway still costs quota. That is precisely what makes probing for
    registered numbers slow and expensive; charging only for successes would
    leave enumeration free.
    """
    await ratelimit.enforce(
        *ratelimit.otp_send_rules(mobile_number, ratelimit.client_ip(request))
    )


async def _issue_otp(mobile_number: str, request: Request, is_new_account: bool) -> OTPSentResponse:
    """Shared tail of signup / signin / resend: generate, store, send.
    Throttling has already happened in _throttle_otp."""
    otp = generate_otp()
    now = datetime.now(timezone.utc)

    await otps_collection.update_one(
        {"mobile_number": mobile_number},
        {
            "$set": {
                "otp_hash": hash_otp(otp),
                "expires_at": now + timedelta(minutes=settings.OTP_EXPIRE_MINUTES),
                "attempts": 0,
                "created_at": now,
            }
        },
        upsert=True,
    )

    try:
        await send_otp_sms(mobile_number, otp)
    except SMSDeliveryError as exc:
        # Never leave a live OTP behind for a message that was never delivered.
        # The pending *user* record is deliberately kept, so a retry doesn't
        # make them type their name again.
        await otps_collection.delete_one({"mobile_number": mobile_number})
        logger.error("OTP delivery failed for %s: %s", mask(mobile_number), exc)
        raise APIError(
            status.HTTP_502_BAD_GATEWAY,
            ErrorCode.OTP_SEND_FAILED,
            "We couldn't send the OTP right now. Please try again in a moment.",
        )

    return OTPSentResponse(
        message="OTP sent successfully",
        mobile_number=mask(mobile_number),
        expires_in_minutes=settings.OTP_EXPIRE_MINUTES,
        resend_available_in_seconds=settings.OTP_COOLDOWN_SECONDS,
        is_new_account=is_new_account,
        dev_otp=None if settings.is_production else otp,
    )


@router.post("/signup", response_model=OTPSentResponse, status_code=status.HTTP_201_CREATED)
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
    await _throttle_otp(payload.mobile_number, request)
    await user_service.create_or_refresh_pending(
        payload.mobile_number, payload.name, payload.email
    )
    return await _issue_otp(payload.mobile_number, request, is_new_account=True)


@router.post("/signin", response_model=OTPSentResponse)
async def signin(payload: SigninRequest, request: Request):
    """
    Start login for an existing account and send an OTP.

    A *pending* account may sign in — someone who abandoned signup and later
    taps "Sign In" shouldn't hit a dead end; verifying activates them.

    - `404 ACCOUNT_NOT_FOUND` — send them to the sign-up screen.
    - `403 ACCOUNT_BLOCKED` / `429 RATE_LIMITED` as above.
    """
    await _throttle_otp(payload.mobile_number, request)
    user = await user_service.get_for_signin(payload.mobile_number)
    is_new = user.get("status") != user_service.STATUS_ACTIVE
    return await _issue_otp(payload.mobile_number, request, is_new_account=is_new)


@router.post("/resend-otp", response_model=OTPSentResponse)
async def resend_otp(payload: SigninRequest, request: Request):
    """
    Resend an OTP to an account that already exists (pending or active).

    Subject to the same cooldown as everything else — this is the single most
    abused endpoint in any OTP system, so it gets no special treatment.
    """
    await _throttle_otp(payload.mobile_number, request)
    user = await user_service.get_for_signin(payload.mobile_number)
    is_new = user.get("status") != user_service.STATUS_ACTIVE
    return await _issue_otp(payload.mobile_number, request, is_new_account=is_new)


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(payload: OTPVerify, request: Request):
    """
    Verify the OTP and log the user in. Used by **both** signup and signin.

    On success the account is activated, the used OTP is destroyed, and an
    access + refresh token pair is issued.
    """
    await ratelimit.enforce(*ratelimit.otp_verify_rules(ratelimit.client_ip(request)))

    user = await user_service.get_by_mobile(payload.mobile_number)
    if not user:
        raise APIError(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.ACCOUNT_NOT_FOUND,
            "No account found with this mobile number. Please sign up first.",
        )
    user_service.ensure_not_blocked(user)

    record = await otps_collection.find_one({"mobile_number": payload.mobile_number})
    if not record:
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCode.OTP_NOT_REQUESTED,
            "No OTP request found. Please request a new OTP.",
        )

    now = datetime.now(timezone.utc)
    expires_at = record["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now > expires_at:
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCode.OTP_EXPIRED,
            "This OTP has expired. Please request a new one.",
        )

    if record["attempts"] >= settings.OTP_MAX_ATTEMPTS:
        raise APIError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            ErrorCode.OTP_ATTEMPTS_EXCEEDED,
            "Too many incorrect attempts. Please request a new OTP.",
        )

    if not verify_otp_hash(payload.otp, record["otp_hash"]):
        updated = await otps_collection.find_one_and_update(
            {"_id": record["_id"]}, {"$inc": {"attempts": 1}}, return_document=True
        )
        attempts_left = max(0, settings.OTP_MAX_ATTEMPTS - updated["attempts"])
        if attempts_left == 0:
            # Burn the OTP outright rather than leaving a guessable code alive
            # until its expiry. Forces a resend, which is rate limited.
            await otps_collection.delete_one({"_id": record["_id"]})
            raise APIError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                ErrorCode.OTP_ATTEMPTS_EXCEEDED,
                "Too many incorrect attempts. Please request a new OTP.",
            )
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCode.OTP_INCORRECT,
            "That OTP is incorrect. Please check and try again.",
            details={"attempts_remaining": attempts_left},
        )

    # Success.
    user = await user_service.activate(payload.mobile_number)
    await otps_collection.delete_one({"_id": record["_id"]})
    # Don't hold a user to the resend cooldown from their own successful login.
    await ratelimit.reset_for("otp_cooldown", payload.mobile_number)

    access_token = create_access_token(subject=user["mobile_number"])
    refresh = await issue_refresh_token(user["mobile_number"])

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh.token,
        expires_in=access_token_expires_in_seconds(),
        user=user_service.to_user_out(user),
    )


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh_tokens(payload: RefreshRequest):
    """
    Exchange a refresh token for a new access token.

    The refresh token is **rotated**: the one you send is revoked and a new one
    comes back. Store the new one. Replaying an old one is treated as theft and
    revokes the whole session.
    """
    try:
        mobile_number, new_refresh = await rotate_refresh_token(payload.refresh_token)
    except RefreshTokenError as exc:
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.REFRESH_TOKEN_REUSED if exc.reuse_detected else ErrorCode.REFRESH_TOKEN_INVALID,
            exc.message,
        )

    user = await user_service.get_by_mobile(mobile_number)
    if not user:
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.ACCOUNT_NOT_FOUND,
            "This account no longer exists.",
        )
    user_service.ensure_not_blocked(user)

    return AccessTokenResponse(
        access_token=create_access_token(subject=mobile_number),
        refresh_token=new_refresh.token,
        expires_in=access_token_expires_in_seconds(),
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(payload: RefreshRequest):
    """End this session. The access token stays valid until it expires (at most
    ACCESS_TOKEN_EXPIRE_MINUTES), but it can no longer be renewed."""
    await revoke_refresh_token(payload.refresh_token, reason="logout")
    # Always report success — whether that token existed is not the caller's business.
    return MessageResponse(message="Logged out")


@router.post("/logout-all", response_model=MessageResponse)
async def logout_all(current_user: dict = Depends(get_current_user)):
    """Log out of every device. Requires a valid access token."""
    revoked = await revoke_all_for_user(current_user["mobile_number"])
    return MessageResponse(message=f"Logged out of {revoked} session(s)")
