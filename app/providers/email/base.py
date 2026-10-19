"""
base.py — The contract every email provider implements.

Deliberately the same shape as providers/sms/base.py, so the two channels can
be driven by one piece of code in auth_service.
"""

from abc import ABC, abstractmethod

from app.core.config import settings


class EmailDeliveryError(Exception):
    """Raised when a message could not be handed to the email gateway."""


class EmailProvider(ABC):
    #: Human-readable name, used in logs and /api/health.
    name: str = "base"

    #: Set False on providers that never touch the network (e.g. the mock).
    requires_network: bool = True

    def __init__(self) -> None:
        self.validate_config()

    def validate_config(self) -> None:
        """
        Raise EmailDeliveryError if required credentials are missing.

        Called at construction time so a misconfigured provider fails loudly on
        first use rather than silently dropping a user's OTP.
        """

    @abstractmethod
    async def send(self, to: str, subject: str, text: str, html: str) -> None:
        """Deliver one message. Both a plain-text and an HTML body are given;
        a provider should send them as alternatives so every client can read it."""

    # ── Helpers shared by all providers ──────────────────────────────────────

    def _require(self, **fields: object) -> None:
        missing = [key for key, value in fields.items() if not value]
        if missing:
            raise EmailDeliveryError(
                f"EMAIL_PROVIDER is '{self.name}' but these settings are missing "
                f"from your .env: {', '.join(missing)}."
            )

    @staticmethod
    def from_address() -> str:
        return settings.EMAIL_FROM

    @staticmethod
    def from_name() -> str:
        return settings.EMAIL_FROM_NAME
