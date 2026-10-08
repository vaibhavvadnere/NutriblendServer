"""
api/videos.py — Video endpoints (/api/v1/admin/videos). Admin session only.
"""

from __future__ import annotations

from typing import Any, Optional

from dashboard.api.client import ApiClient
from dashboard.config import settings
from dashboard.models import DocumentPages, StorageCheck, UploadProgress, Video, VideoPage

_BASE = "/admin/videos"
CHUNK_TIMEOUT_SECONDS = 120.0


def _path(suffix: str = "") -> str:
    return settings.api_path(f"{_BASE}{suffix}")


def list_videos(
    client: ApiClient,
    query: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> VideoPage:
    params: dict[str, Any] = {"page": page, "page_size": page_size}
    for key, value in (("q", query), ("status", status), ("category", category)):
        if value:
            params[key] = value
    return VideoPage.model_validate(client.get(_path(), params=params))


def categories(client: ApiClient) -> list[str]:
    return list(client.get(_path("/categories"))["categories"])


def check_details(client: ApiClient, title: str | None, category: str | None, exclude_id: str | None = None) -> dict:
    """{same_title: [{id, title, status, created_at}], category: {exists, canonical, similar}} — nothing is saved."""
    params = {k: v for k, v in {"title": title, "category": category, "exclude_id": exclude_id}.items() if v}
    return client.get(_path("/check-details"), params=params)


def optimization_status(client: ApiClient) -> dict:
    """{enabled, available, active, crf, preset, skip_below_kbps}: are new videos optimized on the server?"""
    return client.get(_path("/optimization"))


def retry_optimization(client: ApiClient, video_id: str) -> Video:
    """Queue a failed optimization again."""
    return Video.model_validate(client.post(_path(f"/{video_id}/optimization/retry")))


def get_video(client: ApiClient, video_id: str) -> Video:
    return Video.model_validate(client.get(_path(f"/{video_id}")))


def start_upload(client: ApiClient, **fields: Any) -> Video:
    """fields: title, description, category, sort_order, file_name, file_size, content_type,
    sha256 (whole-file fingerprint), allow_duplicate. 409 DUPLICATE_VIDEO if the file exists."""
    return Video.model_validate(client.post(_path(), json=fields))


def replace_file(client: ApiClient, video_id: str, **fields: Any) -> Video:
    """Start uploading a new file for this video. Returns the unfinished upload (an `uploading` video);
    completing it swaps the file in. fields: file_name, file_size, content_type, sha256, allow_duplicate,
    duration_seconds, video_codec. 409 DUPLICATE_VIDEO if this exact file exists."""
    return Video.model_validate(client.post(_path(f"/{video_id}/replace-file"), json=fields))


def content_md5(data: bytes) -> str:
    """Base64 MD5, the standard Content-MD5 header value."""
    import base64
    import hashlib

    return base64.b64encode(hashlib.md5(data).digest()).decode()


def upload_ticket(client: ApiClient, video_id: str) -> dict:
    """{video_id, ticket, expires_at}: lets the browser upload box work on this one video."""
    return client.post(_path(f"/{video_id}/upload/ticket"))


def document_ticket(client: ApiClient, video_id: str) -> dict:
    """{video_id, ticket, expires_at}: lets the browser upload box send this video's document."""
    return client.post(_path(f"/{video_id}/document/ticket"))


def upload_chunk(client: ApiClient, video_id: str, index: int, data: bytes) -> UploadProgress:
    """Send one chunk with its MD5: the server (and R2) reject it if any byte changed on the way."""
    raw = client.put(
        _path(f"/{video_id}/upload/chunks/{index}"),
        content=data,
        headers={"Content-Type": "application/octet-stream", "Content-MD5": content_md5(data)},
        timeout=CHUNK_TIMEOUT_SECONDS,
    )
    return UploadProgress.model_validate(raw)


def upload_progress(client: ApiClient, video_id: str) -> UploadProgress:
    return UploadProgress.model_validate(client.get(_path(f"/{video_id}/upload")))


def complete_upload(client: ApiClient, video_id: str) -> Video:
    return Video.model_validate(client.post(_path(f"/{video_id}/upload/complete"), timeout=CHUNK_TIMEOUT_SECONDS))


def update_video(client: ApiClient, video_id: str, **changes: Any) -> Video:
    return Video.model_validate(client.patch(_path(f"/{video_id}"), json=changes))


def set_status(client: ApiClient, video_id: str, status: str, when_ready: bool = False) -> Video:
    """draft / published. With when_ready, publishing a video whose document is still being prepared
    keeps it a draft that publishes itself the moment the document is ready (409 DOCUMENT_NOT_READY
    otherwise; a failed document always blocks)."""
    body = {"status": status, **({"when_ready": True} if when_ready else {})}
    return Video.model_validate(client.patch(_path(f"/{video_id}/status"), json=body))


def set_thumbnail(client: ApiClient, video_id: str, file_name: str, data: bytes, content_type: str) -> Video:
    raw = client.put(_path(f"/{video_id}/thumbnail"), files={"file": (file_name, data, content_type)})
    return Video.model_validate(raw)


def remove_thumbnail(client: ApiClient, video_id: str) -> Video:
    return Video.model_validate(client.delete(_path(f"/{video_id}/thumbnail")))


def delete_video(client: ApiClient, video_id: str) -> None:
    client.delete(_path(f"/{video_id}"))


def upload_document(client: ApiClient, video_id: str, file_name: str, fh, size: int) -> Video:
    """Stream a document (PDF/DOCX/PPTX) as the raw request body — never fully in memory."""
    from urllib.parse import quote

    def body():
        fh.seek(0)
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            yield block

    raw = client.put(
        _path(f"/{video_id}/document"),
        content=body(),
        headers={"Content-Type": "application/octet-stream", "X-File-Name": quote(file_name),
                 "Content-Length": str(size)},
        timeout=CHUNK_TIMEOUT_SECONDS,
    )
    return Video.model_validate(raw)


def remove_document(client: ApiClient, video_id: str) -> Video:
    return Video.model_validate(client.delete(_path(f"/{video_id}/document")))


def retry_document(client: ApiClient, video_id: str) -> Video:
    return Video.model_validate(client.post(_path(f"/{video_id}/document/retry")))


def document_pages(client: ApiClient, video_id: str) -> DocumentPages:
    return DocumentPages.model_validate(client.get(_path(f"/{video_id}/document/pages")))


def storage_check(client: ApiClient) -> StorageCheck:
    """Videos whose files are missing from the server's current storage."""
    return StorageCheck.model_validate(client.get(_path("/storage-check"), timeout=120))
