"""
core/deps.py — Shared FastAPI dependencies and request helpers.

This is HTTP-layer code: it reads headers and the socket address, then hands
plain values to the services.
"""

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.errors import APIError, ErrorCode
from app.core.exceptions import AccountGone
from app.core.security import decode_access_token
from app.services import user_service

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """Resolve the Bearer access token to the signed-in user's document."""
    if creds is None:
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.UNAUTHORIZED,
            "Authentication required.",
        )

    mobile_number = decode_access_token(creds.credentials)
    if not mobile_number:
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.INVALID_TOKEN,
            "Your session has expired. Please sign in again.",
        )

    user = await user_service.get_by_mobile(mobile_number)
    if not user:
        raise AccountGone()

    user_service.ensure_not_blocked(user)
    return user


def client_ip(request: Request) -> str:
    """
    The caller's IP, used for per-IP rate limits.

    Behind a proxy the socket address is the proxy's, so the real client is in
    X-Forwarded-For. That header is trivially forged, so we only read it when
    TRUST_PROXY_HEADERS is on — which should be true only when the app really
    does sit behind a proxy you control. Getting this backwards either throttles
    every user as one IP, or lets anyone bypass per-IP limits by sending a header.
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
