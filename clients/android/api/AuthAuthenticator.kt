package com.nutriblend.api

import okhttp3.Authenticator
import okhttp3.Request
import okhttp3.Response
import okhttp3.Route

/**
 * Renews the access token when the server answers 401, then replays the request.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * READ THIS BEFORE CHANGING ANYTHING HERE
 *
 * The server **rotates** refresh tokens: each call to /auth/refresh revokes the
 * token you sent and returns a new one. Sending an already-used refresh token is
 * treated as theft, and the server revokes the entire session family — the user
 * is silently logged out.
 *
 * That makes the obvious implementation wrong. If the app fires three requests
 * at once and all three get 401, three threads each read the same stored refresh
 * token and each call /auth/refresh. The first succeeds; the other two present a
 * token that has just been rotated away, the server sees replay, and the user is
 * kicked out for no reason.
 *
 * Two things prevent that here:
 *   1. `synchronized` — only one thread refreshes at a time.
 *   2. The stale-token check inside the lock — a thread that was queued behind
 *      the refresher notices the access token already changed and simply retries
 *      with the new one instead of refreshing again.
 *
 * If you ever see users being randomly signed out, this is the first place to look.
 * ─────────────────────────────────────────────────────────────────────────────
 */
class AuthAuthenticator(
    private val tokenStore: TokenStore,
    private val refreshApi: RefreshApi,
    /** Called when the session is unrecoverable — send the user to the login screen. */
    private val onSessionExpired: () -> Unit,
) : Authenticator {

    private val lock = Any()

    override fun authenticate(route: Route?, response: Response): Request? {
        // Give up rather than loop: if the replayed request also 401s, the new
        // token is not the problem.
        if (responseCount(response) >= 2) {
            signOut()
            return null
        }

        // The token this failed request actually used.
        val staleToken = response.request.header("Authorization")?.removePrefix("Bearer ")

        synchronized(lock) {
            val current = tokenStore.accessToken

            // Someone else refreshed while we waited on the lock. Just use theirs.
            if (current != null && current != staleToken) {
                return response.request.retryWith(current)
            }

            val refreshToken = tokenStore.refreshToken ?: run {
                signOut()
                return null
            }

            val body = try {
                val resp = refreshApi.refresh(RefreshRequest(refreshToken)).execute()
                if (!resp.isSuccessful) {
                    // 401 here means the refresh token is dead — expired, revoked,
                    // or already used. Nothing to recover; the user signs in again.
                    signOut()
                    return null
                }
                resp.body() ?: run { signOut(); return null }
            } catch (e: Exception) {
                // Network failure. Do NOT sign the user out — their tokens are
                // probably fine and the request should just fail this once.
                return null
            }

            // Store both halves together. The refresh token we just sent is now
            // dead, so losing the new one would end the session.
            tokenStore.save(body.accessToken, body.refreshToken)
            return response.request.retryWith(body.accessToken)
        }
    }

    private fun signOut() {
        tokenStore.clear()
        onSessionExpired()
    }

    private fun Request.retryWith(token: String): Request =
        newBuilder().header("Authorization", "Bearer $token").build()

    private fun responseCount(response: Response): Int {
        var count = 1
        var prior = response.priorResponse
        while (prior != null) {
            count++
            prior = prior.priorResponse
        }
        return count
    }
}
