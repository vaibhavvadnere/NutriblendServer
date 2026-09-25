package com.nutriblend.api

import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.PATCH
import retrofit2.http.POST
import retrofit2.http.Path

interface NutriblendApi {

    // ── Sign up ──────────────────────────────────────────────────────────────
    /** Creates a pending account and sends an OTP. 409 if already registered. */
    @POST("${ApiConfig.API_PREFIX}/auth/signup")
    suspend fun signup(@Body body: SignupRequest): Response<OtpSentResponse>

    // ── Sign in ──────────────────────────────────────────────────────────────
    /** Sends an OTP to an existing account. 404 if there is no account. */
    @POST("${ApiConfig.API_PREFIX}/auth/signin")
    suspend fun signin(@Body body: MobileRequest): Response<OtpSentResponse>

    @POST("${ApiConfig.API_PREFIX}/auth/resend-otp")
    suspend fun resendOtp(@Body body: MobileRequest): Response<OtpSentResponse>

    /** Used by BOTH flows. Returns the token pair. */
    @POST("${ApiConfig.API_PREFIX}/auth/verify-otp")
    suspend fun verifyOtp(@Body body: VerifyOtpRequest): Response<TokenResponse>

    // ── Session ──────────────────────────────────────────────────────────────
    @POST("${ApiConfig.API_PREFIX}/auth/logout")
    suspend fun logout(@Body body: RefreshRequest): Response<MessageResponse>

    @POST("${ApiConfig.API_PREFIX}/auth/logout-all")
    suspend fun logoutAll(): Response<MessageResponse>

    // ── Profile ──────────────────────────────────────────────────────────────
    @GET("${ApiConfig.API_PREFIX}/users/me")
    suspend fun me(): Response<User>

    @PATCH("${ApiConfig.API_PREFIX}/users/me")
    suspend fun updateMe(@Body body: UpdateProfileRequest): Response<User>

    @GET("${ApiConfig.API_PREFIX}/users/{id}")
    suspend fun user(@Path("id") id: String): Response<User>
}

/**
 * Refresh lives on its own interface, called by a **separate** OkHttp client
 * that has no Authenticator attached. If refresh went through the authenticated
 * client, a 401 from refresh would trigger the Authenticator, which would call
 * refresh, which would 401... forever.
 */
interface RefreshApi {
    @POST("${ApiConfig.API_PREFIX}/auth/refresh")
    fun refresh(@Body body: RefreshRequest): retrofit2.Call<AccessTokenResponse>
}
