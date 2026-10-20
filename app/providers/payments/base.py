"""
base.py — The contract every payment provider implements.

Everything money-related that talks to the outside world goes through here, so
services never import httpx or know Razorpay's URL shapes. Signature checks are
plain HMAC-SHA256 and identical for the real provider and the mock.
"""

import hashlib
import hmac
from abc import ABC, abstractmethod
from typing import Any, Optional


class PaymentProviderError(Exception):
    """The provider could not be reached or refused the request."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class PaymentProvider(ABC):
    #: Human-readable name, used in logs and /api/health.
    name: str = "base"

    def __init__(self) -> None:
        self.validate_config()

    def validate_config(self) -> None:
        """Raise PaymentProviderError if required credentials are missing."""

    # ── Credentials ──────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def key_id(self) -> str:
        """Public key id — the only credential the app ever receives."""

    @abstractmethod
    def _key_secret(self) -> str: ...

    @abstractmethod
    def _webhook_secret(self) -> Optional[str]: ...

    # ── Calls to the provider ────────────────────────────────────────────────

    @abstractmethod
    async def create_order(self, amount: int, currency: str, receipt: str, notes: dict[str, str]) -> dict[str, Any]:
        """Create an order. Returns at least {"id", "amount", "currency", "status"}."""

    @abstractmethod
    async def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Fetch one payment. Returns at least {"id", "order_id", "status", "amount"}."""

    # ── Signatures (shared) ──────────────────────────────────────────────────

    @staticmethod
    def _hmac_hex(secret: str, message: bytes) -> str:
        return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Checkout's success signature: HMAC-SHA256(key_secret, "order_id|payment_id")."""
        if not (order_id and payment_id and signature):
            return False
        expected = self._hmac_hex(self._key_secret(), f"{order_id}|{payment_id}".encode())
        return hmac.compare_digest(expected, signature)

    def verify_webhook_signature(self, raw_body: bytes, signature: Optional[str]) -> bool:
        """X-Razorpay-Signature: HMAC-SHA256(webhook_secret, raw request body).
        False when no webhook secret is configured — never accept unsigned events."""
        secret = self._webhook_secret()
        if not (secret and signature):
            return False
        return hmac.compare_digest(self._hmac_hex(secret, raw_body), signature)
