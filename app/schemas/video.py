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
    sha256: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the whole file (lowercase hex). Enables duplicate detection, "
                    "same-file checks on resume and the final integrity check.",
    )
    allow_duplicate: bool = Field(
        default=False, description="Upload even if a video with the same SHA-256 already exists.",
    )
    allow_duplicate_title: bool = Field(
        default=False, description="Create it even if another video already has this title.",
    )
    duration_seconds: Optional[float] = Field(
        default=None, gt=0, le=86_400,
        description="Length read from the file by the admin's browser. Used until the server can read it "
                    "itself (ffprobe), which then takes over.",
    )
    video_codec: Optional[str] = Field(
        default=None, max_length=20, pattern=r"^[a-z0-9._-]+$",
        description='Codec name as ffprobe spells it, e.g. "h264" or "hevc".',
    )

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
    allow_duplicate_title: bool = Field(default=False, description="Rename even if another video has this title.")

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


class VideoReplaceRequest(_Strict):
    """POST /admin/videos/{id}/replace-file — the new file that should take the place of the video's file."""
    file_name: str = Field(min_length=1, max_length=255)
    file_size: int = Field(gt=0, description="Exact size of the new file in bytes.")
    content_type: str = Field(description='Must be "video/mp4".')
    sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    allow_duplicate: bool = Field(default=False, description="Upload even if this exact file already exists in a video.")
    duration_seconds: Optional[float] = Field(default=None, gt=0, le=86_400)
    video_codec: Optional[str] = Field(default=None, max_length=20, pattern=r"^[a-z0-9._-]+$")


class VideoStatusRequest(_Strict):
    status: Literal["draft", "published"]
    when_ready: bool = Field(
        default=False,
        description="With status \"published\" while the video's document is still being prepared: keep the "
                    "video a draft and publish it automatically the moment the document is ready "
                    "(instead of answering 409 DOCUMENT_NOT_READY).",
    )


class UploadProgress(BaseModel):
    chunk_size: int
    total_chunks: int
    received_chunks: int
    #: Chunk numbers (0-based) still to send. Capped at 1000 entries.
    missing_chunks: list[int]
    complete: bool
    expires_at: datetime


class UploadTicket(BaseModel):
    """Lets the dashboard's in-browser upload box work on ONE video: send it as
    the `X-Upload-Ticket` header to the upload endpoints of that video."""
    video_id: str
    ticket: str
    expires_at: datetime


