"""
services/auth_service.py — Sign up, sign in, OTP verification and sessions.

    start_signup   { name, mobile_number, email?, state? } -> OTP sent
    start_signin   { mobile_number }               -> OTP sent  (also "resend")
    verify_otp     { mobile_number, otp }          -> access + refresh tokens
    refresh        { refresh_token }               -> new token pair (rotated)
    logout         { refresh_token }               -> ends one session
    logout_all     mobile_number                   -> ends every session

Signup and signin both end at the same verify_otp, so the app has one OTP
screen and one code path for "I am now logged in".

Everything here takes plain values: the router has already validated the
input and worked out the caller's IP.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.config import settings
from app.core.exceptions import (
    AccountGone,
    AccountNotFound,
    OtpAttemptsExceeded,
    OtpExpired,
    OtpIncorrect,
    OtpNotRequested,
    OtpSendFailed,
)
from app.core.security import (
    access_token_expires_in_seconds,
    create_access_token,
    generate_otp,
    hash_otp,
    verify_otp_hash,
)
from app.providers.sms import SMSDeliveryError, send_otp_sms
from app.repositories import otp_repo
from app.schemas.auth import AccessTokenResponse, OTPSentResponse, TokenResponse
from app.services import rate_limiter, token_service, user_service
from app.utils.phone import mask

logger = logging.getLogger("nutriblend.auth")


# ── Sending OTPs ─────────────────────────────────────────────────────────────

async def _throttle_otp(mobile_number: str, ip: str) -> None:
    """
    Consume OTP quota. Called at the very top of every OTP-sending flow —
    **before** the account is even looked up — so a request that is going to
    fail anyway still costs quota. That is precisely what makes probing for
    registered numbers slow and expensive; charging only for successes would
    leave enumeration free.
    """
    await rate_limiter.enforce(*rate_limiter.otp_send_rules(mobile_number, ip))


async def _issue_otp(mobile_number: str, is_new_account: bool) -> OTPSentResponse:
    """Shared tail of signup / signin / resend: generate, store, send.
    Throttling has already happened in _throttle_otp."""
    otp = generate_otp()
    now = datetime.now(timezone.utc)
    await otp_repo.save(
        mobile_number,
        otp_hash=hash_otp(otp),
        expires_at=now + timedelta(minutes=settings.OTP_EXPIRE_MINUTES),
        created_at=now,
    )

    try:
        await send_otp_sms(mobile_number, otp)
    except SMSDeliveryError as exc:
        # Don't leave a live OTP behind that the user never received.
        await otp_repo.delete_by_mobile(mobile_number)
        logger.error("OTP delivery failed for %s: %s", mask(mobile_number), exc)
        raise OtpSendFailed()

    return OTPSentResponse(
        mobile_number=mask(mobile_number),
        expires_in_minutes=settings.OTP_EXPIRE_MINUTES,
        resend_available_in_seconds=settings.OTP_COOLDOWN_SECONDS,
        is_new_account=is_new_account,
        dev_otp=None if settings.is_production else otp,
    )


async def start_signup(
    mobile_number: str, name: str, email: Optional[str], state: Optional[str], ip: str
) -> OTPSentResponse:
    """Create (or refresh) a pending account and send it an OTP."""
    await _throttle_otp(mobile_number, ip)
    await user_service.create_or_refresh_pending(mobile_number, name, email, state)
    return await _issue_otp(mobile_number, is_new_account=True)


async def start_signin(mobile_number: str, ip: str) -> OTPSentResponse:
    """Send an OTP to an existing (pending or active) account. Also serves
    "resend OTP", which gets no special treatment on the cooldown."""
    await _throttle_otp(mobile_number, ip)
    user = await user_service.get_for_signin(mobile_number)
    is_new = user.get("status") != user_service.STATUS_ACTIVE
    return await _issue_otp(mobile_number, is_new_account=is_new)


# ── Verifying OTPs ───────────────────────────────────────────────────────────

async def verify_otp(mobile_number: str, otp: str, ip: str) -> TokenResponse:
    """
    Check the OTP and log the user in. On success the account is activated,
    the used OTP is destroyed, and an access + refresh token pair is issued.
    """
    await rate_limiter.enforce(*rate_limiter.otp_verify_rules(ip))

    user = await user_service.get_by_mobile(mobile_number)
    if not user:
        raise AccountNotFound()
    user_service.ensure_not_blocked(user)

    record = await otp_repo.find_by_mobile(mobile_number)
    if not record:
        raise OtpNotRequested()

    expires_at = record["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise OtpExpired()

    if record["attempts"] >= settings.OTP_MAX_ATTEMPTS:
        raise OtpAttemptsExceeded()

    if not verify_otp_hash(otp, record["otp_hash"]):
        updated = await otp_repo.increment_attempts(record["_id"])
        attempts_left = max(0, settings.OTP_MAX_ATTEMPTS - updated["attempts"])
        if attempts_left == 0:
            await otp_repo.delete(record["_id"])
            raise OtpAttemptsExceeded()
        raise OtpIncorrect(attempts_remaining=attempts_left)

    user = await user_service.activate(mobile_number)
    await otp_repo.delete(record["_id"])
    # A successful login clears the resend cooldown for this number.
    await rate_limiter.reset_for("otp_cooldown", mobile_number)

    access_token = create_access_token(subject=user["mobile_number"])
    refresh = await token_service.issue_refresh_token(user["mobile_number"])
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh.token,
        expires_in=access_token_expires_in_seconds(),
        user=user_service.to_user_out(user),
    )


# ── Sessions ─────────────────────────────────────────────────────────────────

async def refresh(refresh_token: str) -> AccessTokenResponse:
    """Rotate a refresh token and issue a new access token."""
    mobile_number, new_refresh = await token_service.rotate_refresh_token(refresh_token)

    user = await user_service.get_by_mobile(mobile_number)
    if not user:
        raise AccountGone()
    user_service.ensure_not_blocked(user)

    return AccessTokenResponse(
        access_token=create_access_token(subject=mobile_number),
        refresh_token=new_refresh.token,
        expires_in=access_token_expires_in_seconds(),
    )


async def logout(refresh_token: str) -> None:
    """End one session. Unknown tokens are ignored (logout is idempotent)."""
    await token_service.revoke_refresh_token(refresh_token, reason="logout")


async def logout_all(mobile_number: str) -> int:
    """End every session for this user. Returns how many were revoked."""
    return await token_service.revoke_all_for_user(mobile_number)
