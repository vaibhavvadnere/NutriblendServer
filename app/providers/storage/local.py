"""
providers/storage/local.py — Files in a folder on this server (MEDIA_ROOT).

Good for development. Before go-live, move to cloud storage: on most hosts a
redeploy wipes the server's disk, and the server would pay for all video
bandwidth.

Layout under MEDIA_ROOT:
    videos/<id>/<random>.mp4          finished files
    thumbnails/<id>/<random>.jpg
    documents/<id>/<random>.pdf|docx|pptx    (+ .pdf rendition for Word/PowerPoint)
    pages/<id>/<document id>/p0001.jpg        rendered document pages (cache)
    .uploads/<key with '/' -> '__'>.part   uploads in progress

Chunks are written straight into the .part file at their offset (os.pwrite),
so finishing an upload is an instant rename — no copying of multi-GB files.
"""

import os
import re
import shutil
from pathlib import Path
from typing import Optional

import anyio

from app.providers.storage.base import StorageError, StorageProvider

# Only server-generated keys are valid. Anything else (.., absolute paths,
# odd characters) is refused before touching the filesystem.
_KEY_RE = re.compile(
    r"^(?:(?:videos|thumbnails|documents)/[0-9a-f]{24}/[0-9a-f]{32}\.(?:mp4|jpg|png|webp|pdf|docx|pptx)"
    r"|pages/[0-9a-f]{24}/[0-9a-f]{32}/p\d{4}\.jpg)$"
)
_PREFIX_RE = re.compile(r"^pages/[0-9a-f]{24}/[0-9a-f]{32}$")


class LocalStorage(StorageProvider):
    name = "local"

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.uploads = self.root / ".uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)

    # ── paths ────────────────────────────────────────────────────────────────

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise StorageError(f"invalid storage key: {key!r}")
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError(f"key escapes the media root: {key!r}")
        return path

    def _part(self, key: str) -> Path:
        self._path(key)  # validate
        return self.uploads / (key.replace("/", "__") + ".part")

    # ── chunked uploads ──────────────────────────────────────────────────────

    async def begin_upload(self, key: str, size: int) -> None:
        part = self._part(key)

        def _create() -> None:
            with open(part, "wb") as fh:
                fh.truncate(size)  # sparse on most filesystems; fills as chunks land

        await self._run(_create)

    async def write_chunk(self, key: str, offset: int, data: bytes, content_md5: Optional[str] = None) -> None:
        # Local disk: the service already verified `data` against its MD5, and
        # part_checksums() re-reads what was written before completing.
        part = self._part(key)

        def _write() -> None:
            fd = os.open(part, os.O_WRONLY)
            try:
                written = os.pwrite(fd, data, offset)
                if written != len(data):
                    raise StorageError("short write")
            finally:
                os.close(fd)

        await self._run(_write)

    async def part_checksums(self, key: str, chunk_size: int) -> Optional[dict[int, tuple[int, Optional[str]]]]:
        """Re-read the in-progress file and hash each chunk-sized region."""
        import hashlib

        part = self._part(key)

        def _hash() -> dict[int, tuple[int, Optional[str]]]:
            out: dict[int, tuple[int, Optional[str]]] = {}
            with open(part, "rb") as fh:
                index = 0
                while True:
                    block = fh.read(chunk_size)
                    if not block:
                        break
                    out[index] = (len(block), hashlib.md5(block).hexdigest())
                    index += 1
            return out

        return await self._run(_hash)

    async def upload_sha256(self, key: str) -> Optional[str]:
        import hashlib

        part = self._part(key)

        def _hash() -> str:
            h = hashlib.sha256()
            with open(part, "rb") as fh:
                for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                    h.update(block)
            return h.hexdigest()

        return await self._run(_hash)

    async def read_upload_head(self, key: str, length: int) -> bytes:
        part = self._part(key)

        def _read() -> bytes:
            with open(part, "rb") as fh:
                return fh.read(length)

        return await self._run(_read)

    async def complete_upload(self, key: str) -> None:
        part, final = self._part(key), self._path(key)

        def _finish() -> None:
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part, final)  # atomic on the same filesystem

        await self._run(_finish)

    async def abort_upload(self, key: str) -> None:
        part = self._part(key)
        await self._run(lambda: part.unlink(missing_ok=True))

    # ── whole objects ────────────────────────────────────────────────────────

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        final = self._path(key)

        def _put() -> None:
            final.parent.mkdir(parents=True, exist_ok=True)
            tmp = final.with_suffix(final.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, final)

        await self._run(_put)

    async def delete(self, key: str) -> None:
        path = self._path(key)

        def _delete() -> None:
            path.unlink(missing_ok=True)
            for parent in (path.parent, path.parent.parent):
                if parent == self.root:
                    break
                try:
                    parent.rmdir()  # remove per-video folders once empty
                except OSError:
                    break

        await self._run(_delete)

    async def put_file(self, key: str, src: Path, content_type: str) -> None:
        final = self._path(key)

        def _move() -> None:
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(final))

        await self._run(_move)

    async def delete_prefix(self, prefix: str) -> None:
        if not _PREFIX_RE.match(prefix):
            raise StorageError(f"invalid prefix: {prefix!r}")
        folder = (self.root / prefix).resolve()
        await self._run(lambda: shutil.rmtree(folder, ignore_errors=True))

    async def object_size(self, key: str) -> Optional[int]:
        path = self._path(key)

        def _size() -> Optional[int]:
            return path.stat().st_size if path.is_file() else None

        return await anyio.to_thread.run_sync(_size)

    async def read_head(self, key: str, length: int) -> bytes:
        path = self._path(key)

        def _read() -> bytes:
            with open(path, "rb") as fh:
                return fh.read(length)

        return await anyio.to_thread.run_sync(_read)

    def staging_dir(self) -> Path:
        return self.uploads

    async def exists(self, key: str) -> bool:
        path = self._path(key)
        return await anyio.to_thread.run_sync(path.is_file)

    # ── serving & capacity ───────────────────────────────────────────────────

    def local_path(self, key: str) -> Optional[Path]:
        return self._path(key)

    def free_bytes(self) -> Optional[int]:
        return shutil.disk_usage(self.root).free

    def health(self) -> str:
        return "ok" if os.access(self.root, os.W_OK) else "not writable"

    @staticmethod
    async def _run(fn):
        try:
            return await anyio.to_thread.run_sync(fn)
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(f"{type(exc).__name__}: {exc}") from exc
