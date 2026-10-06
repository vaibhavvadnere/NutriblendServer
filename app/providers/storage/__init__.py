"""
providers/storage — Where media file bytes live. Chosen by STORAGE_PROVIDER:

    STORAGE_PROVIDER=local   # a folder on this server (MEDIA_ROOT) — development

Cloudflare R2 / AWS S3 plug in here later as another StorageProvider (see
base.py; the chunked-upload methods map onto S3 multipart uploads). Nothing
outside this package needs to change.
"""

from app.core.config import settings
from app.providers.storage.base import StorageError, StorageProvider
from app.providers.storage.local import LocalStorage

_cached: StorageProvider | None = None


def get_storage() -> StorageProvider:
    """The configured provider (built once, then reused)."""
    global _cached
    if _cached is None:
        if settings.STORAGE_PROVIDER == "local":
            _cached = LocalStorage(settings.MEDIA_ROOT)
        else:  # pragma: no cover — guarded by the config Literal
            raise RuntimeError(f"Unknown STORAGE_PROVIDER: {settings.STORAGE_PROVIDER}")
    return _cached


__all__ = ["StorageError", "StorageProvider", "get_storage"]
