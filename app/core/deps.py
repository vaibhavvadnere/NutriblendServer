"""
core/deps.py — Shared FastAPI dependencies and request helpers.

This is HTTP-layer code: it reads headers and the socket address, then hands
plain values to the services.
"""

import logging
from functools import lru_cache
from typing import Optional

from fastapi import Depends, Header, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.errors import APIError, ErrorCode
from app.core.exceptions import AccountGone, AdminCreateDisabled, AdminOnly
from app.core.security import (
    SCOPE_ADMIN,
    SCOPE_APP,
    UPLOAD_FOR_DOCUMENT,
    UPLOAD_FOR_VIDEO,
    decode_access_claims,
    decode_upload_ticket,
)
from app.services import user_service
from app.utils.phone import InvalidMobileNumber, normalize_mobile

logger = logging.getLogger("nutriblend.auth")

bearer_scheme = HTTPBearer(auto_error=False)


async def _authenticate(creds: Optional[HTTPAuthorizationCredentials]) -> tuple[dict, str]:
    """Resolve a Bearer access token to (user document, session scope)."""
    if creds is None:
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.UNAUTHORIZED,
            "Authentication required.",
        )

    claims = decode_access_claims(creds.credentials)
    if not claims:
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.INVALID_TOKEN,
            "Your session has expired. Please sign in again.",
        )
    mobile_number, scope = claims

    user = await user_service.get_by_mobile(mobile_number)
    if not user:
        raise AccountGone()

    user_service.ensure_usable(user)
    return user, scope


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """The signed-in user, for app endpoints. Admin (dashboard) sessions are refused."""
    user, scope = await _authenticate(creds)
    if scope != SCOPE_APP:
        raise APIError(
            status.HTTP_403_FORBIDDEN,
            ErrorCode.WRONG_SESSION_TYPE,
            "Dashboard sessions cannot be used in the app. Please sign in through the app.",
            {"required_session": SCOPE_APP},
        )
    return user


@lru_cache(maxsize=1)
def admin_mobile_numbers() -> frozenset[str]:
    """ADMIN_MOBILE_NUMBERS (the optional extra lock), normalised. Invalid
    entries are skipped with a warning. Empty = no extra lock."""
    numbers: set[str] = set()
    for raw in settings.ADMIN_MOBILE_NUMBERS.split(","):
        if not raw.strip():
            continue
        try:
            numbers.add(normalize_mobile(raw))
        except InvalidMobileNumber:
            logger.warning("Ignoring invalid entry in ADMIN_MOBILE_NUMBERS: %r", raw.strip())
    return frozenset(numbers)


def is_admin(user: dict) -> bool:
    """role "admin", and — when ADMIN_MOBILE_NUMBERS is set — a listed number."""
    if not user_service.is_admin_account(user):
        return False
    allowed = admin_mobile_numbers()
    return not allowed or user.get("mobile_number") in allowed


async def require_admin(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """
    For /admin endpoints: an admin account (see is_admin) signed in through
    /admin/auth/signin. An admin's *app* session is refused, so a token taken
    from their phone cannot reach admin data.
    """
    user, scope = await _authenticate(creds)
    if scope != SCOPE_ADMIN:
        raise APIError(
            status.HTTP_403_FORBIDDEN,
            ErrorCode.WRONG_SESSION_TYPE,
            "Admin endpoints need a dashboard session. Sign in via /admin/auth/signin.",
            {"required_session": SCOPE_ADMIN},
        )
    if not is_admin(user):
        raise AdminOnly()
    return user


async def _ticket_or_admin(video_id, creds, ticket, purpose) -> None:
    if ticket:
        if decode_upload_ticket(ticket, purpose) != video_id:
            raise APIError(
                status.HTTP_401_UNAUTHORIZED,
                ErrorCode.INVALID_TOKEN,
                "This upload link has expired or is for another video. Start the upload again from the dashboard.",
            )
        return
    await require_admin(creds)


async def require_upload_access(
    video_id: str,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    x_upload_ticket: Optional[str] = Header(default=None, alias="X-Upload-Ticket"),
) -> None:
    """Video-file upload endpoints of one video: an admin session, or an upload
    ticket issued for exactly this video (the dashboard's in-browser upload box)."""
    await _ticket_or_admin(video_id, creds, x_upload_ticket, UPLOAD_FOR_VIDEO)


async def require_document_upload_access(
    video_id: str,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    x_upload_ticket: Optional[str] = Header(default=None, alias="X-Upload-Ticket"),
) -> None:
    """Document upload endpoints of one video: an admin session, or a
    document upload ticket for exactly this video."""
    await _ticket_or_admin(video_id, creds, x_upload_ticket, UPLOAD_FOR_DOCUMENT)


def require_admin_create_enabled() -> None:
    """Guards POST /admin/admins: 403 ADMIN_CREATE_DISABLED when ADMIN_CREATE_ENABLED is false."""
    if not settings.ADMIN_CREATE_ENABLED:
        raise AdminCreateDisabled()


def client_ip(request: Request) -> str:
    """
    The caller's IP, used for per-IP rate limits.

    Behind a proxy the socket address is the proxy's, so the real client is in
    a header: CF-Connecting-IP (set by Cloudflare, which overwrites any value a
    client sends) or else X-Forwarded-For. Headers are trivially forged when the
    server is reachable directly, so we only read them when TRUST_PROXY_HEADERS
    is on — which should be true only when ALL traffic arrives through the proxy
    (production: Cloudflare Tunnel, app bound to 127.0.0.1). Getting this
    backwards either throttles every user as one IP, or lets anyone bypass
    per-IP limits by sending a header.
    """
    if settings.TRUST_PROXY_HEADERS:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def public_base_url(request: Request) -> str:
    """Base URL for links handed to clients (media links): PUBLIC_BASE_URL, or
    the URL this request came in on."""
    return (settings.PUBLIC_BASE_URL or str(request.base_url)).rstrip("/")
