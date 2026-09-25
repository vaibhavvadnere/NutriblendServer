package com.nutriblend.api

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

/**
 * The server returns one error shape everywhere:
 *
 *   { "error": { "code": "ACCOUNT_EXISTS", "message": "...", "details": {...} } }
 *
 * Branch on [code], never on [message] — messages get reworded and translated,
 * codes are a contract.
 */
@JsonClass(generateAdapter = true)
data class ErrorEnvelope(val error: ErrorBody)

@JsonClass(generateAdapter = true)
data class ErrorBody(
    val code: String,
    val message: String,
    val details: Map<String, Any?>? = null,
)

object ErrorCode {
    // Validation
    const val VALIDATION_ERROR = "VALIDATION_ERROR"
    const val INVALID_MOBILE_NUMBER = "INVALID_MOBILE_NUMBER"

    // Account lifecycle
    const val ACCOUNT_EXISTS = "ACCOUNT_EXISTS"
    const val ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    const val ACCOUNT_BLOCKED = "ACCOUNT_BLOCKED"
    const val EMAIL_IN_USE = "EMAIL_IN_USE"

    // OTP
    const val OTP_NOT_REQUESTED = "OTP_NOT_REQUESTED"
    const val OTP_EXPIRED = "OTP_EXPIRED"
    const val OTP_INCORRECT = "OTP_INCORRECT"
    const val OTP_ATTEMPTS_EXCEEDED = "OTP_ATTEMPTS_EXCEEDED"
    const val OTP_SEND_FAILED = "OTP_SEND_FAILED"

    // Auth
    const val UNAUTHORIZED = "UNAUTHORIZED"
    const val INVALID_TOKEN = "INVALID_TOKEN"
    const val REFRESH_TOKEN_INVALID = "REFRESH_TOKEN_INVALID"
    const val REFRESH_TOKEN_REUSED = "REFRESH_TOKEN_REUSED"

    // Throttling
    const val RATE_LIMITED = "RATE_LIMITED"

    // Catch-alls
    const val NOT_FOUND = "NOT_FOUND"
    const val INTERNAL_ERROR = "INTERNAL_ERROR"

    /** Client-side only: no network, timeout, DNS failure. */
    const val NETWORK_ERROR = "NETWORK_ERROR"
}

/**
 * Every failure your UI will see. [code] is what you switch on.
 */
data class ApiException(
    val code: String,
    override val message: String,
    val httpStatus: Int = 0,
    val details: Map<String, Any?>? = null,
) : Exception(message) {

    /** For RATE_LIMITED: seconds until the user may try again. */
    val retryAfterSeconds: Int?
        get() = (details?.get("retry_after_seconds") as? Number)?.toInt()

    /** For OTP_INCORRECT: how many guesses are left before lockout. */
    val attemptsRemaining: Int?
        get() = (details?.get("attempts_remaining") as? Number)?.toInt()

    /**
     * For VALIDATION_ERROR: field name -> message, so you can highlight the
     * offending input rather than showing a generic toast.
     */
    @Suppress("UNCHECKED_CAST")
    val fieldErrors: Map<String, String>
        get() = (details?.get("fields") as? Map<String, String>) ?: emptyMap()

    /** True when the session is gone for good and the user must sign in again. */
    val isSessionEnded: Boolean
        get() = code == ErrorCode.REFRESH_TOKEN_INVALID ||
                code == ErrorCode.REFRESH_TOKEN_REUSED ||
                code == ErrorCode.ACCOUNT_BLOCKED
}
