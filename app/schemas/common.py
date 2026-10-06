"""
schemas/common.py — The response envelope every endpoint uses.

Success:
    HTTP 2xx
    {
      "success": true,
      "message": "OTP sent successfully",
      "data": { ... }                      # endpoint-specific payload ({} if none)
    }

Failure:
    HTTP 4xx / 5xx
    {
      "success": false,
      "message": "That OTP is incorrect. Please check and try again.",
      "error": {
        "code": "OTP_INCORRECT",           # stable — the app branches on this
        "details": { ... }                 # optional, omitted when empty
      }
    }

HTTP status codes stay meaningful (201, 400, 401, 404, 409, 429 ...); `success`
is a convenience for clients, not a replacement for the status.
"""

from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Success envelope. Use `ApiResponse[SomeData]` as a route's response_model."""
    success: bool = True
    message: str
    data: T


class EmptyData(BaseModel):
    """`data` for endpoints that have nothing to return — serialises as {}."""


class ErrorInfo(BaseModel):
    code: str
    details: Optional[dict[str, Any]] = None


class ErrorResponse(BaseModel):
    """Failure envelope (documented in OpenAPI; built by core/errors.py)."""
    success: bool = False
    message: str
    error: ErrorInfo


def ok(data: Any, message: str) -> ApiResponse:
    """Wrap a payload in the success envelope."""
    return ApiResponse(message=message, data=data)


#: Attach to routers so Swagger shows the error shape for every 4xx/5xx.
ERROR_RESPONSES: dict = {
    "4XX": {"model": ErrorResponse, "description": "Client error"},
    "5XX": {"model": ErrorResponse, "description": "Server error"},
}
