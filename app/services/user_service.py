"""
services/user_service.py — The account lifecycle, in one place.

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

from app.core.config import settings
from app.core.exceptions import (
    AccountBlocked,
    AccountExists,
    AccountNotFound,
    EmailInUse,
    InvalidUserId,
)
from app.repositories import user_repo
from app.schemas.user import UserOut

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_BLOCKED = "blocked"


def to_user_out(doc: dict) -> UserOut:
    # Documents written before `status` existed only have `is_verified`.
    status_value = doc.get("status") or (
        STATUS_ACTIVE if doc.get("is_verified") else STATUS_PENDING
    )
    return UserOut(
        id=str(doc["_id"]),
        mobile_number=doc["mobile_number"],
        name=doc["name"],
        email=doc.get("email"),
        state=doc.get("state"),
        status=status_value,
        is_verified=status_value == STATUS_ACTIVE,
        created_at=doc["created_at"],
        verified_at=doc.get("verified_at"),
        last_login_at=doc.get("last_login_at"),
    )


async def get_by_mobile(mobile_number: str) -> Optional[dict]:
    return await user_repo.find_by_mobile(mobile_number)


async def get_by_id(user_id: str) -> dict:
    """Fetch a user by id. Raises InvalidUserId / AccountNotFound."""
    if not user_repo.is_valid_id(user_id):
        raise InvalidUserId()
    doc = await user_repo.find_by_id(user_id)
    if not doc:
        raise AccountNotFound("User not found")
    return doc


def ensure_not_blocked(user: dict) -> None:
    if user.get("status") == STATUS_BLOCKED:
        raise AccountBlocked()


async def _assert_email_free(email: Optional[str], except_mobile: Optional[str] = None) -> None:
    if not email or not settings.ENFORCE_UNIQUE_EMAIL:
        return
    if await user_repo.email_taken(email, except_mobile=except_mobile):
        raise EmailInUse()


async def create_or_refresh_pending(
    mobile_number: str, name: str, email: Optional[str], state: Optional[str] = None
) -> dict:
    """
    The signup write.

    * No account yet            -> create a pending one.
    * Pending account exists    -> update the name/email/state and let them retry.
      (An abandoned signup must never lock a number out permanently.)
    * Active account exists     -> AccountExists; they should be signing in instead.
    * Blocked account exists    -> AccountBlocked.
    """
    now = datetime.now(timezone.utc)
    existing = await user_repo.find_by_mobile(mobile_number)

    if existing:
        ensure_not_blocked(existing)
        if existing.get("status") == STATUS_ACTIVE or existing.get("is_verified"):
            raise AccountExists()

        await _assert_email_free(email, except_mobile=mobile_number)
        changes = {
            "name": name,
            "status": STATUS_PENDING,
            "is_verified": False,
            "updated_at": now,
            "pending_expires_at": now + timedelta(hours=settings.PENDING_USER_TTL_HOURS),
        }
        if email is not None:
            changes["email"] = email
        if state is not None:
            changes["state"] = state
        return await user_repo.update_by_id(existing["_id"], changes)

    await _assert_email_free(email)
    doc = {
        "mobile_number": mobile_number,
        "name": name,
        "email": email,
        "state": state,
        "status": STATUS_PENDING,
        "is_verified": False,
        "created_at": now,
        "updated_at": now,
        "verified_at": None,
        "last_login_at": None,
        "pending_expires_at": now + timedelta(hours=settings.PENDING_USER_TTL_HOURS),
    }
    try:
        return await user_repo.insert(doc)
    except user_repo.DuplicateUser:
        # Lost a race with a concurrent signup for the same number: treat it as
        # the "pending account exists" case above.
        if await user_repo.find_by_mobile(mobile_number):
            return await create_or_refresh_pending(mobile_number, name, email, state)
        raise


async def get_for_signin(mobile_number: str) -> dict:
    """
    The signin read.

    A *pending* account is allowed to sign in: someone who abandoned signup and
    later taps "Sign In" should not hit a dead end. Verifying the OTP activates
    them, exactly as it would have during signup.
    """
    user = await user_repo.find_by_mobile(mobile_number)
    if not user:
        raise AccountNotFound()
    ensure_not_blocked(user)
    return user


async def activate(mobile_number: str) -> dict:
    """Called on successful OTP verification. Idempotent."""
    now = datetime.now(timezone.utc)
    await user_repo.set_verified_at_if_missing(mobile_number, now)
    return await user_repo.update_by_mobile(
        mobile_number,
        {
            "status": STATUS_ACTIVE,
            "is_verified": True,
            "updated_at": now,
            "last_login_at": now,
        },
        unset_fields=["pending_expires_at"],
    )


async def update_profile(
    mobile_number: str,
    name: Optional[str],
    email: Optional[str],
    state: Optional[str] = None,
) -> dict:
    changes: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if name is not None:
        changes["name"] = name
    if email is not None:
        await _assert_email_free(email, except_mobile=mobile_number)
        changes["email"] = email
    if state is not None:
        changes["state"] = state
    return await user_repo.update_by_mobile(mobile_number, changes)
