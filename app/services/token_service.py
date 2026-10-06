"""
services/token_service.py — Refresh-token lifecycle (issue, rotate, revoke).

A refresh token belongs to a *family*: the token issued at login starts a family,
and every rotation stays in that family. That's what makes theft detectable —
if a token that was already rotated away is presented again, either the
legitimate client or the attacker is replaying an old token, so the whole family
is revoked and that session ends.

Only the token's SHA-256 hash is stored (see repositories/refresh_token_repo.py).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.core.exceptions import RefreshTokenInvalid, RefreshTokenReused
from app.core.security import (
    generate_refresh_token,
    hash_token,
    new_token_family_id,
    refresh_token_expiry,
)
from app.repositories import refresh_token_repo


@dataclass
class IssuedRefreshToken:
    token: str
    family_id: str
    expires_at: datetime


async def issue_refresh_token(mobile_number: str, family_id: Optional[str] = None) -> IssuedRefreshToken:
    """Mint a new refresh token, starting a new family unless one is given."""
    token = generate_refresh_token()
    family = family_id or new_token_family_id()
    expires_at = refresh_token_expiry()

    await refresh_token_repo.insert(
        {
            "token_hash": hash_token(token),
            "mobile_number": mobile_number,
            "family_id": family,
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
            "revoked_at": None,
            "revoked_reason": None,
            "replaced_by": None,
        }
    )
    return IssuedRefreshToken(token=token, family_id=family, expires_at=expires_at)


async def rotate_refresh_token(token: str) -> tuple[str, IssuedRefreshToken]:
    """
    Validate a refresh token and swap it for a fresh one in the same family.

    Returns (mobile_number, new_token). Raises RefreshTokenInvalid if the token
    is unknown or expired, and RefreshTokenReused (after killing the family) if
    it was already used.
    """
    record = await refresh_token_repo.find_by_hash(hash_token(token))
    if record is None:
        raise RefreshTokenInvalid()

    if record.get("revoked_at") is not None:
        # This token was already rotated away or explicitly revoked. Presenting
        # it again means it leaked — burn the whole session.
        await revoke_family(record["family_id"], reason="reuse_detected")
        raise RefreshTokenReused()

    if _as_utc(record["expires_at"]) <= datetime.now(timezone.utc):
        raise RefreshTokenInvalid("Refresh token has expired. Please log in again.")

    new_token = await issue_refresh_token(record["mobile_number"], family_id=record["family_id"])
    await refresh_token_repo.mark_rotated(
        record["_id"], replaced_by=hash_token(new_token.token), when=datetime.now(timezone.utc)
    )
    return record["mobile_number"], new_token


async def revoke_refresh_token(token: str, reason: str = "logout") -> bool:
    """Revoke one token (and, with it, its family). True if something was revoked."""
    record = await refresh_token_repo.find_by_hash(hash_token(token))
    if record is None:
        return False
    await revoke_family(record["family_id"], reason=reason)
    return True


async def revoke_family(family_id: str, reason: str) -> int:
    return await refresh_token_repo.revoke_family(family_id, reason, datetime.now(timezone.utc))


async def revoke_all_for_user(mobile_number: str, reason: str = "logout_all") -> int:
    """Log the user out of every device."""
    return await refresh_token_repo.revoke_all_for_mobile(
        mobile_number, reason, datetime.now(timezone.utc)
    )


async def active_session_count(mobile_number: str) -> int:
    return await refresh_token_repo.count_active(mobile_number, datetime.now(timezone.utc))


def _as_utc(value: datetime) -> datetime:
    """Mongo hands back naive datetimes; treat them as UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
