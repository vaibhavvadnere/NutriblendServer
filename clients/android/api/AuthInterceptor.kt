package com.nutriblend.api

import okhttp3.Interceptor
import okhttp3.Response

/**
 * Attaches `Authorization: Bearer <access token>` to requests that need it.
 *
 * Public endpoints are skipped deliberately. Sending a stale bearer token to
 * signup/signin would be harmless but pointless; sending one to /auth/refresh
 * is actively confusing when debugging.
 */
class AuthInterceptor(private val tokenStore: TokenStore) : Interceptor {

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val path = request.url.encodedPath

        if (PUBLIC_PATHS.any { path.endsWith(it) }) {
            return chain.proceed(request)
        }

        val token = tokenStore.accessToken ?: return chain.proceed(request)

        return chain.proceed(
            request.newBuilder()
                .header("Authorization", "Bearer $token")
                .build()
        )
    }

    private companion object {
        val PUBLIC_PATHS = listOf(
            "/auth/signup",
            "/auth/signin",
            "/auth/resend-otp",
            "/auth/verify-otp",
            "/auth/refresh",
            "/auth/logout",     // takes the refresh token in the body, not a header
            "/api/health",
        )
    }
}
