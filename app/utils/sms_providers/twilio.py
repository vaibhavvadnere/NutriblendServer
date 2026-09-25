"""
twilio.py — Twilio Programmable Messaging (https://www.twilio.com/docs/sms).

Setup:
  1. Get your Account SID and Auth Token from the Twilio Console.
  2. Buy/verify a sending number (E.164, e.g. +15551234567).
  3. Put TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER in .env
     and set SMS_PROVIDER=twilio.

Note for India: delivering to Indian numbers still requires DLT registration of
your sender/template with the Indian telecom regulator.

This calls the REST API directly over httpx, so no twilio SDK dependency.
"""

import logging

import httpx

from app.config import settings
from app.utils.sms_providers.base import SMSDeliveryError, SMSProvider

logger = logging.getLogger("nutriblend.sms")


class TwilioProvider(SMSProvider):
    name = "twilio"

    def validate_config(self) -> None:
        self._require(
            TWILIO_ACCOUNT_SID=settings.TWILIO_ACCOUNT_SID,
            TWILIO_AUTH_TOKEN=settings.TWILIO_AUTH_TOKEN,
            TWILIO_FROM_NUMBER=settings.TWILIO_FROM_NUMBER,
        )

    async def send_otp(self, mobile_number: str, otp: str) -> None:
        url = (
            "https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.TWILIO_ACCOUNT_SID}/Messages.json"
        )
        data = {
            "To": self.to_e164(mobile_number),
            "From": settings.TWILIO_FROM_NUMBER,
            "Body": self.render_message(otp),
        }

        try:
            async with httpx.AsyncClient(timeout=settings.SMS_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    url,
                    data=data,
                    auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                )
        except httpx.HTTPError as exc:
            raise SMSDeliveryError(f"Twilio request failed: {exc}") from exc

        if response.status_code >= 400:
            body = _safe_json(response)
            raise SMSDeliveryError(
                f"Twilio rejected the request (HTTP {response.status_code}): "
                f"{body.get('message') or response.text}"
            )
        logger.info("Twilio accepted OTP for %s (sid=%s)", mobile_number, _safe_json(response).get("sid"))


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"message": data}
    except ValueError:
        return {}
