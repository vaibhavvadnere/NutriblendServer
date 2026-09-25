"""
base.py — The contract every SMS provider implements.
"""

from abc import ABC, abstractmethod

from app.config import settings


class SMSDeliveryError(Exception):
    """Raised when an OTP could not be handed to the SMS gateway."""


class SMSProvider(ABC):
    #: Human-readable name, used in logs and /api/health.
    name: str = "base"

    #: Set False on providers that never touch the network (e.g. the mock).
    requires_network: bool = True

    def __init__(self) -> None:
        self.validate_config()

    def validate_config(self) -> None:
        """
        Raise SMSDeliveryError if required credentials are missing.

        Called at construction time so a misconfigured provider fails loudly
        on first use rather than silently dropping a user's OTP.
        """

    @abstractmethod
    async def send_otp(self, mobile_number: str, otp: str) -> None:
        """Deliver `otp` to `mobile_number` (a bare 10-digit Indian number)."""

    # ── Helpers shared by all providers ──────────────────────────────────────

    @staticmethod
    def to_e164(mobile_number: str) -> str:
        """'9876543210' -> '+919876543210'. Already-prefixed numbers pass through."""
        number = mobile_number.strip().replace(" ", "").replace("-", "")
        if number.startswith("+"):
            return number
        cc = settings.SMS_COUNTRY_CODE.lstrip("+")
        if number.startswith(cc) and len(number) > 10:
            return f"+{number}"
        return f"+{cc}{number}"

    @staticmethod
    def with_country_code(mobile_number: str) -> str:
        """'9876543210' -> '919876543210' (no plus — what MSG91/Fast2SMS want)."""
        return SMSProvider.to_e164(mobile_number).lstrip("+")

    @staticmethod
    def render_message(otp: str) -> str:
        return settings.SMS_TEMPLATE.format(
            otp=otp,
            minutes=settings.OTP_EXPIRE_MINUTES,
            app=settings.APP_NAME,
        )

    def _require(self, **fields: object) -> None:
        missing = [key for key, value in fields.items() if not value]
        if missing:
            raise SMSDeliveryError(
                f"SMS_PROVIDER is '{self.name}' but these settings are missing "
                f"from your .env: {', '.join(missing)}."
            )
