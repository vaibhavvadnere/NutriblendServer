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

    # ── Admin (dashboard) ────────────────────────────────────────────────────
    # An account is an admin when its `role` is "admin". Admin accounts can only
    # be created by POST /admin/admins (never by signup or any other endpoint).
    #
    # Optional extra lock: comma-separated mobile numbers. When set, an account
    # needs role "admin" AND a listed number to use /admin endpoints. Empty =
    # the role alone decides.
    ADMIN_MOBILE_NUMBERS: str = ""
    # POST /admin/admins is OPEN (no key, no login) while this is true — meant
    # for creating admins from Swagger during development. Set it to false
    # before the server is reachable from the internet.
    ADMIN_CREATE_ENABLED: bool = True
    # Admin sessions (tokens from /admin/auth/signin -> verify-otp) are shorter-lived.
    ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    # Timezone used to decide what "today" means in admin statistics.
    ADMIN_TIMEZONE: str = "Asia/Kolkata"

    # ── Media (videos & thumbnails) ──────────────────────────────────────────
    # Where file bytes live. "local" = a folder on this server (development).
    # Cloud providers (Cloudflare R2 / AWS S3) plug in behind the same interface.
    STORAGE_PROVIDER: Literal["local"] = "local"
    # Folder for the local provider (created if missing). Keep it out of git.
    MEDIA_ROOT: str = "media"
    # Largest video accepted, in MB.
    MAX_VIDEO_SIZE_MB: int = 4096
    # Uploads are sent in chunks of this size (last one may be smaller). 8 MB
    # suits reverse proxies and matches S3/R2 multipart (min 5 MB per part).
    UPLOAD_CHUNK_SIZE_MB: int = 8
    # An upload that isn't finished within this time is discarded.
    UPLOAD_SESSION_TTL_HOURS: int = 24
    # Largest thumbnail image accepted, in MB.
    MAX_THUMBNAIL_SIZE_MB: int = 5
    # Documents attached to videos (PDF / Word / PowerPoint). 0 = no size limit.
    MAX_DOCUMENT_SIZE_MB: int = 0
    # Width in pixels of the page images the app is shown (documents are
    # view-only: the original file is never sent to anyone).
    DOCUMENT_PAGE_WIDTH: int = 1400
    # LibreOffice converts Word/PowerPoint to PDF. Empty = auto-detect
    # (`soffice` on PATH, or /Applications/LibreOffice.app on macOS).
    LIBREOFFICE_PATH: str = ""
    DOCUMENT_CONVERT_TIMEOUT_SECONDS: int = 300

    # Playback / thumbnail links handed to clients expire after this long.
    MEDIA_URL_TTL_SECONDS: int = 3600
    # Secret that signs media links. Empty = derived from JWT_SECRET.
    MEDIA_URL_SECRET: str = ""
    # Public base URL used to build media links (e.g. https://api.nutriblend.in).
    # Empty = taken from the incoming request.
    PUBLIC_BASE_URL: str = ""

    # ── Legal (privacy policy / account deletion pages) ──────────────────────
    # Shown in the public privacy policy (/privacy-policy) and the account
    # deletion page (/account-deletion). Empty values are flagged in the
    # dashboard's Privacy policy checklist and must be filled before publishing.
    LEGAL_APP_NAME: str = "Nutriblend"
    LEGAL_COMPANY_NAME: str = ""
    LEGAL_COMPANY_ADDRESS: str = ""
    LEGAL_CONTACT_EMAIL: str = ""
    LEGAL_GRIEVANCE_OFFICER: str = ""
    LEGAL_GRIEVANCE_EMAIL: str = ""
    LEGAL_HOSTING_PROVIDER: str = ""
    # e.g. "30 September 2026"
    PRIVACY_POLICY_EFFECTIVE_DATE: str = ""
    LEGAL_DELETION_DAYS: int = 30
    LEGAL_LOG_RETENTION_DAYS: int = 90

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
