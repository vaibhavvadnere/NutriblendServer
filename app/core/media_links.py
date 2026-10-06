"""
core/media_links.py — Expiring, signed links to media files.

Video players can't easily send an Authorization header, so the link itself
is the permission: /media/<key>?exp=<unix time>&sig=<HMAC(key, exp)>.
Links are only handed to signed-in users (app list / admin screens) and stop
working after MEDIA_URL_TTL_SECONDS. Changing MEDIA_URL_SECRET (or JWT_SECRET
when it is empty) invalidates every link at once.
"""

import base64
import hashlib
import hmac
import time
from urllib.parse import quote

from app.core.config import settings

MEDIA_PREFIX = "/media"


def _secret() -> bytes:
    if settings.MEDIA_URL_SECRET:
        return settings.MEDIA_URL_SECRET.encode()
    # Derived, so it never equals the JWT secret itself.
    return hashlib.sha256(b"nutriblend-media:" + settings.JWT_SECRET.encode()).digest()


def _sign(key: str, exp: int) -> str:
    mac = hmac.new(_secret(), f"{key}:{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def signed_url(base_url: str, key: str, ttl_seconds: int | None = None) -> str:
    exp = int(time.time()) + (ttl_seconds or settings.MEDIA_URL_TTL_SECONDS)
    return f"{base_url.rstrip('/')}{MEDIA_PREFIX}/{quote(key)}?exp={exp}&sig={_sign(key, exp)}"


def verify(key: str, exp: int, sig: str) -> bool:
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_sign(key, exp), sig)


def seconds_left(exp: int) -> int:
    return max(0, exp - int(time.time()))
