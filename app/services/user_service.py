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

Roles
-----
Every account has a `role`: "user" (default) or "admin". Documents written
before roles existed have no field and count as "user".

    * Signup and every profile endpoint write role "user" or leave it alone.
    * create_admin() is the ONLY code that writes role "admin", and it is only
      reachable through POST /admin/admins.
    * An admin account starts pending *without* an expiry, and becomes active
      the first time its owner verifies an OTP, like any other account.
    * Public signup can never overwrite an admin account, even a pending one.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.config import settings
from app.core.exceptions import (
    AccountBlocked,
    AccountDeleted,
    AccountExists,
    AccountNotFound,
    CannotDeleteAdmin,
    EmailInUse,
    InvalidUserId,
)
from app.repositories import user_repo
from app.schemas.user import UserOut

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_BLOCKED = "blocked"
STATUS_DELETED = "deleted"

ROLE_USER = "user"
ROLE_ADMIN = "admin"


def role_of(doc: dict) -> str:
    return doc.get("role") or ROLE_USER


def is_admin_account(doc: dict) -> bool:
    return role_of(doc) == ROLE_ADMIN


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


def ensure_usable(user: dict) -> None:
    """
    Refuse an account that can no longer be used, for any reason.

    Blocked: a flat refusal with no explanation — telling someone why they were
    blocked just teaches them how to evade it.

    Deleted: the opposite. The owner asked for this and needs to know the number
    is not simply unregistered, or they will keep trying to sign up and get
    "no account found", which looks like a bug.
    """
    status = user.get("status")
    if status == STATUS_BLOCKED:
        raise AccountBlocked()
    if status == STATUS_DELETED:
        raise AccountDeleted()



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
    * Deleted account exists    -> AccountDeleted. The number stays claimed by the
      tombstone, so signing up again needs an admin to release it.
    """
    now = datetime.now(timezone.utc)
    existing = await user_repo.find_by_mobile(mobile_number)

    if existing:
        ensure_usable(existing)
        if (
            existing.get("status") == STATUS_ACTIVE
            or existing.get("is_verified")
            or is_admin_account(existing)  # a pending admin must never be overwritten
        ):
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
        "role": ROLE_USER,
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
    ensure_usable(user)
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


async def soft_delete(mobile_number: str) -> dict:
    """
    Delete an account at its owner's request.

    "Soft": the row survives as a tombstone (status `deleted`, with
    `deleted_at` and `deleted_mobile_number`) so you keep an audit trail, but
    the name and email are scrubbed and the mobile number is freed — see
    user_repo.soft_delete for why the number cannot stay in place.

    Refusals happen before anything is written:
      * admin accounts — deleting the last admin would lock the dashboard out;
      * blocked accounts — otherwise a banned user could delete and re-register
        with the same number, which is exactly what the ban was stopping.
    """
    user = await get_by_mobile(mobile_number)
    if not user:
        raise AccountNotFound()
    if is_admin_account(user):
        raise CannotDeleteAdmin()
    if user.get("status") == STATUS_BLOCKED:
        raise AccountBlocked()
    if user.get("status") == STATUS_DELETED:
        raise AccountDeleted()

    deleted = await user_repo.soft_delete(mobile_number, datetime.now(timezone.utc))
    if deleted is None:
        raise AccountNotFound()
    return deleted


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


async def create_admin(
    mobile_number: str, name: str, email: Optional[str], created_ip: str
) -> dict:
    """
    Create a brand-new admin account. The only place role "admin" is written.

    * Any existing account for this number (pending, active or blocked, user or
      admin) -> AccountExists. Admins are never made by promoting an account.
    * The account starts pending with NO expiry (it is not an abandoned signup)
      and becomes active when its owner signs in with an OTP, which proves they
      hold the number.
    """
    if await user_repo.find_by_mobile(mobile_number):
        raise AccountExists("An account with this mobile number already exists.")
    await _assert_email_free(email)

    now = datetime.now(timezone.utc)
    doc = {
        "mobile_number": mobile_number,
        "name": name,
        "email": email,
        "state": None,
        "role": ROLE_ADMIN,
        "status": STATUS_PENDING,
        "is_verified": False,
        "created_at": now,
        "updated_at": now,
        "verified_at": None,
        "last_login_at": None,
        "created_via": "admin_setup_api",
        "created_ip": created_ip,
    }
    try:
        return await user_repo.insert(doc)
    except user_repo.DuplicateUser as exc:
        # Lost a race with a signup (or another create) for the same number/email.
        raise AccountExists("An account with this mobile number or email already exists.") from exc
