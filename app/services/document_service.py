"""
services/document_service.py — One view-only document (PDF / Word / PowerPoint) per video.

View-only by design:
  * The original file is stored privately and is NEVER served — not to the
    app, not to the dashboard. There is no endpoint that returns it.
  * Word and PowerPoint are converted to PDF with LibreOffice (in the
    background after upload). The PDF rendition is private too.
  * Viewers get *page images* (JPEG), rendered on first request and cached,
    through signed links that expire, sent with `Cache-Control: no-store`.
    (Nothing shown on a screen can be made capture-proof; this stops the
    document from being downloaded as a file through the app or the API.)

Document sub-document on the video:
    document: { id, key, name, file_type, content_type, size, uploaded_at, sha256?,
                status: processing|ready|failed, error?, pdf_key?, page_count? }

Two ways in:
  * Browser straight to storage (R2): start_direct() -> signed PUT link ->
    finish_direct() (size + file-type check) -> prepare() (SHA-256 check, convert).
    The pending upload lives in `document_upload` on the video until finished.
  * Through the server: upload() (local storage, or when direct is blocked).
"""

import asyncio
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Optional
from urllib.parse import unquote

import anyio
from bson import ObjectId

from app.core import media_links
from app.core.config import settings
from app.core.exceptions import (
    DocumentNotFound,
    DocumentNotReady,
    FileTooLarge,
    InsufficientStorage,
    InvalidMediaFile,
    UnsupportedMediaType,
    UploadChecksumMismatch,
    UploadIncomplete,
    UploadNotInProgress,
    VideoNotFound,
)
from app.providers.storage import get_storage
from app.repositories import video_repo
from app.schemas.video import AdminDocumentInfo, DocumentInfo, DocumentPages, DocumentUploadLink

logger = logging.getLogger("nutriblend.documents")

MB = 1024 * 1024
FILE_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_ZIP_MARKER = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml"}
_FREE_SPACE_MARGIN = 200 * MB
#: A single PUT to R2/S3 can be at most 5 GiB.
_MAX_SINGLE_PUT = 5 * 1024 * MB
_DIRECT_LINK_TTL = 3600   # the link must be valid when the PUT starts; a long PUT may then run on
_MAGIC = {"pdf": b"%PDF-", "docx": b"PK\x03\x04", "pptx": b"PK\x03\x04"}

STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


# ── Output shaping ───────────────────────────────────────────────────────────

def admin_info(video: dict) -> Optional[AdminDocumentInfo]:
    d = video.get("document")
    if not d:
        return None
    return AdminDocumentInfo(
        name=d["name"], file_type=d["file_type"], size=d["size"], page_count=d.get("page_count"),
        status=d["status"], error=d.get("error"), uploaded_at=d["uploaded_at"],
    )


def app_info(video: dict) -> Optional[DocumentInfo]:
    d = video.get("document")
    if not d or d.get("status") != STATUS_READY:
        return None
    return DocumentInfo(name=d["name"], file_type=d["file_type"], size=d["size"], page_count=d.get("page_count"))


def page_key(video_id, doc_id: str, page: int) -> str:
    return f"pages/{video_id}/{doc_id}/p{page:04d}.jpg"


def pages_for(video: dict, base_url: str) -> DocumentPages:
    d = video.get("document")
    if not d:
        raise DocumentNotFound()
    if d.get("status") != STATUS_READY or not d.get("page_count"):
        raise DocumentNotReady(
            "The document is still being prepared." if d.get("status") == STATUS_PROCESSING
            else f"The document couldn't be prepared: {d.get('error') or 'unknown error'}"
        )
    ttl = settings.MEDIA_URL_TTL_SECONDS
    return DocumentPages(
        name=d["name"],
        page_count=d["page_count"],
        pages=[media_links.signed_url(base_url, page_key(video["_id"], d["id"], n), ttl)
               for n in range(1, d["page_count"] + 1)],
        links_expire_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
    )


# ── Upload ───────────────────────────────────────────────────────────────────

def _file_type(file_name: str) -> str:
    ext = os.path.splitext(file_name.lower())[1].lstrip(".")
    if ext not in FILE_TYPES:
        raise UnsupportedMediaType(
            "Documents must be PDF (.pdf), Word (.docx) or PowerPoint (.pptx).",
            details={"allowed_extensions": [f".{e}" for e in FILE_TYPES]},
        )
    return ext


