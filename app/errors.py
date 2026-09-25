"""
errors.py — One error shape for the whole API.

Every failure, whatever causes it, reaches the client as:

    HTTP <status>
    {
      "error": {
        "code": "ACCOUNT_EXISTS",
        "message": "An account with this mobile number already exists.",
        "details": { ... }            # optional, omitted when empty
      }
    }

The **code** is the part the mobile app should branch on. Messages are for
humans and will be reworded (and eventually translated); codes are a contract
and must not change once the app ships.
"""

from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode:
    """Every code the API can return. Keep this list and the app in sync."""

    # Request validation
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_MOBILE_NUMBER = "INVALID_MOBILE_NUMBER"

    # Account lifecycle
    ACCOUNT_EXISTS = "ACCOUNT_EXISTS"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ACCOUNT_BLOCKED = "ACCOUNT_BLOCKED"
    EMAIL_IN_USE = "EMAIL_IN_USE"

    # OTP
    OTP_NOT_REQUESTED = "OTP_NOT_REQUESTED"
    OTP_EXPIRED = "OTP_EXPIRED"
    OTP_INCORRECT = "OTP_INCORRECT"
    OTP_ATTEMPTS_EXCEEDED = "OTP_ATTEMPTS_EXCEEDED"
    OTP_SEND_FAILED = "OTP_SEND_FAILED"

    # Tokens / auth
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_TOKEN = "INVALID_TOKEN"
    REFRESH_TOKEN_INVALID = "REFRESH_TOKEN_INVALID"
    REFRESH_TOKEN_REUSED = "REFRESH_TOKEN_REUSED"

    # Throttling
    RATE_LIMITED = "RATE_LIMITED"

    # Catch-alls
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class APIError(Exception):
    """Raise this anywhere in the app instead of HTTPException."""

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
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}


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
