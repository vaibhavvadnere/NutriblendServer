"""
utils/phone.py — One place that decides what a valid mobile number is.

Users type their number every way imaginable:

    9876543210        +919876543210      +91 98765 43210
    09876543210       91-9876543210      (+91) 9876-543210

All of those are the same person. Normalising centrally — before validation,
before storage, before any database lookup — is what stops "this number is
already registered" bugs where two rows exist for one human.

Everything is stored as a bare 10-digit national number ("9876543210").
The country code is added back only when an SMS gateway needs it.
"""

import re

from app.core.config import settings

# Indian mobile numbers are 10 digits starting 6-9.
MOBILE_REGEX = re.compile(r"^[6-9]\d{9}$")

_NON_DIGITS = re.compile(r"[^\d+]")


class InvalidMobileNumber(ValueError):
    """Raised when a string cannot be read as an Indian mobile number."""


def normalize_mobile(value: str) -> str:
    """
    Turn any reasonable spelling of a number into a bare 10-digit string.

    Raises InvalidMobileNumber if what's left isn't a valid Indian mobile number.
    """
    if value is None:
        raise InvalidMobileNumber("mobile_number is required")

    # Strip everything that isn't a digit or a leading plus.
    cleaned = _NON_DIGITS.sub("", str(value).strip())
    cleaned = cleaned.lstrip("+")

    cc = settings.SMS_COUNTRY_CODE.lstrip("+")

    # Drop a country code prefix ("919876543210" -> "9876543210"), but only when
    # what remains is the right length — so we never mangle a number that simply
    # happens to start with those digits.
    if len(cleaned) == len(cc) + 10 and cleaned.startswith(cc):
        cleaned = cleaned[len(cc):]

    # Drop the STD trunk prefix ("09876543210" -> "9876543210").
    if len(cleaned) == 11 and cleaned.startswith("0"):
        cleaned = cleaned[1:]

    if not MOBILE_REGEX.match(cleaned):
        raise InvalidMobileNumber(
            "Enter a valid 10-digit Indian mobile number "
            "(with or without +91)."
        )
    return cleaned


def to_e164(mobile_number: str) -> str:
    """'9876543210' -> '+919876543210' — the format most SMS gateways want."""
    cc = settings.SMS_COUNTRY_CODE.lstrip("+")
    return f"+{cc}{normalize_mobile(mobile_number)}"


def mask(mobile_number: str) -> str:
    """'9876543210' -> '98XXXXXX10'. For logs and user-facing confirmations."""
    try:
        n = normalize_mobile(mobile_number)
    except InvalidMobileNumber:
        return "**********"
    return f"{n[:2]}XXXXXX{n[-2:]}"
