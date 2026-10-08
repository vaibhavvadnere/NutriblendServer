"""
providers/storage/r2.py — Cloudflare R2 (S3-compatible API, via boto3).

    STORAGE_PROVIDER=r2
    R2_ACCOUNT_ID=...  R2_ACCESS_KEY_ID=...  R2_SECRET_ACCESS_KEY=...  R2_BUCKET=...

The bucket stays private. Clients never get R2 credentials: the /media route
checks the app's own signed link, then redirects to a presigned R2 URL that
expires after R2_PRESIGNED_TTL_SECONDS. Video bytes therefore go straight from
R2 to the phone (R2 has no download/egress fees) and never through the server.

Chunked uploads use S3 multipart uploads. Chunk n becomes part n+1, so chunks
may arrive in any order and re-sending one simply replaces that part. R2 needs
every part except the last to be the same size (>= 5 MB), which is exactly how
the upload protocol splits files (UPLOAD_CHUNK_SIZE_MB).

An unfinished multipart upload can't be read back, so the upload id and the
first bytes of the file (for MP4 sniffing) are kept in the Mongo collection
`storage_uploads` (auto-deleted after 2 days). The bucket's default lifecycle
rule aborts multipart uploads left unfinished for 7 days.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import anyio
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.providers.storage.base import ChecksumMismatch, StorageError, StorageProvider

logger = logging.getLogger("nutriblend.storage.r2")

# Same rules as local.py: only server-generated keys are accepted.
_KEY_RE = re.compile(
    r"^(?:(?:videos|thumbnails|documents)/[0-9a-f]{24}/[0-9a-f]{32}\.(?:mp4|jpg|png|webp|pdf|docx|pptx)"
    r"|pages/[0-9a-f]{24}/[0-9a-f]{32}/p\d{4}\.jpg)$"
)
_PREFIX_RE = re.compile(r"^pages/[0-9a-f]{24}/[0-9a-f]{32}$")

_CONTENT_TYPES = {
    "mp4": "video/mp4",
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

_HEAD_BYTES = 64 * 1024
_UPLOAD_RECORD_TTL = timedelta(days=2)


def _content_type(key: str) -> str:
    return _CONTENT_TYPES.get(key.rsplit(".", 1)[-1], "application/octet-stream")


def _check_key(key: str) -> str:
    if not _KEY_RE.match(key):
        raise StorageError(f"invalid storage key: {key!r}")
    return key


class R2Storage(StorageProvider):
    name = "r2"

    def __init__(self) -> None:
        required = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
        if not settings.R2_ENDPOINT_URL:
            required.insert(0, "R2_ACCOUNT_ID")
        missing = [n for n in required if not getattr(settings, n)]
        if missing:
            raise RuntimeError(f"STORAGE_PROVIDER=r2 needs these settings in .env: {', '.join(missing)}")
        self.bucket = settings.R2_BUCKET
        endpoint = settings.R2_ENDPOINT_URL or f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 4, "mode": "standard"},
                connect_timeout=10,
                read_timeout=120,
                max_pool_connections=20,
            ),
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _uploads():
        from app.core.database import storage_uploads_collection

        return storage_uploads_collection

    async def _call(self, fn, *args, **kwargs) -> Any:
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"R2: {exc}") from exc

    async def _upload_record(self, key: str) -> dict:
        record = await self._uploads().find_one({"_id": key})
        if not record:
            raise StorageError(f"no upload in progress for {key!r}")
        return record

    # ── chunked uploads ──────────────────────────────────────────────────────

    async def begin_upload(self, key: str, size: int) -> None:
        _check_key(key)
        resp = await self._call(
            self.client.create_multipart_upload, Bucket=self.bucket, Key=key, ContentType=_content_type(key)
        )
        now = datetime.now(timezone.utc)
        await self._uploads().replace_one(
            {"_id": key},
            {
                "_id": key,
                "upload_id": resp["UploadId"],
                "size": size,
                "part_size": settings.UPLOAD_CHUNK_SIZE_MB * 1024 * 1024,
                "head": b"",
                "created_at": now,
                "expires_at": now + _UPLOAD_RECORD_TTL,
            },
            upsert=True,
        )

    async def write_chunk(self, key: str, offset: int, data: bytes, content_md5: Optional[str] = None) -> None:
        record = await self._upload_record(_check_key(key))
        part_size = record["part_size"]
        if offset % part_size:
            raise StorageError(f"chunk offset {offset} is not a multiple of {part_size}")
        kwargs: dict[str, Any] = {}
        if content_md5:
            kwargs["ContentMD5"] = content_md5  # R2 rejects the part (BadDigest) if the bytes differ
        try:
            await anyio.to_thread.run_sync(lambda: self.client.upload_part(
                Bucket=self.bucket, Key=key, UploadId=record["upload_id"],
                PartNumber=offset // part_size + 1, Body=data, **kwargs,
            ))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("BadDigest", "InvalidDigest"):
                raise ChecksumMismatch(f"R2 rejected part {offset // part_size + 1}: checksum mismatch") from exc
            raise StorageError(f"R2: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"R2: {exc}") from exc
        if offset == 0:
            await self._uploads().update_one({"_id": key}, {"$set": {"head": data[:_HEAD_BYTES]}})

    async def part_checksums(self, key: str, chunk_size: int) -> Optional[dict[int, tuple[int, Optional[str]]]]:
        """Parts R2 holds: {chunk index: (size, md5 hex)}. A plain part ETag is the
        part's MD5; anything else (unexpected format) is reported as unknown."""
        record = await self._upload_record(_check_key(key))
        out: dict[int, tuple[int, Optional[str]]] = {}
        marker = 0
        while True:
            page = await self._call(
                self.client.list_parts,
                Bucket=self.bucket, Key=key, UploadId=record["upload_id"], PartNumberMarker=marker,
            )
            for p in page.get("Parts", []):
                etag = str(p.get("ETag", "")).strip('"').lower()
                md5 = etag if re.fullmatch(r"[0-9a-f]{32}", etag) else None
                out[p["PartNumber"] - 1] = (int(p.get("Size", 0)), md5)
            if not page.get("IsTruncated"):
                break
            marker = page["NextPartNumberMarker"]
        return out

    async def presign_put(self, key: str, content_type: str, size: int, ttl_seconds: int) -> Optional[str]:
        """Signed PutObject link (documents). Content-Type and Content-Length are
        part of the signature, so only a file of exactly that size is accepted."""
        return await self._call(
            self.client.generate_presigned_url,
            "put_object",
            Params={"Bucket": self.bucket, "Key": _check_key(key), "ContentType": content_type, "ContentLength": size},
            ExpiresIn=ttl_seconds,
        )

    async def object_size(self, key: str) -> Optional[int]:
        try:
            head = await anyio.to_thread.run_sync(
                lambda: self.client.head_object(Bucket=self.bucket, Key=_check_key(key))
            )
            return int(head["ContentLength"])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError(f"R2: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"R2: {exc}") from exc

    async def read_head(self, key: str, length: int) -> bytes:
        resp = await self._call(
            self.client.get_object, Bucket=self.bucket, Key=_check_key(key), Range=f"bytes=0-{max(0, length - 1)}"
        )
        return await anyio.to_thread.run_sync(resp["Body"].read)

    async def presign_part(self, key: str, index: int, content_md5: str, ttl_seconds: int) -> Optional[str]:
        """Signed UploadPart link for chunk `index` (part index+1). Content-MD5 is
        part of the signature: R2 refuses the PUT unless the browser sends that
        exact header, and refuses bytes whose MD5 differs (BadDigest)."""
        record = await self._upload_record(_check_key(key))
        return await self._call(
            self.client.generate_presigned_url,
            "upload_part",
            Params={"Bucket": self.bucket, "Key": key, "UploadId": record["upload_id"],
                    "PartNumber": index + 1, "ContentMD5": content_md5},
            ExpiresIn=ttl_seconds,
        )

    async def read_upload_head(self, key: str, length: int) -> bytes:
        record = await self._upload_record(_check_key(key))
        return bytes(record.get("head") or b"")[:length]

    async def complete_upload(self, key: str) -> None:
        record = await self._upload_record(_check_key(key))
        parts, marker = [], 0
        while True:
            page = await self._call(
                self.client.list_parts,
                Bucket=self.bucket, Key=key, UploadId=record["upload_id"], PartNumberMarker=marker,
            )
            parts += [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in page.get("Parts", [])]
            if not page.get("IsTruncated"):
                break
            marker = page["NextPartNumberMarker"]
        expected = max(1, -(-record["size"] // record["part_size"]))
        if [p["PartNumber"] for p in parts] != list(range(1, expected + 1)):
            raise StorageError(f"R2 has {len(parts)} of {expected} parts for {key!r}")
        await self._call(
            self.client.complete_multipart_upload,
            Bucket=self.bucket, Key=key, UploadId=record["upload_id"], MultipartUpload={"Parts": parts},
        )
        await self._uploads().delete_one({"_id": key})

    async def abort_upload(self, key: str) -> None:
        record = await self._uploads().find_one({"_id": key})
        if not record:
            return
        try:
            await self._call(
                self.client.abort_multipart_upload, Bucket=self.bucket, Key=key, UploadId=record["upload_id"]
            )
        except StorageError as exc:
            if "NoSuchUpload" not in str(exc):
                raise
        await self._uploads().delete_one({"_id": key})

    # ── whole objects ────────────────────────────────────────────────────────

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._call(
            self.client.put_object, Bucket=self.bucket, Key=_check_key(key), Body=data, ContentType=content_type
        )

    async def put_file(self, key: str, src: Path, content_type: str) -> None:
        await self._call(
            self.client.upload_file, str(src), self.bucket, _check_key(key),
            ExtraArgs={"ContentType": content_type},
        )
        Path(src).unlink(missing_ok=True)  # same contract as local: the source is consumed

    async def delete(self, key: str) -> None:
        await self._call(self.client.delete_object, Bucket=self.bucket, Key=_check_key(key))

    async def exists(self, key: str) -> bool:
        try:
            await anyio.to_thread.run_sync(
                lambda: self.client.head_object(Bucket=self.bucket, Key=_check_key(key))
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise StorageError(f"R2: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"R2: {exc}") from exc

    async def delete_prefix(self, prefix: str) -> None:
        if not _PREFIX_RE.match(prefix):
            raise StorageError(f"invalid prefix: {prefix!r}")
        token: Optional[str] = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix + "/"}
            if token:
                kwargs["ContinuationToken"] = token
            page = await self._call(self.client.list_objects_v2, **kwargs)
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            for i in range(0, len(keys), 1000):
                await self._call(
                    self.client.delete_objects,
                    Bucket=self.bucket, Delete={"Objects": keys[i:i + 1000], "Quiet": True},
                )
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]

    async def download_to(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self._call(self.client.download_file, self.bucket, _check_key(key), str(dest))

    async def presigned_url(self, key: str, ttl_seconds: int) -> Optional[str]:
        params = {
            "Bucket": self.bucket,
            "Key": _check_key(key),
            # View-only: shown inline, never offered as a download or kept in caches.
            "ResponseContentDisposition": "inline",
            "ResponseCacheControl": "private, no-store",
        }
        return await self._call(
            self.client.generate_presigned_url, "get_object", Params=params, ExpiresIn=ttl_seconds
        )

    # ── health ───────────────────────────────────────────────────────────────

    async def check(self) -> str:
        """Round-trip to the bucket (startup log). Never raises."""
        try:
            await self._call(self.client.head_bucket, Bucket=self.bucket)
            return "ok"
        except StorageError as exc:
            return f"unreachable ({exc})"[:200]

    def health(self) -> str:
        return "ok"
