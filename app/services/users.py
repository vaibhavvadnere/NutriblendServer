"""
services/users.py — The account lifecycle, in one place.

Routers stay thin: they validate input and shape responses. Every rule about
what a user *is* and when they may move between states lives here, so the
same rule can't drift between signup, signin and verify.

Lifecycle
---------
    signup            verify-otp
    ──────▶ pending  ──────────▶ active ──────▶ blocked (admin only)
              │
              └─ auto-deleted after PENDING_USER_TTL_HOURS if never verified
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import status
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.database import users_collection
from app.errors import APIError, ErrorCode
from app.schemas.user import UserOut

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_BLOCKED = "blocked"


def to_user_out(doc: dict) -> UserOut:
    status_value = doc.get("status") or (
        STATUS_ACTIVE if doc.get("is_verified") else STATUS_PENDING
    )
    return UserOut(
        id=str(doc["_id"]),
        mobile_number=doc["mobile_number"],
        name=doc["name"],
        email=doc.get("email"),
        status=status_value,
        is_verified=status_value == STATUS_ACTIVE,
        created_at=doc["created_at"],
        verified_at=doc.get("verified_at"),
        last_login_at=doc.get("last_login_at"),
    )


async def get_by_mobile(mobile_number: str) -> Optional[dict]:
    return await users_collection.find_one({"mobile_number": mobile_number})


def ensure_not_blocked(user: dict) -> None:
    """Blocked accounts get a flat refusal with no explanation — telling someone
    why they were blocked just teaches them how to evade it."""
    if user.get("status") == STATUS_BLOCKED:
        raise APIError(
            status.HTTP_403_FORBIDDEN,
            ErrorCode.ACCOUNT_BLOCKED,
            "This account is not available. Please contact support.",
        )


async def _assert_email_free(email: Optional[str], except_mobile: Optional[str] = None) -> None:
    if not email or not settings.ENFORCE_UNIQUE_EMAIL:
        return
    query: dict[str, Any] = {"email": email}
    if except_mobile:
        query["mobile_number"] = {"$ne": except_mobile}
    if await users_collection.find_one(query):
        raise APIError(
            status.HTTP_409_CONFLICT,
            ErrorCode.EMAIL_IN_USE,
            "This email address is already linked to another account.",
        )


async def create_or_refresh_pending(
    mobile_number: str, name: str, email: Optional[str]
) -> dict:
    """
    The signup write.

    * No account yet            -> create a pending one.
    * Pending account exists    -> update the name/email and let them retry.
      (An abandoned signup must never lock a number out permanently.)
    * Active account exists     -> 409; they should be signing in instead.
    * Blocked account exists    -> 403.
    """
    now = datetime.now(timezone.utc)
    existing = await get_by_mobile(mobile_number)

    if existing:
        ensure_not_blocked(existing)
        if existing.get("status") == STATUS_ACTIVE or existing.get("is_verified"):
            raise APIError(
                status.HTTP_409_CONFLICT,
                ErrorCode.ACCOUNT_EXISTS,
                "An account with this mobile number already exists. Please sign in instead.",
            )

        await _assert_email_free(email, except_mobile=mobile_number)
        changes = {
            "name": name,
            "status": STATUS_PENDING,
            "is_verified": False,
            "updated_at": now,
            "pending_expires_at": now + timedelta(hours=settings.PENDING_USER_TTL_HOURS),
        }
        # Only overwrite the email when a new one is supplied. Retrying a signup
        # without the optional email field must not silently erase an address
        # the user already gave us.
        if email is not None:
            changes["email"] = email
        await users_collection.update_one({"_id": existing["_id"]}, {"$set": changes})
        return await users_collection.find_one({"_id": existing["_id"]})

    await _assert_email_free(email)
    doc = {
        "mobile_number": mobile_number,
        "name": name,
        "email": email,
        "status": STATUS_PENDING,
        "is_verified": False,
        "created_at": now,
        "updated_at": now,
        "verified_at": None,
        "last_login_at": None,
        "pending_expires_at": now + timedelta(hours=settings.PENDING_USER_TTL_HOURS),
    }
    try:
        result = await users_collection.insert_one(doc)
    except DuplicateKeyError:
        # Two signups for the same number raced each other and we lost. The
        # unique index did its job; re-read and treat it as the existing-account
        # case rather than returning a 500.
        existing = await get_by_mobile(mobile_number)
        if existing:
            return await create_or_refresh_pending(mobile_number, name, email)
        raise
    doc["_id"] = result.inserted_id
    return doc


async def get_for_signin(mobile_number: str) -> dict:
    """
    The signin read.

    A *pending* account is allowed to sign in: someone who abandoned signup and
    later taps "Sign In" should not hit a dead end. Verifying the OTP activates
    them, exactly as it would have during signup.
    """
    user = await get_by_mobile(mobile_number)
    if not user:
        raise APIError(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.ACCOUNT_NOT_FOUND,
            "No account found with this mobile number. Please sign up first.",
        )
    ensure_not_blocked(user)
    return user


async def activate(mobile_number: str) -> dict:
    """Called on successful OTP verification. Idempotent."""
    now = datetime.now(timezone.utc)

    # Stamp verified_at only the first time. Done as its own guarded write
    # rather than with $min, because $min against an existing null would keep
    # the null (null sorts below a date in BSON) and the field would never fill.
    await users_collection.update_one(
        {"mobile_number": mobile_number, "verified_at": None},
        {"$set": {"verified_at": now}},
    )

    return await users_collection.find_one_and_update(
        {"mobile_number": mobile_number},
        {
            "$set": {
                "status": STATUS_ACTIVE,
                "is_verified": True,
                "updated_at": now,
                "last_login_at": now,
            },
            # Activated accounts must stop being TTL candidates.
            "$unset": {"pending_expires_at": ""},
        },
        return_document=True,
    )


async def update_profile(mobile_number: str, name: Optional[str], email: Optional[str]) -> dict:
    changes: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if name is not None:
        changes["name"] = name
    if email is not None:
        await _assert_email_free(email, except_mobile=mobile_number)
        changes["email"] = email
    return await users_collection.find_one_and_update(
        {"mobile_number": mobile_number}, {"$set": changes}, return_document=True
    )
