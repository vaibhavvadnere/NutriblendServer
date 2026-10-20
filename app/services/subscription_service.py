"""
services/subscription_service.py — Who has access, and until when.

Rules
-----
* One subscription document per user; every purchase EXTENDS it. Buying while
  still active adds the new days after the current expiry (nothing is lost);
  buying after expiry starts from now.
* Refund: the refunded order's days are taken back off the expiry (never below
  "now"), so stacked purchases keep the rest.
* Account deletion / admin revoke end access immediately.
* Expiry needs no background job: access is simply `expires_at > now`.

Every change appends to `history` (who / when / why) for the audit trail.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId

from app.repositories import subscription_repo
from app.repositories.subscription_repo import ConcurrentUpdate
from app.schemas.payment import SubscriptionOut

logger = logging.getLogger("nutriblend.subscriptions")

SOURCE_PAYMENT = "payment"
SOURCE_ADMIN = "admin"

_MAX_RETRIES = 5


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Mongo hands back naive datetimes (stored as UTC)."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def to_subscription_out(doc: Optional[dict], now: Optional[datetime] = None) -> SubscriptionOut:
    now = now or datetime.now(timezone.utc)
    if not doc:
        return SubscriptionOut(is_active=False)
    expires_at = _utc(doc.get("expires_at"))
    active = bool(expires_at and expires_at > now)
    days_left = max(0, (expires_at - now).days + (1 if (expires_at - now).seconds else 0)) if active else 0
    return SubscriptionOut(
        is_active=active,
        plan_id=doc.get("plan_id"),
        plan_name=doc.get("plan_name"),
        started_at=_utc(doc.get("started_at")),
        expires_at=expires_at,
        days_left=days_left,
    )


async def get_for_user(user_id: ObjectId) -> SubscriptionOut:
    return to_subscription_out(await subscription_repo.find_by_user(user_id))


async def has_active(user_id: ObjectId) -> bool:
    return (await get_for_user(user_id)).is_active


async def _write(user_id: ObjectId, compute, history_entry: dict) -> dict:
    """Read → compute new fields → compare-and-set, retrying on a concurrent write.
    `compute(current_doc_or_None, now)` returns the fields to set."""
    for _ in range(_MAX_RETRIES):
        now = datetime.now(timezone.utc)
        current = await subscription_repo.find_by_user(user_id)
        fields = compute(current, now)
        entry = {**history_entry, "at": now}
        try:
            if current is None:
                return await subscription_repo.insert_new(
                    {"user_id": user_id, **fields, "history": [entry], "created_at": now, "updated_at": now}
                )
            return await subscription_repo.update_if_version(
                user_id, current.get("version", 1), {**fields, "updated_at": now}, entry
            )
        except ConcurrentUpdate:
            continue
    raise RuntimeError(f"Subscription for {user_id} kept changing underneath us; giving up.")


async def extend(
    user_id: ObjectId,
    days: int,
    plan_id: Optional[str],
    plan_name: Optional[str],
    source: str,
    order_id: Optional[str] = None,
    by: Optional[str] = None,
    note: Optional[str] = None,
) -> SubscriptionOut:
    """Add `days` of access (purchase or admin grant)."""
    def compute(current: Optional[dict], now: datetime) -> dict:
        expires = _utc(current.get("expires_at")) if current else None
        still_active = bool(expires and expires > now)
        base = expires if still_active else now
        return {
            "plan_id": plan_id or (current or {}).get("plan_id"),
            "plan_name": plan_name or (current or {}).get("plan_name"),
            "started_at": _utc(current.get("started_at")) if (still_active and current) else now,
            "expires_at": base + timedelta(days=days),
            "source": source,
            "last_order_id": order_id or (current or {}).get("last_order_id"),
            "ended_reason": None,
        }

    doc = await _write(
        user_id, compute,
        {"action": "extend", "days": days, "source": source, "order_id": order_id, "by": by, "note": note},
    )
    logger.info("Subscription extended: user=%s days=%d source=%s order=%s", user_id, days, source, order_id)
    return to_subscription_out(doc)


async def take_back_days(user_id: ObjectId, days: int, order_id: str) -> Optional[SubscriptionOut]:
    """A refunded purchase: remove its days (expiry never moves into the past)."""
    if await subscription_repo.find_by_user(user_id) is None:
        return None

    def compute(current: Optional[dict], now: datetime) -> dict:
        expires = _utc((current or {}).get("expires_at")) or now
        new_expires = max(now, expires - timedelta(days=days))
        return {"expires_at": new_expires, **({"ended_reason": "refund"} if new_expires <= now else {})}

    doc = await _write(user_id, compute, {"action": "refund", "days": -days, "order_id": order_id})
    logger.warning("Subscription reduced by refund: user=%s days=%d order=%s", user_id, days, order_id)
    return to_subscription_out(doc)


async def end_now(user_id: ObjectId, reason: str, by: Optional[str] = None, note: Optional[str] = None) -> None:
    """Account deleted / admin revoke: access ends immediately. No-op without a subscription."""
    if await subscription_repo.find_by_user(user_id) is None:
        return

    def compute(current: Optional[dict], now: datetime) -> dict:
        expires = _utc((current or {}).get("expires_at"))
        return {"expires_at": min(expires, now) if expires else now, "ended_reason": reason}

    await _write(user_id, compute, {"action": "end", "reason": reason, "by": by, "note": note})
    logger.warning("Subscription ended: user=%s reason=%s", user_id, reason)
