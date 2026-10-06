"""
core/errors.py — One error shape for the whole API.

Every failure, whatever causes it, reaches the client as:

    HTTP <status>
    {
      "success": false,
      "message": "An account with this mobile number already exists.",
      "error": {
        "code": "ACCOUNT_EXISTS",
        "details": { ... }            # optional, omitted when empty
      }
    }

The **code** is the part the mobile app should branch on. Messages are for
humans and will be reworded (and eventually translated); codes are a contract
and must not change once the app ships.

Services raise DomainError subclasses (app/core/exceptions.py), which carry no
HTTP information; _DOMAIN_STATUS below decides the status code for each.
"""

from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import (
    AccountBlocked,
    AccountExists,
    AccountGone,
    AccountNotFound,
    DomainError,
    EmailInUse,
    ErrorCode,
    InvalidUserId,
    OtpAttemptsExceeded,
    OtpExpired,
    OtpIncorrect,
    OtpNotRequested,
    OtpSendFailed,
    RateLimited,
    RefreshTokenInvalid,
    RefreshTokenReused,
)

__all__ = ["APIError", "ErrorCode", "error_body", "register_error_handlers", "status_for"]


# The one place a business error becomes an HTTP status.
_DOMAIN_STATUS: dict[type[DomainError], int] = {
    AccountNotFound: status.HTTP_404_NOT_FOUND,
    AccountGone: status.HTTP_401_UNAUTHORIZED,
    AccountExists: status.HTTP_409_CONFLICT,
    AccountBlocked: status.HTTP_403_FORBIDDEN,
    EmailInUse: status.HTTP_409_CONFLICT,
    InvalidUserId: status.HTTP_400_BAD_REQUEST,
    OtpNotRequested: status.HTTP_400_BAD_REQUEST,
    OtpExpired: status.HTTP_400_BAD_REQUEST,
    OtpIncorrect: status.HTTP_400_BAD_REQUEST,
    OtpAttemptsExceeded: status.HTTP_429_TOO_MANY_REQUESTS,
    OtpSendFailed: status.HTTP_502_BAD_GATEWAY,
    RefreshTokenInvalid: status.HTTP_401_UNAUTHORIZED,
    RefreshTokenReused: status.HTTP_401_UNAUTHORIZED,
    RateLimited: status.HTTP_429_TOO_MANY_REQUESTS,
}


def status_for(exc: DomainError) -> int:
    """HTTP status for a domain error (walks the class hierarchy, so a subclass
    inherits its parent's status unless it has its own entry)."""
    for cls in type(exc).__mro__:
        if cls in _DOMAIN_STATUS:
            return _DOMAIN_STATUS[cls]
    return status.HTTP_500_INTERNAL_SERVER_ERROR


class APIError(Exception):
    """HTTP-layer error, for failures that only make sense in HTTP terms (a
    missing or malformed Authorization header). Business rules raise a
    DomainError from app.core.exceptions instead."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


def error_body(code: str, message: str, details: Optional[dict] = None) -> dict:
    """The failure envelope — see app/schemas/common.py for both shapes."""
    error: dict[str, Any] = {"code": code}
    if details:
        error["details"] = details
    return {"success": False, "message": message, "error": error}


# Map FastAPI's own status codes onto our vocabulary, for errors raised by the
# framework rather than by our code (404 on an unknown path, 405, etc.).
_STATUS_TO_CODE = {
    status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN: ErrorCode.UNAUTHORIZED,
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
            headers=exc.headers or None,
        )

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError):
        headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimited) else None
        return JSONResponse(
            status_code=status_for(exc),
            content=error_body(exc.code, exc.message, exc.details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # Flatten pydantic's errors into {field: message}, which is what a form
        # screen actually needs in order to highlight the offending input.
        fields: dict[str, str] = {}
        for err in exc.errors():
            location = [str(p) for p in err.get("loc", []) if p not in ("body", "query", "path")]
            field = ".".join(location) or "body"
            message = err.get("msg", "Invalid value")
            fields[field] = message.removeprefix("Value error, ")

        # A bad mobile number is common enough to deserve its own code.
        code = (
            ErrorCode.INVALID_MOBILE_NUMBER
            if set(fields) == {"mobile_number"}
            else ErrorCode.VALIDATION_ERROR
        )
        message = (
            next(iter(fields.values()))
            if len(fields) == 1
            else "Some of the values you entered are not valid."
        )
        return JSONResponse(
            status_code=422,  # Unprocessable Content
            content=error_body(code, message, {"fields": fields}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Never leak a stack trace or an internal message to the client.
        import logging

        logging.getLogger("nutriblend").exception("Unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body(
                ErrorCode.INTERNAL_ERROR,
                "Something went wrong on our side. Please try again.",
            ),
        )
