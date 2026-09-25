"""
config.py — Central app configuration, loaded from environment variables (.env).
"""

from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── App ──────────────────────────────────────────────────────────────────
    APP_NAME: str = "Nutriblend API"
    ENV: str = "development"  # "development" or "production"

    # ── MongoDB ──────────────────────────────────────────────────────────────
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_NAME: str = "nutriblend"
    # How long the driver waits to find a reachable server before erroring.
    # Keep this low so a misconfigured URI fails fast instead of hanging.
    MONGO_SERVER_SELECTION_TIMEOUT_MS: int = 8000

    # ── JWT ──────────────────────────────────────────────────────────────────
    JWT_SECRET: str = "change-this-secret-in-.env"
    JWT_ALGORITHM: str = "HS256"
    # Access tokens are now short-lived; clients use the refresh token to get
    # a new one. 30 minutes is a sane default for a mobile app.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # Refresh tokens are opaque random strings stored (hashed) in MongoDB.
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── OTP ──────────────────────────────────────────────────────────────────
    OTP_LENGTH: int = 6
    OTP_EXPIRE_MINUTES: int = 5
    OTP_MAX_ATTEMPTS: int = 5

    # ── API surface ──────────────────────────────────────────────────────────
    # Every endpoint lives under this prefix. Bumping to /api/v2 later lets old
    # app versions keep working against v1 while new ones move over.
    API_PREFIX: str = "/api/v1"

    # ── Accounts ─────────────────────────────────────────────────────────────
    # An abandoned signup (created, never verified) is deleted after this long,
    # so no mobile number is locked up by someone who never finished.
    PENDING_USER_TTL_HOURS: int = 24
    # Enforce one account per email address when an email is supplied.
    ENFORCE_UNIQUE_EMAIL: bool = True

    # ── Rate limiting (OTP abuse protection) ─────────────────────────────────
    RATE_LIMIT_ENABLED: bool = True
    # Minimum gap between two OTPs for the same number. Drives "Resend in 47s".
    OTP_COOLDOWN_SECONDS: int = 60
    OTP_MAX_PER_MOBILE_PER_HOUR: int = 5
    OTP_MAX_PER_MOBILE_PER_DAY: int = 10
    OTP_MAX_PER_IP_PER_HOUR: int = 20
    VERIFY_MAX_PER_IP_PER_HOUR: int = 100
    # Trust X-Forwarded-For for the client IP. Turn ON only when the app sits
    # behind a proxy you control — otherwise callers can spoof their own IP and
    # walk straight around the per-IP limits.
    TRUST_PROXY_HEADERS: bool = False

    # ── SMS delivery ─────────────────────────────────────────────────────────
    # Which provider actually sends the OTP. "mock" just logs it (dev default).
    SMS_PROVIDER: Literal["mock", "msg91", "twilio", "fast2sms"] = "mock"
    # Country code prepended to the 10-digit number when a provider needs E.164.
    SMS_COUNTRY_CODE: str = "91"
    # Message body used by providers that send free-form text ({otp} is replaced).
    SMS_TEMPLATE: str = "{otp} is your Nutriblend verification code. It is valid for {minutes} minutes."
    # Seconds to wait on the provider's HTTP API before giving up.
    SMS_TIMEOUT_SECONDS: float = 10.0

    # MSG91 (https://msg91.com) — OTP API
    MSG91_AUTH_KEY: Optional[str] = None
    MSG91_TEMPLATE_ID: Optional[str] = None
    MSG91_SENDER_ID: Optional[str] = None

    # Twilio (https://twilio.com) — Programmable Messaging
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_FROM_NUMBER: Optional[str] = None  # E.164, e.g. +15551234567

    # Fast2SMS (https://fast2sms.com)
    FAST2SMS_API_KEY: Optional[str] = None
    FAST2SMS_ROUTE: str = "otp"  # "otp" (DLT-free OTP route) or "dlt"
    FAST2SMS_SENDER_ID: Optional[str] = None
    FAST2SMS_MESSAGE_ID: Optional[str] = None  # required when FAST2SMS_ROUTE="dlt"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() == "production"


settings = Settings()
