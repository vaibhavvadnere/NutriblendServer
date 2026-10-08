"""
routers/media.py — Serves media files from the local storage provider.

    GET/HEAD /media/<key>?exp=<unix time>&sig=<signature>

The signed link is the permission (players can't send auth headers); see
core/media_links.py. Supports HTTP Range requests, which video players need to
seek and to start playing before the whole file has downloaded.

With cloud storage (STORAGE_PROVIDER=r2) this route still checks the signed
link, then answers 302 with a short-lived presigned R2 URL: the bytes go from R2
straight to the player (Range requests included) and never through this server.
Document page images are rendered into the bucket on first request.
"""

import mimetypes
import os
import re

import anyio
from fastapi import APIRouter, Query, Request, status
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from app.core import media_links
from app.core.config import settings
from app.core.errors import APIError, ErrorCode
from app.core.exceptions import MediaLinkInvalid
from app.providers.storage import StorageError, get_storage
from app.services import document_service

router = APIRouter(prefix=media_links.MEDIA_PREFIX, tags=["media"])

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_READ_SIZE = 256 * 1024


def _not_found() -> APIError:
    return APIError(status.HTTP_404_NOT_FOUND, ErrorCode.NOT_FOUND, "File not found.")


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """(start, end) inclusive for a single 'bytes=' range, or None if unsatisfiable."""
    match = _RANGE_RE.match(header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None
    first, last = match.groups()
    if first == "":  # suffix range: last N bytes
        length = int(last)
        if length == 0:
            return None
        return max(size - length, 0), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


async def _redirect_to_cloud(key: str) -> Response:
    try:
        if key.startswith("pages/"):
            found = await document_service.ensure_page(key)
        else:
            found = await get_storage().exists(key)
        if not found:
            raise _not_found()
        url = await get_storage().presigned_url(key, settings.R2_PRESIGNED_TTL_SECONDS)
    except StorageError:
        raise _not_found()
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND, headers={"Cache-Control": "private, no-store"})


@router.api_route("/{key:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_media(request: Request, key: str, exp: int = Query(...), sig: str = Query(...)):
    if not media_links.verify(key, exp, sig):
        raise MediaLinkInvalid()

    # Original documents and their PDF renditions are never served (view-only);
    # viewers get page images instead.
    if key.startswith("documents/"):
        raise _not_found()

    if get_storage().name != "local":
        return await _redirect_to_cloud(key)
    try:
        if key.startswith("pages/"):
            path = await document_service.page_image(key)
        else:
            path = get_storage().local_path(key)
    except StorageError:
        raise _not_found()
    if path is None or not await anyio.to_thread.run_sync(path.is_file):
        raise _not_found()

    size = (await anyio.to_thread.run_sync(os.stat, path)).st_size
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        # View-only: shown inline, never offered as a download or kept in caches.
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Content-Type": content_type,
    }

    start, end, code = 0, size - 1, status.HTTP_200_OK
    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range(range_header, size)
        if parsed is None:
            return Response(
                status_code=416,  # Range Not Satisfiable
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        start, end = parsed
        code = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)

    if request.method == "HEAD":
        return Response(status_code=code, headers=headers)

    async def body():
        async with await anyio.open_file(path, "rb") as fh:
            await fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = await fh.read(min(_READ_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=code, headers=headers, media_type=content_type)
