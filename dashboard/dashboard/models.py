"""
models.py — Typed views of the API's `data` payloads.

These mirror the server's schemas (app/schemas/*.py) but are owned by the
dashboard. Unknown fields are ignored, so the server can add fields without
breaking the dashboard.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

UserStatus = Literal["pending", "active", "blocked"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class User(_Model):
    id: str
    mobile_number: str
    name: str
    email: Optional[str] = None
    state: Optional[str] = None
    status: UserStatus = "pending"
    is_verified: bool = False
    created_at: datetime
    verified_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None


class OtpSent(_Model):
    mobile_number: str  # masked by the server, e.g. 98XXXXXX10
    expires_in_minutes: int
    resend_available_in_seconds: int
    is_new_account: bool = False
    dev_otp: Optional[str] = None  # only present when the server is not in production


class TokenPair(_Model):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    scope: str = "app"  # "admin" for dashboard sessions


class LoginResult(TokenPair):
    user: User


class Health(_Model):
    status: str
    app: str
    env: str
    database: str
    sms_provider: str


# ── Admin endpoints (/api/v1/admin/...) ──────────────────────────────────────

class UsersByStatus(_Model):
    active: int = 0
    pending: int = 0
    blocked: int = 0


class DailyCount(_Model):
    date: date
    count: int


class AdminStats(_Model):
    total_users: int
    users_by_status: UsersByStatus
    signups_today: int
    signups_last_7_days: int
    active_last_7_days: int
    signups_by_day: list[DailyCount]
    timezone: str
    videos: dict[str, int] = {}


class UserPage(_Model):
    items: list[User]
    total: int
    page: int
    page_size: int
    pages: int


class UserDetail(User):
    role: str = "user"
    active_sessions: int = 0
    is_admin: bool = False


# ── Videos (/api/v1/admin/videos) ────────────────────────────────────────────

class UploadProgress(_Model):
    chunk_size: int
    total_chunks: int
    received_chunks: int
    missing_chunks: list[int] = []
    complete: bool = False
    expires_at: Optional[datetime] = None

    @property
    def fraction(self) -> float:
        return self.received_chunks / self.total_chunks if self.total_chunks else 0.0


class VideoDocument(_Model):
    name: str
    file_type: str            # pdf | docx | pptx
    size: int
    page_count: Optional[int] = None
    status: str = "processing"  # processing | ready | failed
    error: Optional[str] = None
    uploaded_at: Optional[datetime] = None


class DocumentPages(_Model):
    name: str
    page_count: int
    pages: list[str]
    links_expire_at: Optional[datetime] = None


class Video(_Model):
    id: str
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    sort_order: int = 0
    duration_seconds: Optional[float] = None
    file_size: int
    content_type: str = "video/mp4"
    playback_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    published_at: Optional[datetime] = None
    status: str = "draft"  # uploading | draft | published
    original_file_name: Optional[str] = None
    video_codec: Optional[str] = None
    has_thumbnail: bool = False
    created_at: datetime
    updated_at: Optional[datetime] = None
    uploaded_at: Optional[datetime] = None
    upload: Optional[UploadProgress] = None
    admin_document: Optional[VideoDocument] = None


class VideoPage(_Model):
    items: list[Video]
    total: int
    page: int
    page_size: int
    pages: int
