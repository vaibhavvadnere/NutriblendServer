"""
config.py — Dashboard settings, loaded from dashboard/.env (same pattern as the server).
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# dashboard/.env, resolved from this file so `streamlit run` works from any folder.
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    APP_TITLE: str = "Nutriblend Admin"

    # ── API server ───────────────────────────────────────────────────────────
    SERVER_URL: str = "http://localhost:8000"
    API_PREFIX: str = "/api/v1"
    REQUEST_TIMEOUT_SECONDS: float = 15.0

    # Refresh the access token this many seconds before it expires, so a
    # request never goes out with a token that dies in flight.
    TOKEN_REFRESH_LEEWAY_SECONDS: int = 60

    # ── Display ──────────────────────────────────────────────────────────────
    DISPLAY_TIMEZONE: str = "Asia/Kolkata"

    # ── Logging ──────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def server_url(self) -> str:
        return self.SERVER_URL.rstrip("/")

    def api_path(self, path: str) -> str:
        """'/auth/signin' -> '/api/v1/auth/signin'."""
        return f"/{self.API_PREFIX.strip('/')}/{path.lstrip('/')}"


settings = Settings()
