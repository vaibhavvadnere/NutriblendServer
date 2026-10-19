"""
mock.py — Default provider. Sends nothing; logs the message so you can test
the whole flow locally with no account, no DNS and no deliverability worries.
"""

import logging

from app.providers.email.base import EmailProvider

logger = logging.getLogger("nutriblend.email")


class MockEmailProvider(EmailProvider):
    name = "mock"
    requires_network = False

    async def send(self, to: str, subject: str, text: str, html: str) -> None:
        logger.info("[DEV EMAIL] to=%s subject=%s", to, subject)
        print(f"[DEV EMAIL] to {to} | {subject}\n{text}\n")
