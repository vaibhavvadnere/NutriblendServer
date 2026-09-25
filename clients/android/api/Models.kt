package com.nutriblend.api

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

// ── Requests ─────────────────────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class SignupRequest(
    @Json(name = "mobile_number") val mobileNumber: String,
    val name: String,
    val email: String? = null,
)

@JsonClass(generateAdapter = true)
data class MobileRequest(
    @Json(name = "mobile_number") val mobileNumber: String,
)

@JsonClass(generateAdapter = true)
data class VerifyOtpRequest(
    @Json(name = "mobile_number") val mobileNumber: String,
    val otp: String,
)

@JsonClass(generateAdapter = true)
data class RefreshRequest(
    @Json(name = "refresh_token") val refreshToken: String,
)

// ── Responses ────────────────────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class OtpSentResponse(
    val message: String,
    /** Masked, e.g. "98XXXXXX10" — safe to show on screen. */
    @Json(name = "mobile_number") val maskedMobile: String,
    @Json(name = "expires_in_minutes") val expiresInMinutes: Int,
    /** Seconds until "Resend OTP" should become tappable again. */
    @Json(name = "resend_available_in_seconds") val resendAvailableInSeconds: Int,
    /** True when this is a brand-new account (signup, or an unverified signin). */
    @Json(name = "is_new_account") val isNewAccount: Boolean,
    /** Only present while the server runs with ENV=development. Null in production. */
    @Json(name = "dev_otp") val devOtp: String? = null,
)

@JsonClass(generateAdapter = true)
data class TokenResponse(
    @Json(name = "access_token") val accessToken: String,
    @Json(name = "refresh_token") val refreshToken: String,
    @Json(name = "token_type") val tokenType: String = "bearer",
    /** Access-token lifetime in seconds. */
    @Json(name = "expires_in") val expiresIn: Int,
    val user: User,
)

@JsonClass(generateAdapter = true)
data class AccessTokenResponse(
    @Json(name = "access_token") val accessToken: String,
    @Json(name = "refresh_token") val refreshToken: String,
    @Json(name = "token_type") val tokenType: String = "bearer",
    @Json(name = "expires_in") val expiresIn: Int,
)

@JsonClass(generateAdapter = true)
data class User(
    val id: String,
    @Json(name = "mobile_number") val mobileNumber: String,
    val name: String,
    val email: String? = null,
    /** "pending" | "active" | "blocked" */
    val status: String,
    @Json(name = "is_verified") val isVerified: Boolean,
    @Json(name = "created_at") val createdAt: String,
    @Json(name = "verified_at") val verifiedAt: String? = null,
    @Json(name = "last_login_at") val lastLoginAt: String? = null,
)

@JsonClass(generateAdapter = true)
data class MessageResponse(val message: String)

@JsonClass(generateAdapter = true)
data class UpdateProfileRequest(
    val name: String? = null,
    val email: String? = null,
)
