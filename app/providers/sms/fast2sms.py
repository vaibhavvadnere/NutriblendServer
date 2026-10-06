"""
fast2sms.py — Fast2SMS (https://docs.fast2sms.com).

Setup:
  1. Create a Fast2SMS account and copy your API key (Dev API section).
  2. Set FAST2SMS_API_KEY in .env and SMS_PROVIDER=fast2sms.
  3. Route:
       - FAST2SMS_ROUTE=otp  -> Fast2SMS's OTP route; sends a fixed
         "Your OTP: {otp}" style message. No DLT template needed. Default.
       - FAST2SMS_ROUTE=dlt  -> your own DLT-approved template; also set
         FAST2SMS_SENDER_ID and FAST2SMS_MESSAGE_ID (the template id).
"""

import logging

import httpx

from app.core.config import settings
from app.providers.sms.base import SMSDeliveryError, SMSProvider

logger = logging.getLogger("nutriblend.sms")

API_URL = "https://www.fast2sms.com/dev/bulkV2"


class Fast2SMSProvider(SMSProvider):
    name = "fast2sms"

    def validate_config(self) -> None:
        self._require(FAST2SMS_API_KEY=settings.FAST2SMS_API_KEY)
        if settings.FAST2SMS_ROUTE.lower() == "dlt":
            self._require(
                FAST2SMS_SENDER_ID=settings.FAST2SMS_SENDER_ID,
                FAST2SMS_MESSAGE_ID=settings.FAST2SMS_MESSAGE_ID,
            )

    async def send_otp(self, mobile_number: str, otp: str) -> None:
        # Fast2SMS wants a bare 10-digit number, no country code.
        number = mobile_number.strip().lstrip("+")
        cc = settings.SMS_COUNTRY_CODE.lstrip("+")
        if number.startswith(cc) and len(number) > 10:
            number = number[len(cc):]

        route = settings.FAST2SMS_ROUTE.lower()
        if route == "dlt":
            payload = {
                "route": "dlt",
                "sender_id": settings.FAST2SMS_SENDER_ID,
                "message": settings.FAST2SMS_MESSAGE_ID,
                "variables_values": otp,
                "numbers": number,
                "flash": "0",
            }
        else:
            payload = {"route": "otp", "variables_values": otp, "numbers": number}

        headers = {"authorization": settings.FAST2SMS_API_KEY}

        try:
            async with httpx.AsyncClient(timeout=settings.SMS_TIMEOUT_SECONDS) as client:
                response = await client.post(API_URL, data=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise SMSDeliveryError(f"Fast2SMS request failed: {exc}") from exc

        body = _safe_json(response)
        if response.status_code >= 400 or body.get("return") is False:
            raise SMSDeliveryError(
                f"Fast2SMS rejected the request (HTTP {response.status_code}): "
                f"{body.get('message') or response.text}"
            )
        logger.info("Fast2SMS accepted OTP for %s (request_id=%s)", number, body.get("request_id"))


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"message": data}
    except ValueError:
        return {}
