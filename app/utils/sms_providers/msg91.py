"""
msg91.py — MSG91 OTP API (https://docs.msg91.com).

Setup:
  1. Create an MSG91 account and get your Auth Key (Dashboard -> API).
  2. Register a DLT template for your OTP message and note its Template ID.
     The template must contain a ##OTP## variable.
  3. Put MSG91_AUTH_KEY, MSG91_TEMPLATE_ID and (optionally) MSG91_SENDER_ID in .env
     and set SMS_PROVIDER=msg91.

MSG91 generates/validates OTPs itself if you want, but we send our own OTP as a
template variable so the rest of the app (hashing, expiry, attempt limits)
stays in control.
"""

import logging

import httpx

from app.config import settings
from app.utils.sms_providers.base import SMSDeliveryError, SMSProvider

logger = logging.getLogger("nutriblend.sms")

API_URL = "https://control.msg91.com/api/v5/otp"


class MSG91Provider(SMSProvider):
    name = "msg91"

    def validate_config(self) -> None:
        self._require(
            MSG91_AUTH_KEY=settings.MSG91_AUTH_KEY,
            MSG91_TEMPLATE_ID=settings.MSG91_TEMPLATE_ID,
        )

    async def send_otp(self, mobile_number: str, otp: str) -> None:
        params = {
            "template_id": settings.MSG91_TEMPLATE_ID,
            "mobile": self.with_country_code(mobile_number),
            "authkey": settings.MSG91_AUTH_KEY,
            "otp": otp,
            "otp_expiry": str(settings.OTP_EXPIRE_MINUTES),
        }
        if settings.MSG91_SENDER_ID:
            params["sender"] = settings.MSG91_SENDER_ID

        try:
            async with httpx.AsyncClient(timeout=settings.SMS_TIMEOUT_SECONDS) as client:
                response = await client.post(API_URL, params=params)
        except httpx.HTTPError as exc:
            raise SMSDeliveryError(f"MSG91 request failed: {exc}") from exc

        body = _safe_json(response)
        # MSG91 answers 200 with {"type": "success"|"error", "message": "..."}
        if response.status_code >= 400 or str(body.get("type", "")).lower() == "error":
            raise SMSDeliveryError(
                f"MSG91 rejected the request (HTTP {response.status_code}): "
                f"{body.get('message') or response.text}"
            )
        logger.info("MSG91 accepted OTP for %s (request_id=%s)", mobile_number, body.get("request_id"))


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"message": data}
    except ValueError:
        return {}
