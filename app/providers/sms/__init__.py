"""
providers/sms — OTP delivery. One small module per SMS gateway, all behind the
same interface.

The rest of the app only ever calls send_otp_sms(). Which gateway actually
sends the message is decided by SMS_PROVIDER in .env:

    SMS_PROVIDER=mock       # default — logs the OTP, sends nothing
    SMS_PROVIDER=msg91
    SMS_PROVIDER=twilio
    SMS_PROVIDER=fast2sms

Add a new gateway by writing a class that subclasses SMSProvider (see base.py)
and registering it in _REGISTRY below. Nothing outside this package needs
to change.
"""

import logging

from app.core.config import settings
from app.providers.sms.base import SMSDeliveryError, SMSProvider
from app.providers.sms.fast2sms import Fast2SMSProvider
from app.providers.sms.mock import MockProvider
from app.providers.sms.msg91 import MSG91Provider
from app.providers.sms.twilio import TwilioProvider

_REGISTRY: dict[str, type[SMSProvider]] = {
    "mock": MockProvider,
    "msg91": MSG91Provider,
    "twilio": TwilioProvider,
    "fast2sms": Fast2SMSProvider,
}

logger = logging.getLogger("nutriblend.sms")

_cached: SMSProvider | None = None


def get_provider() -> SMSProvider:
    """Return the configured provider (built once, then reused)."""
    global _cached
    if _cached is None:
        name = settings.SMS_PROVIDER.lower()
        provider_cls = _REGISTRY.get(name)
        if provider_cls is None:
            raise SMSDeliveryError(
                f"Unknown SMS_PROVIDER '{settings.SMS_PROVIDER}'. "
                f"Valid options: {', '.join(sorted(_REGISTRY))}."
            )
        _cached = provider_cls()
    return _cached


def reset_provider_cache() -> None:
    """Drop the cached provider — used by tests that switch SMS_PROVIDER."""
    global _cached
    _cached = None


async def send_otp_sms(mobile_number: str, otp: str) -> None:
    """
    Deliver an OTP. Raises SMSDeliveryError if the gateway is misconfigured
    or refuses the message, so the caller can report a clean failure instead of
    telling the user "OTP sent" when nothing was sent.
    """
    provider = get_provider()
    try:
        await provider.send_otp(mobile_number, otp)
    except SMSDeliveryError:
        raise
    except Exception as exc:  # defensive: never leak a provider's internals
        logger.exception("Unexpected error in SMS provider '%s'", provider.name)
        raise SMSDeliveryError(f"SMS provider '{provider.name}' failed: {exc}") from exc


def current_provider_name() -> str:
    """Name of the active provider — handy for /api/health."""
    try:
        return get_provider().name
    except SMSDeliveryError:
        return "misconfigured"


__all__ = [
    "SMSProvider",
    "SMSDeliveryError",
    "get_provider",
    "reset_provider_cache",
    "send_otp_sms",
    "current_provider_name",
]
