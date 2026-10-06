"""
mock.py — Default provider. Sends nothing; logs the OTP so you can test locally.
"""

import logging

from app.providers.sms.base import SMSProvider

logger = logging.getLogger("nutriblend.sms")


class MockProvider(SMSProvider):
    name = "mock"
    requires_network = False

    async def send_otp(self, mobile_number: str, otp: str) -> None:
        logger.info("[DEV OTP] mobile=%s otp=%s", mobile_number, otp)
        print(f"[DEV OTP] OTP {otp} for {mobile_number} (mock provider — no real SMS sent)")
