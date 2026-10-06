"""
schemas/video.py — Request/response models for videos.

Lifecycle:  uploading ──complete──▶ draft ◀──▶ published
            (chunks arriving)        (admin only)  (visible in the app)
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VideoStatus = Literal["uploading", "draft", "published"]

ALLOWED_VIDEO_TYPES = {"video/mp4": (".mp4", ".m4v")}
ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _clean_text(v: Optional[str], field: str, max_len: int, required: bool) -> Optional[str]:
    if v is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    v = " ".join(str(v).split())
    if not v:
        if required:
            raise ValueError(f"{field} cannot be empty")
        return None
    if len(v) > max_len:
        raise ValueError(f"{field} cannot be longer than {max_len} characters")
    return v


def _clean_description(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = str(v).strip()
    if len(v) > 5000:
        raise ValueError("description cannot be longer than 5000 characters")
    return v or None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VideoCreateRequest(_Strict):
    """POST /admin/videos — the video's details plus the file you are about to upload."""
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    sort_order: int = Field(default=0, ge=0, le=100_000, description="Lower numbers are shown first in the app.")
    file_name: str = Field(min_length=1, max_length=255, description="Original file name, e.g. lesson-1.mp4")
    file_size: int = Field(gt=0, description="Exact size of the file in bytes.")
    content_type: str = Field(description='Must be "video/mp4".')

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return _clean_text(v, "title", 120, required=True)

    @field_validator("category")
    @classmethod
    def _category(cls, v):
        return _clean_text(v, "category", 40, required=False)

    @field_validator("description")
    @classmethod
    def _description(cls, v):
        return _clean_description(v)


class VideoUpdateRequest(_Strict):
    """PATCH /admin/videos/{id} — only the fields you send change. Send
    `null` for description or category to clear it."""
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=100_000)

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return None if v is None else _clean_text(v, "title", 120, required=True)

    @field_validator("category")
    @classmethod
    def _category(cls, v):
        return _clean_text(v, "category", 40, required=False)

    @field_validator("description")
    @classmethod
    def _description(cls, v):
        return _clean_description(v)


class VideoStatusRequest(_Strict):
    status: Literal["draft", "published"]


class UploadProgress(BaseModel):
    chunk_size: int
    total_chunks: int
    received_chunks: int
    #: Chunk numbers (0-based) still to send. Capped at 1000 entries.
    missing_chunks: list[int]
    complete: bool
    expires_at: datetime


DocumentStatus = Literal["processing", "ready", "failed"]


class DocumentInfo(BaseModel):
    """The document attached to a video. View-only: there is never a link to
    the original file — the app shows rendered page images instead."""
    name: str
    file_type: Literal["pdf", "docx", "pptx"]
    size: int
    page_count: Optional[int] = None


class AdminDocumentInfo(DocumentInfo):
    #: processing = converting Word/PowerPoint to pages; failed -> see `error`.
    status: DocumentStatus
    error: Optional[str] = None
    uploaded_at: datetime


class DocumentPages(BaseModel):
    """Signed, expiring links to page images (JPEG), in order."""
    name: str
    page_count: int
    pages: list[str]
    links_expire_at: datetime


class VideoOut(BaseModel):
    """What the mobile app sees (published videos only)."""
    id: str
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    sort_order: int = 0
    duration_seconds: Optional[float] = None
    file_size: int
    content_type: str
    #: Signed, expiring links. Fetch the video again to get fresh ones.
    playback_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    links_expire_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    #: Present when the video has a document that is ready to view
    #: (GET /videos/{id}/document for its pages).
    document: Optional[DocumentInfo] = None


class AdminVideoOut(VideoOut):
    """What the dashboard sees: everything, including drafts and uploads in progress."""
    status: VideoStatus
    original_file_name: Optional[str] = None
    video_codec: Optional[str] = None
    has_thumbnail: bool = False
    created_at: datetime
    updated_at: datetime
    uploaded_at: Optional[datetime] = None
    created_by: Optional[str] = None
    #: Present while status is "uploading".
    upload: Optional[UploadProgress] = None
    #: Attached document, in any state (processing / ready / failed).
    admin_document: Optional[AdminDocumentInfo] = None


class VideoPage(BaseModel):
    items: list[VideoOut]
    total: int
    page: int
    page_size: int
    pages: int


class AdminVideoPage(BaseModel):
    items: list[AdminVideoOut]
    total: int
    page: int
    page_size: int
    pages: int


class CategoryList(BaseModel):
    categories: list[str]
