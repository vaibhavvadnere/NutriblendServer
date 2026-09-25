"""
deps.py — Shared FastAPI dependencies (e.g. "get current logged-in user").
"""

from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.database import users_collection
from app.errors import APIError, ErrorCode
from app.services.users import ensure_not_blocked
from app.utils.security import decode_access_token

# auto_error=False so a missing header produces our own coded 401 rather than
# FastAPI's bare 403.
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
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

    user = await users_collection.find_one({"mobile_number": mobile_number})
    if not user:
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.ACCOUNT_NOT_FOUND,
            "This account no longer exists.",
        )
    # A token issued before an account was blocked must stop working immediately.
    ensure_not_blocked(user)
    return user
