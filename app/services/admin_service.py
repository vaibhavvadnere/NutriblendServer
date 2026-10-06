"""
services/admin_service.py — What the admin dashboard can see and do.

Read-mostly: statistics, the user list, one user's details. The only write is
blocking / unblocking an account, which follows the same lifecycle rules as
user_service (see its docstring).

Statistics and the user list cover app users only: admin accounts are left out.
"""

import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.exceptions import InvalidStatusChange
from app.repositories import user_repo
from app.schemas.admin import (
    AdminStats,
    AdminUserDetail,
    AdminUserList,
    DailyCount,
    UsersByStatus,
)
from app.services import token_service, user_service, video_service

MAX_PAGE_SIZE = 100
SIGNUP_CHART_DAYS = 30


def _local_midnight_utc(day: date, tz: ZoneInfo) -> datetime:
    """Start of `day` in `tz`, as a UTC datetime (what Mongo stores)."""
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)


async def get_stats() -> AdminStats:
    tz_name = settings.ADMIN_TIMEZONE
    tz = ZoneInfo(tz_name)
    now = datetime.now(timezone.utc)
    today = now.astimezone(tz).date()

    today_start = _local_midnight_utc(today, tz)
    week_start = _local_midnight_utc(today - timedelta(days=6), tz)
    chart_start_day = today - timedelta(days=SIGNUP_CHART_DAYS - 1)

    by_status = await user_repo.count_by_status()
    per_day = await user_repo.signups_per_day(_local_midnight_utc(chart_start_day, tz), tz_name)

    return AdminStats(
        total_users=await user_repo.count_all(),
        users_by_status=UsersByStatus(**{k: v for k, v in by_status.items() if k in UsersByStatus.model_fields}),
        signups_today=await user_repo.count_created_since(today_start),
        signups_last_7_days=await user_repo.count_created_since(week_start),
        active_last_7_days=await user_repo.count_logged_in_since(week_start),
        signups_by_day=[
            DailyCount(date=day, count=per_day.get(day.isoformat(), 0))
            for day in (chart_start_day + timedelta(days=i) for i in range(SIGNUP_CHART_DAYS))
        ],
        timezone=tz_name,
        videos=await video_service.counts(),
    )


async def list_users(
    query: Optional[str], status: Optional[str], page: int, page_size: int
) -> AdminUserList:
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    docs, total = await user_repo.search(query, status, (page - 1) * page_size, page_size)
    return AdminUserList(
        items=[user_service.to_user_out(doc) for doc in docs],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(math.ceil(total / page_size), 1),
    )


async def get_user_detail(user_id: str) -> AdminUserDetail:
    doc = await user_service.get_by_id(user_id)
    user = user_service.to_user_out(doc)
    return AdminUserDetail(
        **user.model_dump(),
        role=user_service.role_of(doc),
        active_sessions=await token_service.active_session_count(doc["mobile_number"]),
        is_admin=user_service.is_admin_account(doc),
    )


async def set_status(user_id: str, new_status: str, admin: dict) -> AdminUserDetail:
    """
    Block or unblock an account.

    * blocked: the account can no longer sign in, and every session is revoked.
      Its current access token also stops working at once, because
      get_current_user rejects blocked accounts on every request.
    * active:  unblocks. An account that never verified its number goes back to
      pending (and expires as usual) rather than being made active.

    Admins cannot be blocked from here: that would lock you out of the dashboard.
    """
    doc = await user_service.get_by_id(user_id)
    current = user_service.to_user_out(doc).status
    now = datetime.now(timezone.utc)

    if new_status == user_service.STATUS_BLOCKED:
        if user_service.is_admin_account(doc) or doc["_id"] == admin["_id"]:
            raise InvalidStatusChange("Admin accounts cannot be blocked.")
        if current != user_service.STATUS_BLOCKED:
            await user_repo.update_by_id(
                doc["_id"], {"status": user_service.STATUS_BLOCKED, "updated_at": now, "blocked_at": now}
            )
            await token_service.revoke_all_for_user(doc["mobile_number"], reason="blocked_by_admin")

    elif new_status == user_service.STATUS_ACTIVE:
        if current == user_service.STATUS_PENDING:
            raise InvalidStatusChange(
                "A pending account becomes active only when its owner verifies the OTP."
            )
        if current == user_service.STATUS_BLOCKED:
            if doc.get("verified_at"):
                changes = {"status": user_service.STATUS_ACTIVE, "is_verified": True}
            else:
                changes = {
                    "status": user_service.STATUS_PENDING,
                    "is_verified": False,
                    "pending_expires_at": now + timedelta(hours=settings.PENDING_USER_TTL_HOURS),
                }
            await user_repo.update_by_id(doc["_id"], {**changes, "updated_at": now})

    else:  # guarded by the request schema; kept for direct callers
        raise InvalidStatusChange()

    return await get_user_detail(user_id)
