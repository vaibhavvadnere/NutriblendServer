"""
utils/formatting.py — Small display helpers.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from dashboard.config import settings

_DIGITS = re.compile(r"\D")


def clean_mobile(value: str) -> str:
    """Keep digits only, and drop a leading 91 / 0 from a 12 / 11-digit number.
    The server does the real validation; this just tidies what was typed."""
    digits = _DIGITS.sub("", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def mask_mobile(mobile: str) -> str:
    """'9876543210' -> '98XXXXXX10'."""
    digits = clean_mobile(mobile)
    if len(digits) < 4:
        return "**********"
    return f"{digits[:2]}{'X' * (len(digits) - 4)}{digits[-2:]}"


def fmt_datetime(value: Optional[datetime], with_time: bool = True) -> str:
    """Server datetimes are UTC; show them in DISPLAY_TIMEZONE."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo(settings.DISPLAY_TIMEZONE))
    return local.strftime("%d %b %Y, %I:%M %p" if with_time else "%d %b %Y")


def fmt_bytes(size: Optional[int]) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def fmt_duration(seconds: Optional[float]) -> str:
    """125.4 -> '2:05', 3725 -> '1:02:05'."""
    if seconds is None:
        return "—"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
