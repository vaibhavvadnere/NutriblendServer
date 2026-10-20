"""
providers/payments — Payment gateway. Chosen by PAYMENT_PROVIDER:

    PAYMENT_PROVIDER=mock       # default — in-memory fake, no network
    PAYMENT_PROVIDER=razorpay   # needs RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET

Services only call get_payment_provider(); nothing else imports a provider.
"""

from app.core.config import settings
from app.providers.payments.base import PaymentProvider, PaymentProviderError

_cached: PaymentProvider | None = None


def get_payment_provider() -> PaymentProvider:
    """The configured provider (built once, then reused)."""
    global _cached
    if _cached is None:
        if settings.PAYMENT_PROVIDER == "razorpay":
            from app.providers.payments.razorpay import RazorpayProvider

            _cached = RazorpayProvider()
        else:
            from app.providers.payments.mock import MockPaymentProvider

            _cached = MockPaymentProvider()
    return _cached


def reset_payment_provider() -> None:
    """Drop the cached provider — used by tests that switch PAYMENT_PROVIDER."""
    global _cached
    _cached = None


def current_provider_name() -> str:
    return settings.PAYMENT_PROVIDER


__all__ = ["PaymentProvider", "PaymentProviderError", "get_payment_provider", "reset_payment_provider", "current_provider_name"]
