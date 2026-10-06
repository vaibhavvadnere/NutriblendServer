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
    document: { id, key, name, file_type, content_type, size, uploaded_at,
                status: processing|ready|failed, error?, pdf_key?, page_count? }
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
    VideoNotFound,
)
from app.providers.storage import get_storage
from app.repositories import video_repo
from app.schemas.video import AdminDocumentInfo, DocumentInfo, DocumentPages

logger = logging.getLogger("nutriblend.documents")

MB = 1024 * 1024
FILE_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_ZIP_MARKER = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml"}
_FREE_SPACE_MARGIN = 200 * MB

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


async def upload(
    video_id: str, raw_file_name: str, body: AsyncIterator[bytes], declared_size: Optional[int]
) -> tuple[dict, bool]:
    """Store the document (replacing any previous one). Returns (video, needs_conversion)."""
    video = await video_repo.find_by_id(video_id)
    if not video:
        raise VideoNotFound()
    file_name = os.path.basename(unquote(raw_file_name or "").replace("\\", "/")).strip()[:255]
    if not file_name:
        raise UnsupportedMediaType("Send the file name in the X-File-Name header.")
    file_type = _file_type(file_name)

    limit = settings.MAX_DOCUMENT_SIZE_MB * MB  # 0 = unlimited
    if limit and declared_size and declared_size > limit:
        raise FileTooLarge(f"Documents can be at most {settings.MAX_DOCUMENT_SIZE_MB} MB.", details={"max_bytes": limit})
    storage = get_storage()
    free = storage.free_bytes()
    if free is not None and declared_size and free < declared_size * 3 + _FREE_SPACE_MARGIN:
        raise InsufficientStorage(details={"needed_bytes": declared_size, "free_bytes": free})

    tmp = storage.staging_dir() / f"doc-{uuid.uuid4().hex}.part"
    size = 0
    try:
        async with await anyio.open_file(tmp, "wb") as fh:
            async for part in body:
                size += len(part)
                if limit and size > limit:
                    raise FileTooLarge(f"Documents can be at most {settings.MAX_DOCUMENT_SIZE_MB} MB.")
                await fh.write(part)
        if size == 0:
            raise InvalidMediaFile("The document is empty.")
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
        "status": STATUS_PROCESSING, "error": None, "pdf_key": key if file_type == "pdf" else None,
        "page_count": None,
    }
    updated = await video_repo.update(video["_id"], {"document": document, "updated_at": now})
    if video.get("document"):
        await _delete_files(video["_id"], video["document"])
    logger.info("Document stored: video=%s name=%s size=%d", video_id, file_name, size)
    return updated, True


async def remove(video_id: str) -> dict:
    video = await video_repo.find_by_id(video_id)
    if not video:
        raise VideoNotFound()
    if not video.get("document"):
        return video
    updated = await video_repo.update(video["_id"], {"updated_at": datetime.now(timezone.utc)}, unset_fields=["document"])
    await _delete_files(video["_id"], video["document"])
    return updated


async def delete_for_video(video: dict) -> None:
    if video.get("document"):
        await _delete_files(video["_id"], video["document"])


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


async def prepare(video_id: str) -> None:
    """Make the document viewable: convert Word/PowerPoint to PDF, count pages.
    Runs after the upload response (background task); never raises."""
    video = await video_repo.find_by_id(video_id)
    d = (video or {}).get("document")
    if not d or d.get("status") == STATUS_READY:
        return
    storage = get_storage()
    try:
        source = storage.local_path(d["key"])
        if d["file_type"] == "pdf":
            pdf_key, pdf_path = d["key"], source
        else:
            soffice = _soffice()
            if not soffice:
                raise RuntimeError(
                    "LibreOffice is not installed on the server, so Word/PowerPoint can't be shown. "
                    "Install it (macOS: brew install --cask libreoffice) and click Retry."
                )
            pdf_key = f"documents/{video['_id']}/{d['id']}.pdf"
            with tempfile.TemporaryDirectory() as work:
                profile = Path(work, "profile").as_uri()
                proc = await asyncio.create_subprocess_exec(
                    soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                    "--convert-to", "pdf", "--outdir", work, str(source),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, err = await asyncio.wait_for(proc.communicate(), timeout=settings.DOCUMENT_CONVERT_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    proc.kill()
                    raise RuntimeError("Converting the document took too long.")
                produced = Path(work, Path(source).stem + ".pdf")
                if proc.returncode != 0 or not produced.exists():
                    raise RuntimeError(f"LibreOffice could not convert the document. {(err or b'').decode(errors='ignore')[:200]}")
                await storage.put_file(pdf_key, produced, FILE_TYPES["pdf"])
            pdf_path = storage.local_path(pdf_key)

        pages = await anyio.to_thread.run_sync(_count_pages, pdf_path)
        if pages < 1:
            raise RuntimeError("The document has no pages.")
        await _set_result(video["_id"], d["id"], status=STATUS_READY, pdf_key=pdf_key, page_count=pages, error=None)
        logger.info("Document ready: video=%s pages=%d", video_id, pages)
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


async def page_image(key: str) -> Optional[Path]:
    """Local path of a page image (rendered on first request). None if it can't exist."""
    parts = key.split("/")  # pages/<video>/<doc>/p0001.jpg
    if len(parts) != 4 or not ObjectId.is_valid(parts[1]):
        return None
    storage = get_storage()
    path = storage.local_path(key)
    if path is not None and await anyio.to_thread.run_sync(path.is_file):
        return path

    video = await video_repo.find_by_id(parts[1])
    d = (video or {}).get("document")
    page = int(parts[3][1:5])
    if not d or d["id"] != parts[2] or d.get("status") != STATUS_READY or not 1 <= page <= (d.get("page_count") or 0):
        return None
    tmp = storage.staging_dir() / f"page-{uuid.uuid4().hex}.jpg"
    try:
        await anyio.to_thread.run_sync(_render, storage.local_path(d["pdf_key"]), page, tmp)
        await storage.put_file(key, tmp, "image/jpeg")
    finally:
        tmp.unlink(missing_ok=True)
    return storage.local_path(key)
