"""
utils/tokens.py — Refresh-token lifecycle (issue, rotate, revoke).

A refresh token belongs to a *family*: the token issued at login starts a family,
and every rotation stays in that family. That's what makes theft detectable —
if a token that was already rotated away is presented again, either the
legitimate client or the attacker is replaying an old token, so the whole family
is revoked and that session ends.

Stored document shape (collection: refresh_tokens):
    {
      token_hash:    sha256 of the opaque token   (unique index)
      mobile_number: who it belongs to
      family_id:     ties rotations of one login session together
      created_at, expires_at (TTL index),
      revoked_at:    None while usable
      revoked_reason: "rotated" | "logout" | "logout_all" | "reuse_detected"
      replaced_by:   token_hash of the token that superseded this one
    }
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.database import refresh_tokens_collection
from app.utils.security import (
    generate_refresh_token,
    hash_token,
    new_token_family_id,
    refresh_token_expiry,
)


class RefreshTokenError(Exception):
    """Raised when a refresh token is unknown, expired, or revoked."""

    def __init__(self, message: str, reuse_detected: bool = False):
        super().__init__(message)
        self.message = message
        self.reuse_detected = reuse_detected


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

    await refresh_tokens_collection.insert_one(
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

    Returns (mobile_number, new_token). Raises RefreshTokenError if the token is
    unknown, expired or already used — and in the reuse case, kills the family.
    """
    record = await refresh_tokens_collection.find_one({"token_hash": hash_token(token)})
    if record is None:
        raise RefreshTokenError("Invalid refresh token")

    if record.get("revoked_at") is not None:
        # This token was already rotated away or explicitly revoked. Presenting
        # it again means it leaked — burn the whole session.
        await revoke_family(record["family_id"], reason="reuse_detected")
        raise RefreshTokenError(
            "This refresh token was already used. For your security all sessions "
            "from this login have been revoked — please log in again.",
            reuse_detected=True,
        )

    if _as_utc(record["expires_at"]) <= datetime.now(timezone.utc):
        raise RefreshTokenError("Refresh token has expired. Please log in again.")

    new_token = await issue_refresh_token(record["mobile_number"], family_id=record["family_id"])

    await refresh_tokens_collection.update_one(
        {"_id": record["_id"]},
        {
            "$set": {
                "revoked_at": datetime.now(timezone.utc),
                "revoked_reason": "rotated",
                "replaced_by": hash_token(new_token.token),
            }
        },
    )
    return record["mobile_number"], new_token


async def revoke_refresh_token(token: str, reason: str = "logout") -> bool:
    """Revoke one token (and, with it, its family). True if something was revoked."""
    record = await refresh_tokens_collection.find_one({"token_hash": hash_token(token)})
    if record is None:
        return False
    await revoke_family(record["family_id"], reason=reason)
    return True


async def revoke_family(family_id: str, reason: str) -> int:
    result = await refresh_tokens_collection.update_many(
        {"family_id": family_id, "revoked_at": None},
        {"$set": {"revoked_at": datetime.now(timezone.utc), "revoked_reason": reason}},
    )
    return result.modified_count


async def revoke_all_for_user(mobile_number: str, reason: str = "logout_all") -> int:
    """Log the user out of every device."""
    result = await refresh_tokens_collection.update_many(
        {"mobile_number": mobile_number, "revoked_at": None},
        {"$set": {"revoked_at": datetime.now(timezone.utc), "revoked_reason": reason}},
    )
    return result.modified_count


async def active_session_count(mobile_number: str) -> int:
    return await refresh_tokens_collection.count_documents(
        {
            "mobile_number": mobile_number,
            "revoked_at": None,
            "expires_at": {"$gt": datetime.now(timezone.utc)},
        }
    )


def _as_utc(value: datetime) -> datetime:
    """Mongo hands back naive datetimes; treat them as UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
