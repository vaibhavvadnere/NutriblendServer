"""
razorpay.py — Razorpay over its REST API (https://razorpay.com/docs/api/).

httpx is used directly instead of Razorpay's Python SDK because the SDK is
synchronous and would block the event loop on every call.
"""

import logging
from typing import Any, Optional

import httpx

from app.core.config import settings
from app.providers.payments.base import PaymentProvider, PaymentProviderError

logger = logging.getLogger("nutriblend.payments")


class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    def validate_config(self) -> None:
        missing = [k for k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET") if not getattr(settings, k)]
        if missing:
            raise PaymentProviderError(f"PAYMENT_PROVIDER=razorpay but {', '.join(missing)} is not set in .env.")
        if not settings.RAZORPAY_WEBHOOK_SECRET:
            logger.warning("RAZORPAY_WEBHOOK_SECRET is not set: webhooks will be rejected until it is.")

    @property
    def key_id(self) -> str:
        return settings.RAZORPAY_KEY_ID or ""

    def _key_secret(self) -> str:
        return settings.RAZORPAY_KEY_SECRET or ""

    def _webhook_secret(self) -> Optional[str]:
        return settings.RAZORPAY_WEBHOOK_SECRET

    async def _request(self, method: str, path: str, json: Optional[dict] = None) -> dict[str, Any]:
        url = f"{settings.RAZORPAY_API_BASE.rstrip('/')}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=settings.RAZORPAY_TIMEOUT_SECONDS) as client:
                response = await client.request(method, url, json=json, auth=(self.key_id, self._key_secret()))
        except httpx.HTTPError as exc:
            logger.error("Razorpay %s %s unreachable: %s", method, path, exc)
            raise PaymentProviderError("Couldn't reach the payment provider. Please try again.") from exc

        if response.status_code >= 400:
            # Razorpay errors: {"error": {"code": "BAD_REQUEST_ERROR", "description": "..."}}
            try:
                error = response.json().get("error") or {}
            except ValueError:
                error = {}
            logger.error("Razorpay %s %s -> %s %s", method, path, response.status_code, error)
            raise PaymentProviderError(
                error.get("description") or f"Payment provider error ({response.status_code}).",
                error.get("code"),
            )
        return response.json()

    async def create_order(self, amount: int, currency: str, receipt: str, notes: dict[str, str]) -> dict[str, Any]:
        # receipt: max 40 chars; notes: max 15 key-value pairs (Razorpay limits).
        return await self._request(
            "POST", "orders",
            {"amount": amount, "currency": currency, "receipt": receipt[:40], "notes": notes},
        )

    async def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._request("GET", f"payments/{payment_id}")
