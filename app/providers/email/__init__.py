"""
providers/email — OTP delivery by email. One module per gateway, all behind the
same interface, exactly like providers/sms.

The rest of the app only calls send_otp_email(). Which gateway sends it is
decided by EMAIL_PROVIDER in .env:

    EMAIL_PROVIDER=mock     # default — logs the message, sends nothing
    EMAIL_PROVIDER=smtp     # any SMTP relay or mailbox
    EMAIL_PROVIDER=brevo    # Brevo transactional API

Email delivery is also gated by EMAIL_ENABLED, so the whole channel can be
switched off in one place without touching any code — which matters here,
because sending the OTP to both channels is a temporary arrangement while DLT
registration for SMS is pending.
"""

import logging

from app.core.config import settings
from app.providers.email.base import EmailDeliveryError, EmailProvider
from app.providers.email.brevo import BrevoEmailProvider
from app.providers.email.mock import MockEmailProvider
from app.providers.email.smtp import SMTPEmailProvider
from app.providers.email.templates import label_for, otp_html, otp_subject, otp_text

_REGISTRY: dict[str, type[EmailProvider]] = {
    "mock": MockEmailProvider,
    "smtp": SMTPEmailProvider,
    "brevo": BrevoEmailProvider,
}

logger = logging.getLogger("nutriblend.email")

_cached: EmailProvider | None = None


def get_provider() -> EmailProvider:
    global _cached
    if _cached is None:
        name = settings.EMAIL_PROVIDER.lower()
        provider_cls = _REGISTRY.get(name)
        if provider_cls is None:
            raise EmailDeliveryError(
                f"Unknown EMAIL_PROVIDER '{settings.EMAIL_PROVIDER}'. "
                f"Valid options: {', '.join(sorted(_REGISTRY))}."
            )
        _cached = provider_cls()
    return _cached


def reset_provider_cache() -> None:
    """Drop the cached provider — used by tests that switch EMAIL_PROVIDER."""
    global _cached
    _cached = None


def is_enabled() -> bool:
    return settings.EMAIL_ENABLED


async def send_otp_email(email: str, otp: str, purpose: str) -> None:
    """
    Deliver an OTP by email. Raises EmailDeliveryError on any failure, so the
    caller decides what a failed channel means.
    """
    provider = get_provider()
    label = label_for(purpose)
    try:
        await provider.send(
            to=email,
            subject=otp_subject(otp),
            text=otp_text(otp, label),
            html=otp_html(otp, label),
        )
    except EmailDeliveryError:
        raise
    except Exception as exc:  # defensive: never leak a provider's internals
        logger.exception("Unexpected error in email provider '%s'", provider.name)
        raise EmailDeliveryError(f"Email provider '{provider.name}' failed: {exc}") from exc


def current_provider_name() -> str:
    """Name of the active provider — handy for /api/health."""
    if not settings.EMAIL_ENABLED:
        return "disabled"
    try:
        return get_provider().name
    except EmailDeliveryError:
        return "misconfigured"


def mask_email(email: str) -> str:
    """'boss@example.com' -> 'b•••@example.com'. For responses and logs."""
    if not email or "@" not in email:
        return "•••"
    local, _, domain = email.partition("@")
    head = local[0] if local else "•"
    return f"{head}•••@{domain}"


__all__ = [
    "EmailProvider",
    "EmailDeliveryError",
    "get_provider",
    "reset_provider_cache",
    "send_otp_email",
    "current_provider_name",
    "is_enabled",
    "mask_email",
]
