"""
utils/sms.py — OTP delivery (public interface).

The rest of the app only ever calls send_otp_sms(). Which gateway actually
sends the message is decided by SMS_PROVIDER in .env:

    SMS_PROVIDER=mock       # default — logs the OTP, sends nothing
    SMS_PROVIDER=msg91
    SMS_PROVIDER=twilio
    SMS_PROVIDER=fast2sms

Each provider lives in app/utils/sms_providers/. Adding another one means
writing a class there and registering it — no changes here or in the routers.
"""

import logging

from app.utils.sms_providers import SMSDeliveryError, get_provider

logger = logging.getLogger("nutriblend.sms")

__all__ = ["send_otp_sms", "SMSDeliveryError", "current_provider_name"]


async def send_otp_sms(mobile_number: str, otp: str) -> None:
    """
    Deliver an OTP. Raises SMSDeliveryError if the gateway is misconfigured
    or refuses the message, so the caller can return a clean 502 instead of
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
