"""
mock.py — In-memory stand-in for Razorpay (tests and local runs without keys).

Signatures use the same HMAC as Razorpay with fixed secrets, so tests can build
valid (and tampered) signatures with sign_payment() / sign_webhook().
"""

import json
import secrets
from typing import Any, Optional

from app.providers.payments.base import PaymentProvider, PaymentProviderError

MOCK_KEY_ID = "rzp_test_mock"
MOCK_KEY_SECRET = "mock_key_secret"
MOCK_WEBHOOK_SECRET = "mock_webhook_secret"


class MockPaymentProvider(PaymentProvider):
    name = "mock"

    def __init__(self) -> None:
        super().__init__()
        self.orders: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, dict[str, Any]] = {}
        #: Set to make the next call raise, e.g. to test "provider unreachable".
        self.fail_next: Optional[str] = None

    @property
    def key_id(self) -> str:
        return MOCK_KEY_ID

    def _key_secret(self) -> str:
        return MOCK_KEY_SECRET

    def _webhook_secret(self) -> Optional[str]:
        return MOCK_WEBHOOK_SECRET

    def _maybe_fail(self) -> None:
        if self.fail_next:
            message, self.fail_next = self.fail_next, None
            raise PaymentProviderError(message)

    async def create_order(self, amount: int, currency: str, receipt: str, notes: dict[str, str]) -> dict[str, Any]:
        self._maybe_fail()
        order = {
            "id": f"order_mock{secrets.token_hex(6)}",
            "amount": amount, "currency": currency, "receipt": receipt[:40],
            "notes": notes, "status": "created",
        }
        self.orders[order["id"]] = order
        return order

    async def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        self._maybe_fail()
        payment = self.payments.get(payment_id)
        if payment is None:
            raise PaymentProviderError("The id provided does not exist", "BAD_REQUEST_ERROR")
        return payment

    async def fetch_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        self._maybe_fail()
        return [p for p in self.payments.values() if p["order_id"] == order_id]

    # ── Test helpers ─────────────────────────────────────────────────────────

    def pay(self, order_id: str, status: str = "captured") -> tuple[str, str]:
        """Simulate the user paying an order. Returns (payment_id, checkout signature)."""
        order = self.orders[order_id]
        payment_id = f"pay_mock{secrets.token_hex(6)}"
        self.payments[payment_id] = {
            "id": payment_id, "order_id": order_id, "status": status,
            "amount": order["amount"], "currency": order["currency"],
        }
        return payment_id, self.sign_payment(order_id, payment_id)

    def sign_payment(self, order_id: str, payment_id: str) -> str:
        return self._hmac_hex(MOCK_KEY_SECRET, f"{order_id}|{payment_id}".encode())

    def sign_webhook(self, body: dict[str, Any]) -> tuple[bytes, str]:
        raw = json.dumps(body).encode()
        return raw, self._hmac_hex(MOCK_WEBHOOK_SECRET, raw)