def _looks_like(path: Path, file_type: str) -> bool:
    with open(path, "rb") as fh:
        head = fh.read(8)
    if file_type == "pdf":
        return head.startswith(b"%PDF-")
    if not head.startswith(b"PK\x03\x04") or not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return _ZIP_MARKER[file_type] in zf.namelist()
    except zipfile.BadZipFile:
        return False


async def _delete_files(video_id, d: dict) -> None:
    storage = get_storage()
    for key in {d.get("key"), d.get("pdf_key")} - {None}:
        await storage.delete(key)
    await storage.delete_prefix(f"pages/{video_id}/{d['id']}")


def _clean_name(raw_file_name: str) -> str:
    file_name = os.path.basename(unquote(raw_file_name or "").replace("\\", "/")).strip()[:255]
    if not file_name:
        raise UnsupportedMediaType("Send the file name in the X-File-Name header.")
    return file_name


def _check_size(size: Optional[int]) -> None:
    limit = settings.MAX_DOCUMENT_SIZE_MB * MB  # 0 = unlimited
    if limit and size and size > limit:
        raise FileTooLarge(f"Documents can be at most {settings.MAX_DOCUMENT_SIZE_MB} MB.", details={"max_bytes": limit})


async def _replace_document(video: dict, document: dict) -> dict:
    """Make `document` the video's document; remove the old one's files."""
    updated = await video_repo.update(
        video["_id"], {"document": document, "updated_at": document["uploaded_at"]}, unset_fields=["document_upload"]
    )
    old = video.get("document")
    if old and old.get("id") != document["id"]:
        await _delete_files(video["_id"], old)
    return updated


async def start_direct(video_id: str, raw_file_name: str, size: int, sha256: Optional[str]) -> DocumentUploadLink:
    """A signed link for the browser to PUT the document straight into storage.
    url None (local storage, or direct uploads switched off) -> use upload()."""
    video = await video_repo.find_by_id(video_id)
    if not video:
        raise VideoNotFound()
    file_name = _clean_name(raw_file_name)
    file_type = _file_type(file_name)
    _check_size(size)
    storage = get_storage()
    doc_id = uuid.uuid4().hex
    if not settings.UPLOAD_DIRECT_TO_STORAGE or size > _MAX_SINGLE_PUT:
        return DocumentUploadLink(upload_id=doc_id)
    key = f"documents/{video['_id']}/{doc_id}.{file_type}"
    url = await storage.presign_put(key, FILE_TYPES[file_type], size, _DIRECT_LINK_TTL)
    if not url:
        return DocumentUploadLink(upload_id=doc_id)

    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.UPLOAD_TICKET_TTL_HOURS)
    previous = video.get("document_upload")
    await video_repo.update(video["_id"], {"document_upload": {
        "id": doc_id, "key": key, "name": file_name, "file_type": file_type, "size": size,
        "sha256": sha256, "expires_at": expires_at,
    }})
    if previous and previous.get("key") and previous["key"] != (video.get("document") or {}).get("key"):
        await storage.delete(previous["key"])          # an earlier attempt that never finished
    return DocumentUploadLink(
        upload_id=doc_id, url=url, headers={"Content-Type": FILE_TYPES[file_type]},
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=_DIRECT_LINK_TTL),
    )


async def finish_direct(video_id: str, upload_id: str) -> dict:
    """The browser finished its PUT: check the file arrived whole and is the
    right kind, then make it the video's document (prepare() runs next)."""
    video = await video_repo.find_by_id(video_id)
    if not video:
        raise VideoNotFound()
    pending = video.get("document_upload")
    if not pending or pending["id"] != upload_id:
        d = video.get("document")
        if d and d["id"] == upload_id:
            return video                                    # already finished (a retried request)
        raise UploadNotInProgress("This document upload has expired or was replaced. Upload the document again.")
    storage = get_storage()
    size = await storage.object_size(pending["key"])
    if size is None:
        raise UploadIncomplete("The document hasn't arrived in storage. Send it again.")
    if size != pending["size"]:
        await storage.delete(pending["key"])
        raise UploadIncomplete(f"Only {size:,} of {pending['size']:,} bytes arrived. Send the document again.")
    head = await storage.read_head(pending["key"], 8)
    if not head.startswith(_MAGIC[pending["file_type"]]):
        await storage.delete(pending["key"])
        await video_repo.update(video["_id"], {}, unset_fields=["document_upload"])
        raise InvalidMediaFile(f"This file is not a valid .{pending['file_type']} document.")

    now = datetime.now(timezone.utc)
    document = {
        "id": pending["id"], "key": pending["key"], "name": pending["name"], "file_type": pending["file_type"],
        "content_type": FILE_TYPES[pending["file_type"]], "size": size, "uploaded_at": now,
        "sha256": pending.get("sha256"), "status": STATUS_PROCESSING, "error": None,
        "pdf_key": pending["key"] if pending["file_type"] == "pdf" else None, "page_count": None,
    }
    updated = await _replace_document(video, document)
    logger.info("Document stored (direct): video=%s name=%s size=%d", video_id, pending["name"], size)
    return updated


