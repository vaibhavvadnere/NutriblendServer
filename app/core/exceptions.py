"""
core/exceptions.py — Business (domain) errors.

Services raise these when a business rule says "no". They deliberately know
nothing about HTTP — no status codes, no headers — so the business logic can
be reused and reasoned about without FastAPI in the picture.

core/errors.py translates each one into the API's error response, so the
mapping from "what went wrong" to "which HTTP status" lives in exactly one place.
"""

from typing import Any, Optional


class ErrorCode:
    """Every code the API can return. Keep this list and the app in sync.

    Codes are a contract with the mobile app and must not change once shipped;
    messages are for humans and may be reworded (and eventually translated)."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_MOBILE_NUMBER = "INVALID_MOBILE_NUMBER"
    ACCOUNT_EXISTS = "ACCOUNT_EXISTS"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ACCOUNT_BLOCKED = "ACCOUNT_BLOCKED"
    EMAIL_IN_USE = "EMAIL_IN_USE"
    OTP_NOT_REQUESTED = "OTP_NOT_REQUESTED"
    OTP_EXPIRED = "OTP_EXPIRED"
    OTP_INCORRECT = "OTP_INCORRECT"
    OTP_ATTEMPTS_EXCEEDED = "OTP_ATTEMPTS_EXCEEDED"
    OTP_SEND_FAILED = "OTP_SEND_FAILED"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_TOKEN = "INVALID_TOKEN"
    REFRESH_TOKEN_INVALID = "REFRESH_TOKEN_INVALID"
    REFRESH_TOKEN_REUSED = "REFRESH_TOKEN_REUSED"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DomainError(Exception):
    """Base class. Subclasses set a default `code` and `message`; a caller may
    override the message where the same rule needs different wording."""

    code: str = ErrorCode.INTERNAL_ERROR
    message: str = "Something went wrong on our side. Please try again."

    def __init__(self, message: Optional[str] = None, details: Optional[dict[str, Any]] = None):
        self.message = message or type(self).message
        self.details = details or {}
        super().__init__(self.message)


# ── Accounts ─────────────────────────────────────────────────────────────────

class AccountNotFound(DomainError):
    """No account for this mobile number / id (the caller should sign up)."""
    code = ErrorCode.ACCOUNT_NOT_FOUND
    message = "No account found with this mobile number. Please sign up first."


class AccountGone(DomainError):
    """A token was valid, but the account it belongs to has since been deleted."""
    code = ErrorCode.ACCOUNT_NOT_FOUND
    message = "This account no longer exists."


class AccountExists(DomainError):
    code = ErrorCode.ACCOUNT_EXISTS
    message = "An account with this mobile number already exists. Please sign in instead."


class AccountBlocked(DomainError):
    """Blocked accounts get a flat refusal with no explanation — telling someone
    why they were blocked just teaches them how to evade it."""
    code = ErrorCode.ACCOUNT_BLOCKED
    message = "This account is not available. Please contact support."


class EmailInUse(DomainError):
    code = ErrorCode.EMAIL_IN_USE
    message = "This email address is already linked to another account."


class InvalidUserId(DomainError):
    code = ErrorCode.VALIDATION_ERROR
    message = "Invalid user id"


# ── OTP ──────────────────────────────────────────────────────────────────────

class OtpNotRequested(DomainError):
    code = ErrorCode.OTP_NOT_REQUESTED
    message = "No OTP request found. Please request a new OTP."


class OtpExpired(DomainError):
    code = ErrorCode.OTP_EXPIRED
    message = "This OTP has expired. Please request a new one."


class OtpIncorrect(DomainError):
    code = ErrorCode.OTP_INCORRECT
    message = "That OTP is incorrect. Please check and try again."

    def __init__(self, attempts_remaining: int):
        super().__init__(details={"attempts_remaining": attempts_remaining})
        self.attempts_remaining = attempts_remaining


class OtpAttemptsExceeded(DomainError):
    code = ErrorCode.OTP_ATTEMPTS_EXCEEDED
    message = "Too many incorrect attempts. Please request a new OTP."


class OtpSendFailed(DomainError):
    code = ErrorCode.OTP_SEND_FAILED
    message = "We couldn't send the OTP right now. Please try again in a moment."


# ── Sessions ─────────────────────────────────────────────────────────────────

class RefreshTokenInvalid(DomainError):
    """Unknown or expired refresh token."""
    code = ErrorCode.REFRESH_TOKEN_INVALID
    message = "Invalid refresh token"


class RefreshTokenReused(DomainError):
    """An already-rotated token was presented again — treated as theft."""
    code = ErrorCode.REFRESH_TOKEN_REUSED
    message = (
        "This refresh token was already used. For your security all sessions "
        "from this login have been revoked — please log in again."
    )


# ── Abuse protection ─────────────────────────────────────────────────────────

class RateLimited(DomainError):
    code = ErrorCode.RATE_LIMITED
    message = "Too many requests. Please try again later."

    def __init__(self, retry_after: int, message: Optional[str] = None, scope: str = ""):
        super().__init__(message, details={"retry_after_seconds": retry_after})
        self.retry_after = retry_after
        self.scope = scope
