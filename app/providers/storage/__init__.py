"""
providers/storage — Where media file bytes live. Chosen by STORAGE_PROVIDER:

    STORAGE_PROVIDER=local   # a folder on this server (MEDIA_ROOT) — development
    STORAGE_PROVIDER=r2      # Cloudflare R2 (private bucket) — production
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
        elif settings.STORAGE_PROVIDER == "r2":
            from app.providers.storage.r2 import R2Storage

            _cached = R2Storage()
        else:  # pragma: no cover — guarded by the config Literal
            raise RuntimeError(f"Unknown STORAGE_PROVIDER: {settings.STORAGE_PROVIDER}")
    return _cached


_staging: StorageProvider | None = None


def get_staging_storage() -> StorageProvider:
    """A folder on this server where uploaded videos wait to be optimized (VIDEO_STAGING_ROOT)."""
    global _staging
    if _staging is None:
        _staging = LocalStorage(settings.VIDEO_STAGING_ROOT)
    return _staging


__all__ = ["StorageError", "StorageProvider", "get_storage", "get_staging_storage"]