async def upload(
    video_id: str, raw_file_name: str, body: AsyncIterator[bytes], declared_size: Optional[int],
    sha256: Optional[str] = None,
) -> tuple[dict, bool]:
    """Store the document sent through the server (replacing any previous one).
    `sha256` (optional, hex) is checked before anything is stored."""
    import hashlib

    video = await video_repo.find_by_id(video_id)
    if not video:
        raise VideoNotFound()
    file_name = _clean_name(raw_file_name)
    file_type = _file_type(file_name)
    _check_size(declared_size)
    limit = settings.MAX_DOCUMENT_SIZE_MB * MB  # 0 = unlimited
    storage = get_storage()
    free = storage.free_bytes()
    if free is not None and declared_size and free < declared_size * 3 + _FREE_SPACE_MARGIN:
        raise InsufficientStorage(details={"needed_bytes": declared_size, "free_bytes": free})

    tmp = storage.staging_dir() / f"doc-{uuid.uuid4().hex}.part"
    size = 0
    digest = hashlib.sha256()
    try:
        async with await anyio.open_file(tmp, "wb") as fh:
            async for part in body:
                size += len(part)
                if limit and size > limit:
                    raise FileTooLarge(f"Documents can be at most {settings.MAX_DOCUMENT_SIZE_MB} MB.")
                digest.update(part)
                await fh.write(part)
        if size == 0:
            raise InvalidMediaFile("The document is empty.")
        if sha256 and digest.hexdigest() != sha256.strip().lower():
            raise UploadChecksumMismatch("The document was damaged on the way (checksum mismatch). Send it again.")
        if not await anyio.to_thread.run_sync(_looks_like, tmp, file_type):
            raise InvalidMediaFile(f"This file is not a valid .{file_type} document.")

        doc_id = uuid.uuid4().hex
        key = f"documents/{video['_id']}/{doc_id}.{file_type}"
        await storage.put_file(key, tmp, FILE_TYPES[file_type])
    finally:
        tmp.unlink(missing_ok=True)

    now = datetime.now(timezone.utc)
    document = {
        "id": doc_id, "key": key, "name": file_name, "file_type": file_type,
        "content_type": FILE_TYPES[file_type], "size": size, "uploaded_at": now,
        "sha256": digest.hexdigest(), "status": STATUS_PROCESSING, "error": None,
        "pdf_key": key if file_type == "pdf" else None, "page_count": None,
    }
    updated = await _replace_document(video, document)
    logger.info("Document stored: video=%s name=%s size=%d", video_id, file_name, size)
    return updated, True


async def remove(video_id: str) -> dict:
    video = await video_repo.find_by_id(video_id)
    if not video:
        raise VideoNotFound()
    if not video.get("document"):
        return video
    updated = await video_repo.update(
        video["_id"], {"updated_at": datetime.now(timezone.utc)}, unset_fields=["document", "publish_when_ready"]
    )
    await _delete_files(video["_id"], video["document"])
    return updated


async def delete_for_video(video: dict) -> None:
    if video.get("document"):
        await _delete_files(video["_id"], video["document"])
    pending = video.get("document_upload")
    if pending and pending.get("key"):
        await get_storage().delete(pending["key"])


# ── Conversion (background) ──────────────────────────────────────────────────

def _soffice() -> Optional[str]:
    candidates = [settings.LIBREOFFICE_PATH, shutil.which("soffice"), shutil.which("libreoffice"),
                  "/Applications/LibreOffice.app/Contents/MacOS/soffice"]
    return next((c for c in candidates if c and os.path.exists(c)), None)


def _count_pages(pdf_path: Path) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(pdf)
    finally:
        pdf.close()


async def _set_result(video_oid, doc_id: str, **fields) -> None:
    """Update the document only if it is still the same one (not replaced meanwhile)."""
    from app.core.database import videos_collection

    await videos_collection.update_one(
        {"_id": video_oid, "document.id": doc_id},
        {"$set": {f"document.{k}": v for k, v in fields.items()}},
    )


