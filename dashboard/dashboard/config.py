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
    # The API address as the ADMIN'S BROWSER reaches it (the upload box talks
    # to the API directly). Empty = SERVER_URL. Set it when the dashboard
    # reaches the API by an internal address, e.g. https://api.nutriblend.co.in
    PUBLIC_SERVER_URL: str = ""
    # Largest video the upload box accepts (should match the server's MAX_VIDEO_SIZE_MB).
    MAX_VIDEO_SIZE_MB: int = 4096
    API_PREFIX: str = "/api/v1"
    REQUEST_TIMEOUT_SECONDS: float = 15.0

    # Stay signed in across page refreshes. The browser keeps only a random key in sessionStorage (so it is
    # forgotten when the tab is closed); the tokens stay inside this dashboard process. The key stops working
    # after this many hours without use, and when the dashboard restarts. 0 = off (every refresh signs out).
    SESSION_REMEMBER_HOURS: float = 12

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

    @property
    def browser_api_base(self) -> str:
        """e.g. http://localhost:8000/api/v1 — used by code running in the browser."""
        return f"{(self.PUBLIC_SERVER_URL or self.SERVER_URL).rstrip('/')}/{self.API_PREFIX.strip('/')}"

    def api_path(self, path: str) -> str:
        """'/auth/signin' -> '/api/v1/auth/signin'."""
        return f"/{self.API_PREFIX.strip('/')}/{path.lstrip('/')}"


settings = Settings()
