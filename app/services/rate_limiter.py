"""
services/rate_limiter.py — Abuse protection for the OTP endpoints.

Why this exists
---------------
`signup`, `signin` and `resend-otp` all send an SMS. Without limits, anyone can
call them in a loop: you pay for every message, and somebody's phone buzzes all
night. These limits are the difference between a demo and something you can
point a real app at.

How it works
------------
Fixed-window counters in MongoDB, one document per (scope, identifier, window).
Mongo rather than in-process memory because the counters must be shared across
every worker process and survive restarts — an in-memory limiter silently stops
working the moment you run more than one uvicorn worker. Mongo rather than Redis
because it needs no extra service; move to Redis if request volume ever makes
the extra round-trip matter.

Every counter carries a TTL, so the collection cleans itself up.

Design notes
------------
* A blocked attempt still counts against the window. Hammering the endpoint
  therefore extends your own lockout rather than probing around it.
* Limits are consumed *before* the work is attempted, so a request that fails
  for another reason (unknown number, existing account) still costs quota. That
  is deliberate: it is what makes number enumeration expensive.
* Every rejection carries Retry-After, so the app can render "Resend in 47s"
  instead of letting the user mash a button.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings
from app.core.exceptions import RateLimited
from app.repositories import rate_limit_repo

logger = logging.getLogger("nutriblend.ratelimit")


@dataclass(frozen=True)
class Rule:
    """One limit: `limit` events per `window_seconds` for a given key."""
    scope: str
    identifier: str
    limit: int
    window_seconds: int
    #: Shown to the user when this particular rule trips.
    message: str = "Too many requests. Please try again later."


async def _consume(rule: Rule, now: datetime) -> None:
    """Increment one counter and raise if it has gone over the limit."""
    window_start_epoch = math.floor(now.timestamp() / rule.window_seconds) * rule.window_seconds
    window_end = datetime.fromtimestamp(window_start_epoch + rule.window_seconds, tz=timezone.utc)
    doc_id = f"{rule.scope}:{rule.identifier}:{window_start_epoch}"

    count = await rate_limit_repo.increment(doc_id, rule.scope, rule.identifier, window_end)
    if count > rule.limit:
        retry_after = max(1, int((window_end - now).total_seconds()))
        logger.warning(
            "Rate limit hit: scope=%s identifier=%s count=%d limit=%d",
            rule.scope, rule.identifier, count, rule.limit,
        )
        raise RateLimited(retry_after, rule.message, rule.scope)


async def enforce(*rules: Rule) -> None:
    """Apply every rule. Raises RateLimited on the first one that trips."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    now = datetime.now(timezone.utc)
    for rule in rules:
        await _consume(rule, now)


# ── Ready-made rule sets ─────────────────────────────────────────────────────

def otp_send_rules(mobile_number: str, ip: str) -> tuple[Rule, ...]:
    """Limits for any endpoint that sends an OTP (signup / signin / resend)."""
    return (
        Rule(
            scope="otp_cooldown",
            identifier=mobile_number,
            limit=1,
            window_seconds=settings.OTP_COOLDOWN_SECONDS,
            message="Please wait before requesting another OTP.",
        ),
        Rule(
            scope="otp_mobile_hour",
            identifier=mobile_number,
            limit=settings.OTP_MAX_PER_MOBILE_PER_HOUR,
            window_seconds=3600,
            message="Too many OTP requests for this number. Please try again later.",
        ),
        Rule(
            scope="otp_mobile_day",
            identifier=mobile_number,
            limit=settings.OTP_MAX_PER_MOBILE_PER_DAY,
            window_seconds=86400,
            message="Daily OTP limit reached for this number. Please try again tomorrow.",
        ),
        Rule(
            scope="otp_ip_hour",
            identifier=ip,
            limit=settings.OTP_MAX_PER_IP_PER_HOUR,
            window_seconds=3600,
            message="Too many OTP requests from this device. Please try again later.",
        ),
    )


def otp_verify_rules(ip: str) -> tuple[Rule, ...]:
    """Limits for verify-otp — stops brute-forcing codes across many accounts.

    Per-account guessing is already capped by OTP_MAX_ATTEMPTS; this catches the
    attacker who spreads guesses thinly across thousands of numbers instead."""
    return (
        Rule(
            scope="verify_ip_hour",
            identifier=ip,
            limit=settings.VERIFY_MAX_PER_IP_PER_HOUR,
            window_seconds=3600,
            message="Too many verification attempts. Please try again later.",
        ),
    )


async def reset_for(scope: str, identifier: str) -> None:
    """Clear a counter early — used after a successful login so a legitimate
    user isn't held to the cooldown from their own sign-in attempt."""
    await rate_limit_repo.delete_for(scope, identifier)