async def _stop_waiting(video_oid, doc_id: str) -> None:
    """Cancel "publish when ready" (the document failed): the admin decides what happens next."""
    from app.core.database import videos_collection

    await videos_collection.update_one(
        {"_id": video_oid, "document.id": doc_id}, {"$unset": {"publish_when_ready": ""}}
    )


async def prepare(video_id: str) -> None:
    """Make the document viewable: convert Word/PowerPoint to PDF, count pages.
    Runs after the upload response (background task); never raises."""
    video = await video_repo.find_by_id(video_id)
    d = (video or {}).get("document")
    if not d or d.get("status") == STATUS_READY:
        return
    storage = get_storage()
    try:
        with tempfile.TemporaryDirectory() as work:
            # Local storage: use the stored file. Cloud storage: download a copy.
            source = storage.local_path(d["key"])
            if source is None:
                source = Path(work, f"{d['id']}.{d['file_type']}")
                await storage.download_to(d["key"], source)
            await anyio.to_thread.run_sync(_verify_file, Path(source), d)

            if d["file_type"] == "pdf":
                pdf_key, pdf_path, produced = d["key"], source, None
            else:
                soffice = _soffice()
                if not soffice:
                    raise RuntimeError(
                        "LibreOffice is not installed on the server, so Word/PowerPoint can't be shown. "
                        "Install it (macOS: brew install --cask libreoffice) and click Retry."
                    )
                pdf_key = f"documents/{video['_id']}/{d['id']}.pdf"
                outdir = Path(work, "out")
                outdir.mkdir()
                profile = Path(work, "profile").as_uri()
                proc = await asyncio.create_subprocess_exec(
                    soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                    "--convert-to", "pdf", "--outdir", str(outdir), str(source),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, err = await asyncio.wait_for(proc.communicate(), timeout=settings.DOCUMENT_CONVERT_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    proc.kill()
                    raise RuntimeError("Converting the document took too long.")
                produced = Path(outdir, Path(source).stem + ".pdf")
                if proc.returncode != 0 or not produced.exists():
                    raise RuntimeError(f"LibreOffice could not convert the document. {(err or b'').decode(errors='ignore')[:200]}")
                pdf_path = produced

            pages = await anyio.to_thread.run_sync(_count_pages, pdf_path)
            if pages < 1:
                raise RuntimeError("The document has no pages.")
            if produced is not None:
                await storage.put_file(pdf_key, produced, FILE_TYPES["pdf"])
        await _set_result(video["_id"], d["id"], status=STATUS_READY, pdf_key=pdf_key, page_count=pages, error=None)
        logger.info("Document ready: video=%s pages=%d", video_id, pages)
        if await video_repo.publish_if_waiting(video["_id"]):
            logger.info("Published video %s: its document is ready", video_id)
    except Exception as exc:  # noqa: BLE001 — recorded on the document, shown in the dashboard
        if isinstance(exc, ImportError):
            message = ("The server is missing the PDF tools. Run: .venv/bin/python3 -m pip install -r "
                       "requirements.txt, restart the server, then click Retry.")
        elif isinstance(exc, RuntimeError):
            message = str(exc)
        else:
            message = f"The file couldn't be read ({type(exc).__name__})."
        logger.warning("Document preparation failed for video %s: %s", video_id, exc)
        await _set_result(video["_id"], d["id"], status=STATUS_FAILED, error=message)
        await _stop_waiting(video["_id"], d["id"])      # a failed document never publishes the video later


def _verify_file(path: Path, d: dict) -> None:
    """Before preparing: the stored bytes are exactly the file the admin chose
    (SHA-256, when known) and really are a PDF / Word / PowerPoint file."""
    import hashlib

    if d.get("sha256"):
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(8 * MB), b""):
                digest.update(block)
        if digest.hexdigest() != d["sha256"]:
            raise RuntimeError("The document was damaged during upload. Remove it and upload it again.")
    if not _looks_like(path, d["file_type"]):
        raise RuntimeError(f"This file is not a valid .{d['file_type']} document. Remove it and upload the right file.")


async def retry(video_id: str) -> dict:
    video = await video_repo.find_by_id(video_id)
    if not video:
        raise VideoNotFound()
    if not video.get("document"):
        raise DocumentNotFound()
    await _set_result(video["_id"], video["document"]["id"], status=STATUS_PROCESSING, error=None)
    return await video_repo.find_by_id(video_id)


# ── Page images ──────────────────────────────────────────────────────────────

def _render(pdf_path: Path, page: int, out: Path) -> None:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        p = pdf[page - 1]
        scale = settings.DOCUMENT_PAGE_WIDTH / max(p.get_width(), 1)
        image = p.render(scale=min(scale, 4.0)).to_pil().convert("RGB")
        image.save(out, "JPEG", quality=80, optimize=True)
    finally:
        pdf.close()


def _cache_dir() -> Path:
    path = Path(settings.MEDIA_CACHE_DIR or Path(tempfile.gettempdir(), "nutriblend-cache"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _trim_cache(folder: Path, keep: Path) -> None:
    """Delete the least recently used cached files above MEDIA_CACHE_MAX_MB."""
    files = sorted((p for p in folder.glob("*.pdf") if p.is_file()), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    limit = settings.MEDIA_CACHE_MAX_MB * MB
    for p in files:
        if total <= limit:
            break
        if p != keep:
            total -= p.stat().st_size
            p.unlink(missing_ok=True)


async def _local_pdf(pdf_key: str) -> Path:
    """A local file with the PDF: the stored file (local storage) or a cached download (cloud)."""
    storage = get_storage()
    path = storage.local_path(pdf_key)
    if path is not None:
        return path
    folder = _cache_dir()
    cached = folder / (pdf_key.replace("/", "__"))
    if await anyio.to_thread.run_sync(cached.is_file):
        await anyio.to_thread.run_sync(os.utime, cached)  # mark as recently used
        return cached
    tmp = folder / f"{cached.name}.{uuid.uuid4().hex}.part"
    try:
        await storage.download_to(pdf_key, tmp)
        await anyio.to_thread.run_sync(os.replace, tmp, cached)
    finally:
        tmp.unlink(missing_ok=True)
    await anyio.to_thread.run_sync(_trim_cache, folder, cached)
    return cached


async def _page_source(key: str) -> Optional[tuple[dict, int]]:
    """(document, page number) if `key` names a page that can exist, else None."""
    parts = key.split("/")  # pages/<video>/<doc>/p0001.jpg
    if len(parts) != 4 or not ObjectId.is_valid(parts[1]) or not parts[3][1:5].isdigit():
        return None
    video = await video_repo.find_by_id(parts[1])
    d = (video or {}).get("document")
    page = int(parts[3][1:5])
    if not d or d["id"] != parts[2] or d.get("status") != STATUS_READY or not 1 <= page <= (d.get("page_count") or 0):
        return None
    return d, page


_render_locks: dict[str, asyncio.Lock] = {}


async def _render_and_store(key: str, d: dict, page: int) -> None:
    storage = get_storage()
    pdf_path = await _local_pdf(d["pdf_key"])
    tmp = storage.staging_dir() / f"page-{uuid.uuid4().hex}.jpg"
    try:
        await anyio.to_thread.run_sync(_render, pdf_path, page, tmp)
        await storage.put_file(key, tmp, "image/jpeg")
    finally:
        tmp.unlink(missing_ok=True)


async def page_image(key: str) -> Optional[Path]:
    """Local storage: path of a page image (rendered on first request). None if it can't exist."""
    storage = get_storage()
    path = storage.local_path(key)
    if path is not None and await anyio.to_thread.run_sync(path.is_file):
        return path
    found = await _page_source(key)
    if not found:
        return None
    await _render_and_store(key, *found)
    return storage.local_path(key)


async def ensure_page(key: str) -> bool:
    """Cloud storage: make sure the page image exists in the bucket (rendered on
    first request). False if no such page can exist."""
    storage = get_storage()
    if await storage.exists(key):
        return True
    found = await _page_source(key)
    if not found:
        return False
    lock = _render_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            if not await storage.exists(key):  # another request may have rendered it meanwhile
                await _render_and_store(key, *found)
    finally:
        if not lock.locked():
            _render_locks.pop(key, None)
    return True


async def resume_unfinished() -> int:
    """Restart preparation of documents a restart or deploy interrupted."""
    from app.core.database import videos_collection

    ids = [str(v["_id"]) async for v in videos_collection.find({"document.status": STATUS_PROCESSING}, {"_id": 1})]
    for video_id in ids:
        asyncio.create_task(prepare(video_id))
    if ids:
        logger.info("Resuming preparation of %d document(s)", len(ids))
    return len(ids)
