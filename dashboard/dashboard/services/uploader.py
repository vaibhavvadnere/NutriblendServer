"""
services/uploader.py — Resumable, chunked video upload.

    start_and_upload()  new video: POST details -> send every chunk -> complete
    resume()            existing "uploading" video: send only the missing chunks -> complete

The source is any seekable binary file object (a file opened from this Mac, or
the browser upload). Only one chunk (8 MB by default) is in memory at a time
when reading from disk. Each chunk is retried with back-off on network errors
and server hiccups; client errors (wrong size, not an MP4, ...) stop at once.

Integrity:
  * Before starting, the whole file's SHA-256 is computed ("Checking file…").
    The server uses it to spot a file that was already uploaded (DuplicateFound)
    and, on resume, we use it to make sure it's the exact same file.
  * Every chunk carries its MD5; a chunk damaged on the way is rejected and sent again.
  * At the end the server re-checks what storage holds; any chunk that doesn't
    match is sent again automatically (up to VERIFY_ROUNDS times).
"""

from __future__ import annotations

import hashlib
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
VERIFY_ROUNDS = 3
HASH_BLOCK = 8 * 1024 * 1024

#: on_progress(bytes_sent_so_far, total_bytes, chunks_done, total_chunks)
ProgressCallback = Callable[[int, int, int, int], None]
#: on_check(bytes_checked, total_bytes) while the file's fingerprint is computed
CheckCallback = Callable[[int, int], None]


class UploadError(Exception):
    """Upload stopped; `video_id` (if any) can be resumed from the Videos page."""

    def __init__(self, message: str, video_id: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.video_id = video_id


class DuplicateFound(UploadError):
    """This exact file is already a video. `existing` is the server's details:
    video_id, title, status, created_at, uploaded_fraction. `sha256` is kept so
    "Upload anyway" doesn't need to read the file again."""

    def __init__(self, message: str, existing: dict, sha256: str):
        super().__init__(message, existing.get("video_id"))
        self.existing = existing
        self.sha256 = sha256


@dataclass
class Source:
    name: str
    size: int
    fh: BinaryIO
    path: Optional[str] = None   # set for files on this Mac (lets the fingerprint be cached)

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
    return Source(name=os.path.basename(full), size=size, fh=fh, path=full)


# (path, size, mtime_ns) -> sha256. Re-checking a 2 GB file takes a while; do it once.
_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def file_sha256(source: Source, on_check: Optional[CheckCallback] = None) -> str:
    """SHA-256 of the whole file, reading one block at a time (never all in memory)."""
    cache_key = None
    if source.path:
        try:
            st_ = os.stat(source.path)
            cache_key = (source.path, st_.st_size, st_.st_mtime_ns)
        except OSError:
            cache_key = None
        if cache_key in _HASH_CACHE:
            if on_check:
                on_check(source.size, source.size)
            return _HASH_CACHE[cache_key]
    digest = hashlib.sha256()
    done = 0
    source.fh.seek(0)
    while True:
        block = source.fh.read(HASH_BLOCK)
        if not block:
            break
        digest.update(block)
        done += len(block)
        if on_check:
            on_check(done, source.size)
    source.fh.seek(0)
    if done != source.size:
        raise UploadError(f"Could only read {done:,} of {source.size:,} bytes — is the file still being copied?")
    result = digest.hexdigest()
    if cache_key:
        _HASH_CACHE[cache_key] = result
    return result


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
            # A chunk damaged on the way (MD5 didn't match) is worth sending again.
            if exc.code != "UPLOAD_CHECKSUM_MISMATCH" and exc.status_code is not None and exc.status_code < 500:
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
    for round_ in range(VERIFY_ROUNDS):
        # The server lists at most 1000 missing chunks at a time, so work in batches.
        while not progress.complete and progress.missing_chunks:
            for index in progress.missing_chunks:
                result = _send_chunk(client, video.id, index, source.read_chunk(index, chunk_size))
                report(result.received_chunks)
            progress = videos_api.upload_progress(client, video.id)

        try:
            return videos_api.complete_upload(client, video.id)
        except ApiError as exc:
            if exc.code == "UPLOAD_INCOMPLETE" and round_ < VERIFY_ROUNDS - 1:
                # The final check found chunks that don't match (or are missing): send those again.
                logger.warning("Video %s: server asked again for chunks %s (%s)", video.id,
                               exc.details.get("missing_chunks"), exc.details.get("reason", "missing"))
                progress = videos_api.upload_progress(client, video.id)
                report(progress.received_chunks)
                continue
            if exc.code == "UPLOAD_CHECKSUM_MISMATCH":
                raise UploadError(
                    "The file's fingerprint didn't match after upload — it was probably changed while "
                    "uploading. Make sure the file is final and upload it again.", video.id) from exc
            raise UploadError(exc.message, video.id) from exc
    raise UploadError("The server kept finding damaged chunks. Check the connection and try again.", video.id)


def start_and_upload(
    client: ApiClient,
    source: Source,
    *,
    title: str,
    description: Optional[str],
    category: Optional[str],
    sort_order: int,
    on_progress: Optional[ProgressCallback] = None,
    on_check: Optional[CheckCallback] = None,
    sha256: Optional[str] = None,
    allow_duplicate: bool = False,
) -> Video:
    """Raises DuplicateFound if this exact file is already a video (unless allow_duplicate)."""
    sha256 = sha256 or file_sha256(source, on_check)
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
            sha256=sha256,
            allow_duplicate=allow_duplicate,
        )
    except ApiError as exc:
        if exc.code == "DUPLICATE_VIDEO":
            raise DuplicateFound(exc.message, exc.details, sha256) from exc
        raise UploadError(exc.message) from exc
    logger.info("Uploading %s (%d bytes) as video %s", source.name, source.size, video.id)
    return _send_missing(client, video, source, on_progress)


def resume(
    client: ApiClient,
    video: Video,
    source: Source,
    on_progress: Optional[ProgressCallback] = None,
    on_check: Optional[CheckCallback] = None,
) -> Video:
    if source.size != video.file_size:
        raise UploadError(
            f"That file is {source.size:,} bytes but this upload expects {video.file_size:,} bytes. "
            "Choose the same file you started with.",
            video.id,
        )
    if video.sha256 and file_sha256(source, on_check) != video.sha256:
        raise UploadError(
            "That file is the same size but not the same content as the one this upload started with. "
            "Choose the original file.",
            video.id,
        )
    return _send_missing(client, video, source, on_progress)
