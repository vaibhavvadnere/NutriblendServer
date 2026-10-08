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
    # "r2"    = Cloudflare R2 (production). Needs the R2_* settings below.
    STORAGE_PROVIDER: Literal["local", "r2"] = "local"
    # Folder for the local provider (created if missing). Keep it out of git.
    MEDIA_ROOT: str = "media"
    # Largest video accepted, in MB.
    MAX_VIDEO_SIZE_MB: int = 4096
    # Uploads are sent in chunks of this size (last one may be smaller). 8 MB
    # suits reverse proxies and matches S3/R2 multipart (min 5 MB per part).
    UPLOAD_CHUNK_SIZE_MB: int = 8
    # An upload that isn't finished within this time is discarded.
    UPLOAD_SESSION_TTL_HOURS: int = 24
    # Browser uploads: the dashboard sends pieces straight to the storage (R2)
    # with short-lived signed links, so video bytes never pass through this
    # server. Piece 0 always comes through the server (it checks the file is an
    # MP4). Has no effect with STORAGE_PROVIDER=local (always via the server).
    UPLOAD_DIRECT_TO_STORAGE: bool = True
    UPLOAD_PART_URL_TTL_SECONDS: int = 900
    # An upload ticket lets the admin's browser keep uploading ONE video for
    # this long, independent of the (short) dashboard session.
    UPLOAD_TICKET_TTL_HOURS: int = 12
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

    # ── Video optimization ───────────────────────────────────────────────────
    # Uploaded videos are first received on THIS server's disk (the staging
    # folder), re-encoded to a much smaller H.264 file with ffmpeg in the
    # background, and only that file is stored in the media storage (R2). The
    # original never reaches R2. Needs ffmpeg + ffprobe; without them (or with
    # this off) videos are uploaded straight to the storage as before.
    VIDEO_OPTIMIZE_ENABLED: bool = True
    # Where originals wait for optimization. Needs room for the largest video
    # plus the encoded copy; keep it on a disk with plenty of free space.
    VIDEO_STAGING_ROOT: str = "staging"
    # x264 quality (lower = better and bigger). 21 is visually the same as the
    # original for screen/explainer videos and typical camera footage.
    VIDEO_OPTIMIZE_CRF: int = 21
    # x264 speed/size trade-off (ultrafast … veryslow). "fast" suits a small server.
    VIDEO_OPTIMIZE_PRESET: str = "fast"
    VIDEO_OPTIMIZE_AUDIO_KBPS: int = 128
    # Encoder threads; keep low so the API stays responsive.
    VIDEO_OPTIMIZE_THREADS: int = 2
    # H.264 videos at or below this video bitrate are already efficient: they
    # are stored as they are (re-encoding would only lose quality).
    VIDEO_OPTIMIZE_SKIP_BELOW_KBPS: int = 4000
    # The encoded file must be at least this much smaller than the original,
    # otherwise the original is kept (only for files that were already H.264).
    VIDEO_OPTIMIZE_MIN_SAVING_PERCENT: int = 10
    # How many videos are encoded at the same time.
    VIDEO_OPTIMIZE_CONCURRENCY: int = 1
    # An encode may take at most (this × the video's length + 10 minutes).
    VIDEO_OPTIMIZE_TIMEOUT_FACTOR: int = 20
    # Seconds between looks at the queue when it is empty.
    VIDEO_OPTIMIZE_POLL_SECONDS: int = 5
    # Free space to keep on the staging disk on top of what is needed (MB).
    VIDEO_STAGING_MARGIN_MB: int = 500

    # ── Cloudflare R2 (STORAGE_PROVIDER=r2) ──────────────────────────────────
    # Account ID is in the R2 dashboard; the key pair comes from an R2 API token
    # with "Object Read & Write" on this bucket only. Keep the keys in .env.
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET: str = ""
    # Optional override (e.g. a jurisdiction endpoint, or a local test server).
    # Empty = https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com
    R2_ENDPOINT_URL: str = ""
    # Lifetime of the direct R2 link a /media request is redirected to. Players
    # reopen /media (whose own link lasts MEDIA_URL_TTL_SECONDS) when it expires.
    R2_PRESIGNED_TTL_SECONDS: int = 600
    # Local cache for PDFs downloaded from R2 to render document pages.
    MEDIA_CACHE_DIR: str = ""  # empty = <system temp>/nutriblend-cache
    MEDIA_CACHE_MAX_MB: int = 1024

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

    # ── HTTP ─────────────────────────────────────────────────────────────────
    # Browser origins allowed to call the API (JSON list). The Android app is
    # not a browser. DASHBOARD_ORIGINS are always added: the dashboard's upload
    # box runs in the admin's browser and calls the upload endpoints directly.
    CORS_ORIGINS: list[str] = ["*"]
    # Where the dashboard is opened from, e.g. ["https://admin.nutriblend.co.in"].
    # Also used by scripts/r2_cors.py to allow browser uploads into the bucket.
    DASHBOARD_ORIGINS: list[str] = ["http://localhost:8501", "http://127.0.0.1:8501"]

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
