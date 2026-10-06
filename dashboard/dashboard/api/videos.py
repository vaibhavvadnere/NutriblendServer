"""
api/videos.py — Video endpoints (/api/v1/admin/videos). Admin session only.
"""

from __future__ import annotations

from typing import Any, Optional

from dashboard.api.client import ApiClient
from dashboard.config import settings
from dashboard.models import DocumentPages, UploadProgress, Video, VideoPage

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


def get_video(client: ApiClient, video_id: str) -> Video:
    return Video.model_validate(client.get(_path(f"/{video_id}")))


def start_upload(client: ApiClient, **fields: Any) -> Video:
    """fields: title, description, category, sort_order, file_name, file_size, content_type."""
    return Video.model_validate(client.post(_path(), json=fields))


def upload_chunk(client: ApiClient, video_id: str, index: int, data: bytes) -> UploadProgress:
    raw = client.put(
        _path(f"/{video_id}/upload/chunks/{index}"),
        content=data,
        headers={"Content-Type": "application/octet-stream"},
        timeout=CHUNK_TIMEOUT_SECONDS,
    )
    return UploadProgress.model_validate(raw)


def upload_progress(client: ApiClient, video_id: str) -> UploadProgress:
    return UploadProgress.model_validate(client.get(_path(f"/{video_id}/upload")))


def complete_upload(client: ApiClient, video_id: str) -> Video:
    return Video.model_validate(client.post(_path(f"/{video_id}/upload/complete"), timeout=CHUNK_TIMEOUT_SECONDS))


def update_video(client: ApiClient, video_id: str, **changes: Any) -> Video:
    return Video.model_validate(client.patch(_path(f"/{video_id}"), json=changes))


def set_status(client: ApiClient, video_id: str, status: str) -> Video:
    return Video.model_validate(client.patch(_path(f"/{video_id}/status"), json={"status": status}))


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
