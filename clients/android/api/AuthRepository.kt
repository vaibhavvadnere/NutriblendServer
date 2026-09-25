package com.nutriblend.api

import com.squareup.moshi.Moshi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import retrofit2.Response
import java.io.IOException

/**
 * What your ViewModels call. Every method returns Kotlin's [Result]:
 *
 *     when (val r = auth.signUp("9876543210", "Boss")) {
 *         is Result.Success -> ...
 *         is Result.Failure -> (r.exceptionOrNull() as ApiException).code
 *     }
 *
 * Failures are always [ApiException], so you can switch on `code` and never have
 * to parse an error body yourself.
 */
class AuthRepository(
    private val api: NutriblendApi,
    private val tokenStore: TokenStore,
    moshi: Moshi,
) {

    private val errorAdapter = moshi.adapter(ErrorEnvelope::class.java)

    val isSignedIn: Boolean get() = tokenStore.isSignedIn

    // ── Sign up ──────────────────────────────────────────────────────────────

    /**
     * Step 1 of registration. Creates a pending account and sends an OTP.
     *
     * Failure codes worth handling:
     *   ACCOUNT_EXISTS        -> navigate to the sign-in screen
     *   EMAIL_IN_USE          -> highlight the email field
     *   INVALID_MOBILE_NUMBER -> highlight the number field
     *   RATE_LIMITED          -> show a countdown (see retryAfterSeconds)
     */
    suspend fun signUp(mobile: String, name: String, email: String? = null) =
        call { api.signup(SignupRequest(mobile, name, email)) }

    // ── Sign in ──────────────────────────────────────────────────────────────

    /**
     * Step 1 of login. Sends an OTP to an existing account.
     *
     *   ACCOUNT_NOT_FOUND -> navigate to the sign-up screen
     *   ACCOUNT_BLOCKED   -> show "contact support"
     */
    suspend fun signIn(mobile: String) = call { api.signin(MobileRequest(mobile)) }

    /** The "Resend OTP" button. Same 60s cooldown as everything else. */
    suspend fun resendOtp(mobile: String) = call { api.resendOtp(MobileRequest(mobile)) }

    // ── Verify (used by both flows) ──────────────────────────────────────────

    /**
     * Step 2 of both flows. On success the tokens are stored for you, so the
     * user is signed in by the time this returns.
     *
     *   OTP_INCORRECT         -> show attemptsRemaining
     *   OTP_EXPIRED           -> offer Resend
     *   OTP_ATTEMPTS_EXCEEDED -> force a Resend
     */
    suspend fun verifyOtp(mobile: String, otp: String): Result<TokenResponse> =
        call { api.verifyOtp(VerifyOtpRequest(mobile, otp)) }
            .onSuccess { tokenStore.save(it.accessToken, it.refreshToken) }

    // ── Session ──────────────────────────────────────────────────────────────

    /**
     * Signs out on this device.
     *
     * Local tokens are cleared even if the network call fails — the user tapped
     * "log out" and must end up logged out. The server-side token expires on its
     * own, so the worst case is a revoked-late session, not a stuck user.
     */
    suspend fun logout(): Result<Unit> {
        val refresh = tokenStore.refreshToken
        return try {
            if (refresh != null) call { api.logout(RefreshRequest(refresh)) }
            Result.success(Unit)
        } catch (e: Exception) {
            Result.success(Unit)
        } finally {
            tokenStore.clear()
        }
    }

    suspend fun logoutEverywhere(): Result<MessageResponse> =
        call { api.logoutAll() }.also { tokenStore.clear() }

    // ── Profile ──────────────────────────────────────────────────────────────

    suspend fun me(): Result<User> = call { api.me() }

    suspend fun updateProfile(name: String? = null, email: String? = null): Result<User> =
        call { api.updateMe(UpdateProfileRequest(name, email)) }

    // ── Plumbing ─────────────────────────────────────────────────────────────

    private suspend fun <T> call(block: suspend () -> Response<T>): Result<T> =
        withContext(Dispatchers.IO) {
            try {
                val response = block()
                val body = response.body()
                if (response.isSuccessful && body != null) {
                    Result.success(body)
                } else {
                    Result.failure(response.toApiException())
                }
            } catch (e: IOException) {
                Result.failure(
                    ApiException(
                        ErrorCode.NETWORK_ERROR,
                        "Can't reach the server. Check your connection and try again.",
                    )
                )
            } catch (e: Exception) {
                Result.failure(
                    ApiException(ErrorCode.INTERNAL_ERROR, "Something went wrong. Please try again.")
                )
            }
        }

    private fun <T> Response<T>.toApiException(): ApiException {
        val raw = errorBody()?.string()

        val parsed = raw?.let {
            runCatching { errorAdapter.fromJson(it)?.error }.getOrNull()
        }

        if (parsed != null) {
            // The server sends Retry-After as a header too; prefer the body value
            // but fall back to the header if the body somehow lacks it.
            val details = parsed.details?.toMutableMap() ?: mutableMapOf()
            if (!details.containsKey("retry_after_seconds")) {
                headers()["Retry-After"]?.toIntOrNull()?.let {
                    details["retry_after_seconds"] = it
                }
            }
            return ApiException(parsed.code, parsed.message, code(), details)
        }

        // Shouldn't happen — every server error is enveloped — but a proxy or
        // load balancer can return its own HTML error page.
        return ApiException(
            code = if (code() == 401) ErrorCode.UNAUTHORIZED else ErrorCode.INTERNAL_ERROR,
            message = "Request failed (HTTP ${code()}).",
            httpStatus = code(),
        )
    }
}
