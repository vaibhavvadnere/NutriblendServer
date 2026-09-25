"""
sms_providers — One small module per SMS gateway, all behind the same interface.

Add a new gateway by writing a class that subclasses SMSProvider (see base.py)
and registering it in get_provider() below. Nothing outside this package needs
to change.
"""

from app.config import settings
from app.utils.sms_providers.base import SMSDeliveryError, SMSProvider
from app.utils.sms_providers.fast2sms import Fast2SMSProvider
from app.utils.sms_providers.mock import MockProvider
from app.utils.sms_providers.msg91 import MSG91Provider
from app.utils.sms_providers.twilio import TwilioProvider

_REGISTRY: dict[str, type[SMSProvider]] = {
    "mock": MockProvider,
    "msg91": MSG91Provider,
    "twilio": TwilioProvider,
    "fast2sms": Fast2SMSProvider,
}

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


__all__ = [
    "SMSProvider",
    "SMSDeliveryError",
    "get_provider",
    "reset_provider_cache",
]
