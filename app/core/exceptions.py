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
    ACCOUNT_DELETED = "ACCOUNT_DELETED"
    EMAIL_IN_USE = "EMAIL_IN_USE"
    OTP_NOT_REQUESTED = "OTP_NOT_REQUESTED"
    OTP_EXPIRED = "OTP_EXPIRED"
    OTP_INCORRECT = "OTP_INCORRECT"
    OTP_ATTEMPTS_EXCEEDED = "OTP_ATTEMPTS_EXCEEDED"
    OTP_SEND_FAILED = "OTP_SEND_FAILED"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    INVALID_STATUS_CHANGE = "INVALID_STATUS_CHANGE"
    ADMIN_CREATE_DISABLED = "ADMIN_CREATE_DISABLED"
    WRONG_SESSION_TYPE = "WRONG_SESSION_TYPE"
    VIDEO_NOT_FOUND = "VIDEO_NOT_FOUND"
    VIDEO_NOT_READY = "VIDEO_NOT_READY"
    DOCUMENT_NOT_READY = "DOCUMENT_NOT_READY"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    INVALID_MEDIA_FILE = "INVALID_MEDIA_FILE"
    UPLOAD_NOT_IN_PROGRESS = "UPLOAD_NOT_IN_PROGRESS"
    UPLOAD_INVALID_CHUNK = "UPLOAD_INVALID_CHUNK"
    UPLOAD_INCOMPLETE = "UPLOAD_INCOMPLETE"
    INSUFFICIENT_STORAGE = "INSUFFICIENT_STORAGE"
    MEDIA_LINK_INVALID = "MEDIA_LINK_INVALID"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    DOCUMENT_NOT_READY = "DOCUMENT_NOT_READY"
    DUPLICATE_VIDEO = "DUPLICATE_VIDEO"
    DUPLICATE_TITLE = "DUPLICATE_TITLE"
    VIDEO_OPTIMIZING = "VIDEO_OPTIMIZING"
    UPLOAD_CHECKSUM_MISMATCH = "UPLOAD_CHECKSUM_MISMATCH"
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


class AccountDeleted(DomainError):
    """The account was deleted by its owner. Kept distinct from
    ACCOUNT_NOT_FOUND so support can tell "never existed" from "deleted"."""
    code = ErrorCode.ACCOUNT_DELETED
    message = (
        "This account has been deleted. This mobile number cannot be registered "
        "again — please contact support if you need it restored."
    )


class CannotDeleteAdmin(DomainError):
    """Admin accounts are not deletable through the app's self-service flow —
    losing the only admin would lock everyone out of the dashboard."""
    code = ErrorCode.FORBIDDEN
    message = "Admin accounts cannot be deleted from the app. Contact support."


class EmailInUse(DomainError):
    code = ErrorCode.EMAIL_IN_USE
    message = "This email address is already linked to another account."


class InvalidUserId(DomainError):
    code = ErrorCode.VALIDATION_ERROR
    message = "Invalid user id"


class AdminOnly(DomainError):
    """Signed in, but this mobile number is not in ADMIN_MOBILE_NUMBERS."""
    code = ErrorCode.FORBIDDEN
    message = "This account does not have admin access."


class NotAnAdmin(DomainError):
    """/admin/auth/signin for a number that is not an admin account."""
    code = ErrorCode.FORBIDDEN
    message = "This mobile number is not registered as an admin."


class AdminCreateDisabled(DomainError):
    """ADMIN_CREATE_ENABLED is false, so POST /admin/admins is switched off."""
    code = ErrorCode.ADMIN_CREATE_DISABLED
    message = "Creating admin accounts is disabled on this server."


class InvalidStatusChange(DomainError):
    """An admin status change that the lifecycle does not allow."""
    code = ErrorCode.INVALID_STATUS_CHANGE
    message = "This status change is not allowed."


# ── Videos & media ──────────────────────────────────────────────────────────

class VideoNotFound(DomainError):
    code = ErrorCode.VIDEO_NOT_FOUND
    message = "Video not found."


class VideoNotReady(DomainError):
    """Publishing (or playing) a video whose file hasn't finished uploading."""
    code = ErrorCode.VIDEO_NOT_READY
    message = "This video's file hasn't finished uploading yet."


class DocumentNotReady(DomainError):
    """Publishing a video whose document is still being prepared (or failed to be)."""
    code = ErrorCode.DOCUMENT_NOT_READY
    message = "This video's document isn't ready yet."


class FileTooLarge(DomainError):
    code = ErrorCode.FILE_TOO_LARGE
    message = "The file is too large."


class UnsupportedMediaType(DomainError):
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE
    message = "This file type is not supported."


class InvalidMediaFile(DomainError):
    """The bytes don't look like the declared type (e.g. a renamed file)."""
    code = ErrorCode.INVALID_MEDIA_FILE
    message = "The file content doesn't match a supported format."


class UploadNotInProgress(DomainError):
    code = ErrorCode.UPLOAD_NOT_IN_PROGRESS
    message = "There is no upload in progress for this video."


class UploadInvalidChunk(DomainError):
    code = ErrorCode.UPLOAD_INVALID_CHUNK
    message = "Invalid upload chunk."


class UploadIncomplete(DomainError):
    code = ErrorCode.UPLOAD_INCOMPLETE
    message = "Some chunks are still missing."


class InsufficientStorage(DomainError):
    code = ErrorCode.INSUFFICIENT_STORAGE
    message = "Not enough storage space on the server for this file."


class DuplicateVideo(DomainError):
    """The same file (same SHA-256) is already a video. `details` names it."""
    code = ErrorCode.DUPLICATE_VIDEO
    message = "This exact video file has already been uploaded."


class VideoOptimizing(DomainError):
    """The video is still being optimized (or its optimization failed), so it can't be published yet."""
    code = ErrorCode.VIDEO_OPTIMIZING
    message = "This video is still being optimized."


class DuplicateTitle(DomainError):
    """Another video already has this title. `details.videos` names them."""
    code = ErrorCode.DUPLICATE_TITLE
    message = "A video with this title already exists."


class UploadChecksumMismatch(DomainError):
    """Bytes don't match the fingerprint the client sent: corrupted on the way."""
    code = ErrorCode.UPLOAD_CHECKSUM_MISMATCH
    message = "The data was corrupted on the way. Please send it again."


class DocumentNotFound(DomainError):
    code = ErrorCode.DOCUMENT_NOT_FOUND
    message = "This video has no document."


class DocumentNotReady(DomainError):
    """The document is still being converted, or conversion failed."""
    code = ErrorCode.DOCUMENT_NOT_READY
    message = "The document isn't ready to view yet."


class MediaLinkInvalid(DomainError):
    code = ErrorCode.MEDIA_LINK_INVALID
    message = "This media link is invalid or has expired."


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
