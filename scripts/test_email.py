#!/usr/bin/env python3
"""
test_email.py — Send one OTP email with the configured provider.

    ./.venv/bin/python3 scripts/test_email.py you@example.com
    ./.venv/bin/python3 scripts/test_email.py you@example.com --provider smtp

Use this to prove the mail path works BEFORE switching EMAIL_PROVIDER in .env.
With OTP_REQUIRE_ALL_CHANNELS=true a broken mail setup makes every OTP request
fail, so it is worth finding out here rather than from a user.

--provider overrides .env for this run only, so you can test smtp while the
server keeps running on mock.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.providers import email as email_provider  # noqa: E402
from app.providers.email.base import EmailDeliveryError  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("to", help="address to send the test message to")
    parser.add_argument("--provider", help="override EMAIL_PROVIDER for this run")
    parser.add_argument("--otp", default="123456", help="code to put in the message")
    args = parser.parse_args()

    if args.provider:
        settings.EMAIL_PROVIDER = args.provider
        email_provider.reset_provider_cache()

    print(f"Provider : {settings.EMAIL_PROVIDER}")
    print(f"From     : {settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM}>")
    if settings.EMAIL_PROVIDER.lower() == "smtp":
        print(f"SMTP     : {settings.SMTP_HOST}:{settings.SMTP_PORT} "
              f"as {settings.SMTP_USERNAME} "
              f"(TLS={settings.SMTP_USE_TLS}, SSL={settings.SMTP_USE_SSL})")
        if settings.SMTP_USERNAME and settings.EMAIL_FROM != settings.SMTP_USERNAME:
            print("\n  ! EMAIL_FROM does not match SMTP_USERNAME.")
            print("    Gmail rewrites or rejects a From header that isn't the")
            print("    authenticated account. Make them the same.\n")
    print(f"To       : {args.to}")
    print("\nSending...", flush=True)

    try:
        await email_provider.send_otp_email(args.to, args.otp, "app")
    except EmailDeliveryError as exc:
        print(f"\nFAILED\n  {exc}\n")
        print("Common causes:")
        print("  * SMTP_PASSWORD empty, or the normal Google password instead of")
        print("    a 16-character App Password (spaces removed).")
        print("  * 2-Step Verification not enabled, so no App Password exists.")
        print("  * EMAIL_FROM different from SMTP_USERNAME.")
        print("  * Port/TLS mismatch — 587 with SMTP_USE_TLS=true, or")
        print("    465 with SMTP_USE_SSL=true.")
        return 1

    print("\nSent. Check the inbox — and the spam folder, which is where mail from")
    print("a plain Gmail account often lands until you move to a verified domain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
