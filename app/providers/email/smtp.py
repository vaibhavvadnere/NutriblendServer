"""
smtp.py — Plain SMTP. Works with any mailbox or relay you already have
(Brevo's SMTP relay, Zoho, Google Workspace, your host's mail server).

Setup:
    EMAIL_PROVIDER=smtp
    SMTP_HOST=smtp-relay.brevo.com
    SMTP_PORT=587
    SMTP_USERNAME=...
    SMTP_PASSWORD=...
    SMTP_USE_TLS=true          # STARTTLS on 587 (use SMTP_USE_SSL for port 465)
    EMAIL_FROM=noreply@nutriblend.co.in
    EMAIL_FROM_NAME=Nutriblend

smtplib is blocking, so the send runs in a worker thread — otherwise one slow
mail server would stall every other request on the event loop.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage

import anyio

from app.core.config import settings
from app.providers.email.base import EmailDeliveryError, EmailProvider

logger = logging.getLogger("nutriblend.email")


class SMTPEmailProvider(EmailProvider):
    name = "smtp"

    def validate_config(self) -> None:
        self._require(
            SMTP_HOST=settings.SMTP_HOST,
            SMTP_USERNAME=settings.SMTP_USERNAME,
            SMTP_PASSWORD=settings.SMTP_PASSWORD,
            EMAIL_FROM=settings.EMAIL_FROM,
        )

    async def send(self, to: str, subject: str, text: str, html: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{self.from_name()} <{self.from_address()}>"
        message["To"] = to
        # Nothing useful can come back to a no-reply address; say so explicitly
        # so well-behaved clients hide the Reply button.
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        try:
            await anyio.to_thread.run_sync(self._send_blocking, message)
        except EmailDeliveryError:
            raise
        except Exception as exc:
            raise EmailDeliveryError(f"SMTP send failed: {exc}") from exc

    def _send_blocking(self, message: EmailMessage) -> None:
        timeout = settings.EMAIL_TIMEOUT_SECONDS
        if settings.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT,
                timeout=timeout, context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout)
        try:
            if settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL:
                server.starttls(context=ssl.create_default_context())
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(message)
        finally:
            try:
                server.quit()
            except Exception:
                pass
