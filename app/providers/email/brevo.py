"""
brevo.py — Brevo (ex-Sendinblue) transactional email API.

Setup:
  1. Create a free Brevo account (300 emails/day, no card).
  2. Senders, Domains & Dedicated IPs -> add nutriblend.co.in, then add the
     DNS records it gives you (verification TXT, DKIM, SPF) at your registrar.
  3. Wait for the domain to show as verified.
  4. BREVO_API_KEY=... , EMAIL_PROVIDER=brevo,
     EMAIL_FROM=noreply@nutriblend.co.in

The API is used rather than SMTP because it returns a message id and a clear
error body, which makes delivery problems much easier to diagnose.
"""

import logging

import httpx

from app.core.config import settings
from app.providers.email.base import EmailDeliveryError, EmailProvider

logger = logging.getLogger("nutriblend.email")

API_URL = "https://api.brevo.com/v3/smtp/email"


class BrevoEmailProvider(EmailProvider):
    name = "brevo"

    def validate_config(self) -> None:
        self._require(
            BREVO_API_KEY=settings.BREVO_API_KEY,
            EMAIL_FROM=settings.EMAIL_FROM,
        )

    async def send(self, to: str, subject: str, text: str, html: str) -> None:
        payload = {
            "sender": {"email": self.from_address(), "name": self.from_name()},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": text,
            "htmlContent": html,
        }
        headers = {"api-key": settings.BREVO_API_KEY, "accept": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=settings.EMAIL_TIMEOUT_SECONDS) as client:
                response = await client.post(API_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise EmailDeliveryError(f"Brevo request failed: {exc}") from exc

        if response.status_code >= 400:
            body = _safe_json(response)
            raise EmailDeliveryError(
                f"Brevo rejected the message (HTTP {response.status_code}): "
                f"{body.get('message') or response.text}"
            )
        logger.info("Brevo accepted mail (messageId=%s)", _safe_json(response).get("messageId"))


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"message": data}
    except ValueError:
        return {}