class PartChecksum(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    #: base64 MD5 of the chunk (the value of a Content-MD5 header)
    md5: str = Field(pattern=r"^[A-Za-z0-9+/]{22}==$")


class PartUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parts: list[PartChecksum] = Field(min_length=1, max_length=64)


class PartUrl(BaseModel):
    index: int
    #: PUT the chunk here with `headers`. None = send it through the server
    #: instead (PUT /admin/videos/{id}/upload/chunks/{index}).
    url: Optional[str] = None
    headers: dict[str, str] = {}


class PartUrls(BaseModel):
    parts: list[PartUrl]
    #: Links stop working at this time; ask again for any chunk not sent by then.
    expires_at: Optional[datetime] = None


class DocumentUploadRequest(BaseModel):
    """Start sending a document straight to storage (from the browser)."""
    model_config = ConfigDict(extra="forbid")
    file_name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    #: SHA-256 of the file (hex): checked before the document is prepared.
    sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DocumentUploadLink(BaseModel):
    upload_id: str
    #: PUT the whole file here with `headers`, then call …/document/upload/{upload_id}/complete.
    #: None = storage can't take it directly: PUT /admin/videos/{id}/document instead.
    url: Optional[str] = None
    headers: dict[str, str] = {}
    expires_at: Optional[datetime] = None


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


class ReplacementInfo(BaseModel):
    """A new file being uploaded to replace this video's file (the old file keeps playing meanwhile)."""
    #: The unfinished upload (an `uploading` video record): GET it for progress, resume it with its tickets.
    video_id: str
    file_name: Optional[str] = None
    file_size: int
    started_at: datetime
    #: Once the new file is uploaded it is optimized before it is swapped in: "queued" | "running" | "failed".
    optimization: Optional[str] = None


class OptimizationInfo(BaseModel):
    """How the video's file is being (or was) turned into a smaller one. The original never reaches the media storage."""
    state: Literal["queued", "running", "done", "failed"]
    #: 0..1 while running.
    progress: float = 0.0
    source_size: Optional[int] = Field(default=None, description="Size of the file that was uploaded, in bytes.")
    output_size: Optional[int] = Field(default=None, description="Size of the stored file, in bytes (when done).")
    saved_percent: Optional[int] = Field(default=None, description="How much smaller the stored file is than the upload.")
    mode: Optional[Literal["compressed", "converted", "kept"]] = Field(
        default=None,
        description="compressed = re-encoded smaller; converted = re-encoded to H.264 for compatibility; "
                    "kept = the upload was already efficient and is stored as it is.",
    )
    reason: Optional[str] = None
    error: Optional[str] = Field(default=None, description="Why it failed (state failed). The upload is kept; retry it.")
    queued_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class OptimizationStatus(BaseModel):
    enabled: bool = Field(description="VIDEO_OPTIMIZE_ENABLED.")
    available: bool = Field(description="ffmpeg and ffprobe are installed on the server.")
    active: bool = Field(description="New uploads are optimized (enabled and available).")
    crf: int
    preset: str
    skip_below_kbps: int


class AdminVideoOut(VideoOut):
    """What the dashboard sees: everything, including drafts and uploads in progress."""
    status: VideoStatus
    original_file_name: Optional[str] = None
    video_codec: Optional[str] = None
    #: SHA-256 of the file as declared by the uploader (if it sent one).
    sha256: Optional[str] = None
    #: True once every byte was verified: each chunk against its MD5 (checked by
    #: the server and again by the storage), plus the whole-file SHA-256 where
    #: the storage can read it back (local).
    integrity_verified: bool = False
    has_thumbnail: bool = False
    #: Published and actually shown in the app. A published video whose document isn't ready
    #: (still preparing, or failed) is hidden from the app until it is.
    visible_in_app: bool = False
    #: A draft that goes live by itself as soon as its document is ready.
    publish_when_ready: bool = False
    #: Set while a new file is being uploaded to replace this video's file.
    replacement: Optional[ReplacementInfo] = None
    #: Present for videos that go through optimization (queued / running / done / failed).
    optimization: Optional[OptimizationInfo] = None
    #: On the unfinished upload of a replacement: the id of the video it will replace.
    replaces: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    uploaded_at: Optional[datetime] = None
    created_by: Optional[str] = None
    #: Present while status is "uploading".
    upload: Optional[UploadProgress] = None
    #: Attached document, in any state (processing / ready / failed).
    admin_document: Optional[AdminDocumentInfo] = None
    #: Files the record points to that are NOT in the current storage
    #: ("video", "thumbnail", "document"). Only filled by GET /admin/videos/{id}
    #: and the storage check; an empty list elsewhere means "not checked".
    missing_files: list[Literal["video", "thumbnail", "document"]] = []


class BrokenVideo(BaseModel):
    id: str
    title: str
    status: VideoStatus
    created_at: datetime
    missing_files: list[Literal["video", "thumbnail", "document"]]


class StorageCheck(BaseModel):
    """GET /admin/videos/storage-check — records whose files are missing."""
    storage: str
    checked: int
    broken: list[BrokenVideo]
    #: Records that could not be checked (storage unreachable).
    errors: int = 0


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


class TitleMatch(BaseModel):
    id: str
    title: str
    status: str
    created_at: datetime


class CategoryCheck(BaseModel):
    exists: bool = False
    canonical: Optional[str] = Field(default=None, description="Spelling already in use (differs only by capitals/spaces).")
    similar: list[str] = Field(default_factory=list, description="Existing categories that look like a typo of it.")


class DetailsCheck(BaseModel):
    same_title: list[TitleMatch] = Field(default_factory=list)
    category: CategoryCheck = Field(default_factory=CategoryCheck)
