"""
api/errors.py — Typed errors raised by the API client.

Pages catch these (usually via components.layout.api_errors) instead of
inspecting status codes. `code` is the server's stable error code.
"""

from __future__ import annotations

from typing import Any, Optional


class ApiError(Exception):
    """Any failure reported by the server, or a response we could not understand."""

    def __init__(
        self,
        message: str,
        code: str = "INTERNAL_ERROR",
        status_code: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}

    def __repr__(self) -> str:  # never includes tokens — messages come from the server
        return f"{type(self).__name__}(status={self.status_code}, code={self.code!r})"


class NetworkError(ApiError):
    """The server could not be reached (down, wrong URL, timeout)."""


class Unauthorized(ApiError):
    """401 — missing, invalid or expired credentials."""


class SessionExpired(Unauthorized):
    """401 that survived a refresh attempt. The user must sign in again."""


class Forbidden(ApiError):
    """403 — signed in, but not allowed (not an admin, or account blocked)."""


class NotFound(ApiError):
    """404."""


class ValidationFailed(ApiError):
    """422 — the server rejected the input. `fields` maps field -> message."""

    @property
    def fields(self) -> dict[str, str]:
        return dict(self.details.get("fields", {}))


class RateLimited(ApiError):
    """429 — too many requests. `retry_after` is in seconds when known."""

    def __init__(self, *args: Any, retry_after: Optional[int] = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


_BY_STATUS: dict[int, type[ApiError]] = {
    401: Unauthorized,
    403: Forbidden,
    404: NotFound,
    422: ValidationFailed,
    429: RateLimited,
}


def error_for_status(status_code: int) -> type[ApiError]:
    return _BY_STATUS.get(status_code, ApiError)
