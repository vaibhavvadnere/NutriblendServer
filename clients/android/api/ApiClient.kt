package com.nutriblend.api

import android.content.Context
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import java.util.concurrent.TimeUnit

/**
 * Builds and holds the API client. Create one instance per app (in your
 * Application class, or as a singleton in your DI graph) — never one per screen.
 */
class ApiClient(
    context: Context,
    private val onSessionExpired: () -> Unit = {},
    enableLogging: Boolean = true,
) {

    val tokenStore = TokenStore(context)

    private val moshi: Moshi = Moshi.Builder()
        .add(KotlinJsonAdapterFactory())
        .build()

    private val logging = HttpLoggingInterceptor().apply {
        // NEVER BODY in a release build — it prints tokens and OTPs to logcat.
        level = if (enableLogging) HttpLoggingInterceptor.Level.BODY
                else HttpLoggingInterceptor.Level.NONE
    }

    /** Bare client used only for /auth/refresh — no auth, no authenticator. */
    private val refreshClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .addInterceptor(logging)
        .build()

    private val refreshApi: RefreshApi = Retrofit.Builder()
        .baseUrl(ApiConfig.BASE_URL)
        .client(refreshClient)
        .addConverterFactory(MoshiConverterFactory.create(moshi))
        .build()
        .create(RefreshApi::class.java)

    private val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .addInterceptor(AuthInterceptor(tokenStore))
        .authenticator(AuthAuthenticator(tokenStore, refreshApi, onSessionExpired))
        .addInterceptor(logging)
        .build()

    val api: NutriblendApi = Retrofit.Builder()
        .baseUrl(ApiConfig.BASE_URL)
        .client(httpClient)
        .addConverterFactory(MoshiConverterFactory.create(moshi))
        .build()
        .create(NutriblendApi::class.java)

    val auth: AuthRepository by lazy { AuthRepository(api, tokenStore, moshi) }
}
