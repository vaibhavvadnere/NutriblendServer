"""
services/uploader.py — Resumable, chunked video upload.

    start_and_upload()  new video: POST details -> send every chunk -> complete
    resume()            existing "uploading" video: send only the missing chunks -> complete

The source is any seekable binary file object (a file opened from this Mac, or
the browser upload). Only one chunk (8 MB by default) is in memory at a time
when reading from disk. Each chunk is retried with back-off on network errors
and server hiccups; client errors (wrong size, not an MP4, ...) stop at once.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Optional

from dashboard.api import videos as videos_api
from dashboard.api.client import ApiClient
from dashboard.api.errors import ApiError, NetworkError
from dashboard.models import UploadProgress, Video

logger = logging.getLogger("dashboard.uploader")

VIDEO_EXTENSIONS = (".mp4", ".m4v")
DOCUMENT_EXTENSIONS = (".pdf", ".docx", ".pptx")
MAX_ATTEMPTS = 4

#: on_progress(bytes_sent_so_far, total_bytes, chunks_done, total_chunks)
ProgressCallback = Callable[[int, int, int, int], None]


class UploadError(Exception):
    """Upload stopped; `video_id` (if any) can be resumed from the Videos page."""

    def __init__(self, message: str, video_id: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.video_id = video_id


@dataclass
class Source:
    name: str
    size: int
    fh: BinaryIO

    def read_chunk(self, index: int, chunk_size: int) -> bytes:
        self.fh.seek(index * chunk_size)
        return self.fh.read(chunk_size)


MAC_PERMISSION_HELP = (
    "macOS is blocking the dashboard from reading this folder. Open System Settings → "
    "Privacy & Security → Files and Folders (or Full Disk Access), allow the app you started "
    "the dashboard from (e.g. Terminal, iTerm or VS Code), then restart the dashboard. "
    "Or move the video to a folder such as ~/Movies."
)


@dataclass
class LocalVideo:
    path: str
    name: str
    size: int
    modified: float


def clean_path(raw: str) -> str:
    """Tidy a pasted path: quotes, file:// URLs, %20, '\\ ' escapes, ~, Unicode forms."""
    from urllib.parse import unquote, urlparse

    text = raw.strip().strip('"').strip("'").strip()
    if text.startswith("file://"):
        text = unquote(urlparse(text).path)
    text = text.replace("\\ ", " ")  # shell-escaped spaces from Terminal drag & drop
    return os.path.abspath(os.path.expanduser(text))


def _resolve(full: str) -> str:
    """The same path, or a sibling whose name only differs in Unicode form /
    whitespace (common when a name is typed or copied)."""
    if os.path.exists(full):
        return full
    import unicodedata

    folder, name = os.path.split(full)
    wanted = " ".join(unicodedata.normalize("NFC", name).split()).lower()
    try:
        for entry in os.listdir(folder):
            if " ".join(unicodedata.normalize("NFC", entry).split()).lower() == wanted:
                return os.path.join(folder, entry)
    except PermissionError as exc:
        raise UploadError(MAC_PERMISSION_HELP) from exc
    except OSError:
        pass
    return full


def source_from_path(path: str, extensions: tuple[str, ...] = VIDEO_EXTENSIONS) -> Source:
    """Open a file on this Mac. Raises UploadError with a readable message."""
    full = _resolve(clean_path(path))
    if not full.lower().endswith(extensions):
        raise UploadError(f"Only {' / '.join(extensions)} files can be used here.")
    try:
        size = os.stat(full).st_size
        fh = open(full, "rb")
    except PermissionError as exc:
        raise UploadError(MAC_PERMISSION_HELP) from exc
    except FileNotFoundError as exc:
        raise UploadError(f"No file found at {full}. Check the name, or pick it from the folder list.") from exc
    except IsADirectoryError as exc:
        raise UploadError("That is a folder, not a video file.") from exc
    return Source(name=os.path.basename(full), size=size, fh=fh)


def videos_in_folder(folder: str, extensions: tuple[str, ...] = VIDEO_EXTENSIONS) -> list[LocalVideo]:
    """Matching files in a folder, newest first. Raises UploadError (not found / macOS permission)."""
    full = clean_path(folder)
    try:
        entries = list(os.scandir(full))
    except PermissionError as exc:
        raise UploadError(MAC_PERMISSION_HELP) from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise UploadError(f"Folder not found: {full}") from exc
    found = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.name.lower().endswith(extensions):
            continue
        try:
            if entry.is_file():
                st_ = entry.stat()
                found.append(LocalVideo(entry.path, entry.name, st_.st_size, st_.st_mtime))
        except OSError:
            continue
    return sorted(found, key=lambda v: v.modified, reverse=True)


def _sleep(attempt: int) -> None:
    time.sleep(min(2 ** attempt, 8))


def _send_chunk(client: ApiClient, video_id: str, index: int, data: bytes) -> UploadProgress:
    for attempt in range(MAX_ATTEMPTS):
        try:
            return videos_api.upload_chunk(client, video_id, index, data)
        except NetworkError as exc:
            reason = exc.message
        except ApiError as exc:
            if exc.status_code is not None and exc.status_code < 500:
                raise UploadError(exc.message, video_id) from exc
            reason = exc.message
        logger.warning("Chunk %d of %s failed (attempt %d): %s", index, video_id, attempt + 1, reason)
        if attempt < MAX_ATTEMPTS - 1:
            _sleep(attempt)
    raise UploadError(f"Chunk {index} could not be sent after {MAX_ATTEMPTS} attempts: {reason}", video_id)


def _send_missing(
    client: ApiClient, video: Video, source: Source, on_progress: Optional[ProgressCallback]
) -> Video:
    """Send every chunk the server doesn't have yet, then complete the upload."""
    progress = video.upload or videos_api.upload_progress(client, video.id)
    chunk_size, total_chunks = progress.chunk_size, progress.total_chunks

    def report(done: int) -> None:
        if on_progress:
            on_progress(min(done * chunk_size, source.size), source.size, done, total_chunks)

    report(progress.received_chunks)
    # The server lists at most 1000 missing chunks at a time, so work in batches.
    while not progress.complete and progress.missing_chunks:
        for index in progress.missing_chunks:
            result = _send_chunk(client, video.id, index, source.read_chunk(index, chunk_size))
            report(result.received_chunks)
        progress = videos_api.upload_progress(client, video.id)

    try:
        return videos_api.complete_upload(client, video.id)
    except ApiError as exc:
        raise UploadError(exc.message, video.id) from exc


def start_and_upload(
    client: ApiClient,
    source: Source,
    *,
    title: str,
    description: Optional[str],
    category: Optional[str],
    sort_order: int,
    on_progress: Optional[ProgressCallback] = None,
) -> Video:
    try:
        video = videos_api.start_upload(
            client,
            title=title,
            description=description,
            category=category,
            sort_order=sort_order,
            file_name=source.name,
            file_size=source.size,
            content_type="video/mp4",
        )
    except ApiError as exc:
        raise UploadError(exc.message) from exc
    logger.info("Uploading %s (%d bytes) as video %s", source.name, source.size, video.id)
    return _send_missing(client, video, source, on_progress)


def resume(client: ApiClient, video: Video, source: Source, on_progress: Optional[ProgressCallback] = None) -> Video:
    if source.size != video.file_size:
        raise UploadError(
            f"That file is {source.size:,} bytes but this upload expects {video.file_size:,} bytes. "
            "Choose the same file you started with.",
            video.id,
        )
    return _send_missing(client, video, source, on_progress)
