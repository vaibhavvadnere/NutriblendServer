package com.nutriblend.api

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Where the tokens live.
 *
 * EncryptedSharedPreferences, not plain SharedPreferences: a refresh token is a
 * 30-day login. On a rooted or compromised device, plain prefs are a text file
 * anyone can read.
 *
 * Access and refresh tokens are always written **together** in one commit. They
 * are a matched pair — the server rotates the refresh token on every use, so a
 * half-written update (new access, old refresh) would leave you holding a token
 * the server has already revoked, and the next refresh would look like theft and
 * end the session.
 */
class TokenStore(context: Context) {

    private val prefs: SharedPreferences = EncryptedSharedPreferences.create(
        context.applicationContext,
        "nutriblend_auth",
        MasterKey.Builder(context.applicationContext)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    val accessToken: String? get() = prefs.getString(KEY_ACCESS, null)
    val refreshToken: String? get() = prefs.getString(KEY_REFRESH, null)
    val isSignedIn: Boolean get() = refreshToken != null

    @Synchronized
    fun save(access: String, refresh: String) {
        prefs.edit()
            .putString(KEY_ACCESS, access)
            .putString(KEY_REFRESH, refresh)
            .commit()   // commit, not apply: the OkHttp Authenticator runs on a
                        // background thread and the very next request must see
                        // the new value. apply() is asynchronous.
    }

    @Synchronized
    fun clear() {
        prefs.edit().clear().commit()
    }

    private companion object {
        const val KEY_ACCESS = "access_token"
        const val KEY_REFRESH = "refresh_token"
    }
}
