# Nutriblend Android API client

A drop-in Kotlin client for the Nutriblend auth API: sign up, sign in, OTP
verification, secure token storage, and automatic access-token renewal.

Copy the `api/` folder into your app's source tree (e.g.
`app/src/main/java/com/nutriblend/api/`) and change the package line if your
package name differs.

---

## 1. Dependencies

`app/build.gradle.kts`:

```kotlin
dependencies {
    // Networking
    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.squareup.retrofit2:converter-moshi:2.11.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")

    // JSON
    implementation("com.squareup.moshi:moshi-kotlin:1.15.1")

    // Encrypted token storage
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
```

`AndroidManifest.xml`:

```xml
<uses-permission android:name="android.permission.INTERNET" />
```

> `security-crypto` has been on alpha for a long time; it is nonetheless the
> standard AndroidX option and is widely used in production. The 1.0.0 stable
> release also works if you prefer it.

## 2. Point it at your server

Edit `ApiConfig.BASE_URL`. **`localhost` will not work** — on a phone or
emulator that means the device itself.

| Running on | Base URL |
|---|---|
| Android emulator | `http://10.0.2.2:8000/` |
| Real device, same wifi | `http://192.168.x.x:8000/` (`ipconfig getifaddr en0` on your Mac) |
| Deployed | `https://api.yourdomain.com/` |

For a real device the server must also be started with
`--host 0.0.0.0`, otherwise it only listens on the Mac's loopback.

Plain `http://` needs a network security config — see the comment in
`ApiConfig.kt`. Scope it to your dev host; do not enable cleartext app-wide.

## 3. Create the client once

In your `Application` class:

```kotlin
class NutriblendApp : Application() {
    lateinit var apiClient: ApiClient
        private set

    override fun onCreate() {
        super.onCreate()
        apiClient = ApiClient(
            context = this,
            onSessionExpired = {
                // Refresh failed for good — bounce to the login screen.
                // Called on a background thread; post to the main thread.
            },
            enableLogging = BuildConfig.DEBUG,   // never log bodies in release
        )
    }
}
```

## 4. The flows

Both flows end at `verifyOtp`, so you need **one** OTP screen.

### Sign up

```kotlin
// Screen 1 — name + mobile
when (val r = auth.signUp(mobile = "9876543210", name = "Boss", email = null)) {
    is Result -> r.fold(
        onSuccess = { otp ->
            // otp.maskedMobile           -> "OTP sent to 98XXXXXX10"
            // otp.resendAvailableInSeconds -> start the resend countdown
            // otp.devOtp                 -> only while server ENV=development
            goToOtpScreen()
        },
        onFailure = { e ->
            when ((e as ApiException).code) {
                ErrorCode.ACCOUNT_EXISTS  -> goToSignIn("You already have an account")
                ErrorCode.EMAIL_IN_USE    -> showEmailError(e.message)
                ErrorCode.INVALID_MOBILE_NUMBER -> showMobileError(e.message)
                ErrorCode.RATE_LIMITED    -> startCountdown(e.retryAfterSeconds ?: 60)
                else -> showToast(e.message)
            }
        },
    )
}
```

### Sign in

```kotlin
auth.signIn(mobile).fold(
    onSuccess = { goToOtpScreen() },
    onFailure = { e ->
        when ((e as ApiException).code) {
            ErrorCode.ACCOUNT_NOT_FOUND -> goToSignUp("No account yet — let's create one")
            ErrorCode.ACCOUNT_BLOCKED   -> showContactSupport()
            ErrorCode.RATE_LIMITED      -> startCountdown(e.retryAfterSeconds ?: 60)
            else -> showToast(e.message)
        }
    },
)
```

### OTP screen (shared)

```kotlin
auth.verifyOtp(mobile, code).fold(
    onSuccess = { goToHome() },           // tokens are already stored
    onFailure = { e ->
        when ((e as ApiException).code) {
            ErrorCode.OTP_INCORRECT ->
                showError("Wrong code. ${e.attemptsRemaining} attempts left.")
            ErrorCode.OTP_EXPIRED, ErrorCode.OTP_NOT_REQUESTED ->
                showError("That code expired. Tap Resend.")
            ErrorCode.OTP_ATTEMPTS_EXCEEDED ->
                showError("Too many attempts. Tap Resend for a new code.")
            else -> showToast(e.message)
        }
    },
)
```

Resend button: disable it for `resendAvailableInSeconds` (60 by default) and
count down. If you skip this the user will tap it immediately, get a 429, and
think the app is broken.

## 5. Authenticated calls

Just call them. The access token is attached automatically, and a 401 triggers a
silent refresh + retry:

```kotlin
val user = auth.me().getOrNull()
auth.updateProfile(name = "Boss V")
auth.logout()            // this device
auth.logoutEverywhere()  // all devices
```

At app launch, `auth.isSignedIn` tells you whether to show the login screen or
go straight to home. It only checks that a refresh token exists — if it has
expired, the first real call triggers `onSessionExpired`.

## 6. The one thing to be careful about

The server **rotates** refresh tokens. Every call to `/auth/refresh` kills the
token you sent and returns a new one, and replaying a used token is treated as
theft — the server revokes the whole session and the user is signed out.

`AuthAuthenticator` handles this correctly: refreshes are serialised behind a
lock, and a thread that was waiting notices the token already changed and reuses
it instead of refreshing again. Without that, three simultaneous 401s would mean
three refresh calls, two of which look like replay attacks.

So: **do not call `/auth/refresh` yourself anywhere else**, and don't store
tokens outside `TokenStore`. If users ever report random logouts, start here.

## 7. Error codes

Switch on `ApiException.code`, never on the message — messages get reworded and
translated; codes are a contract.

| Code | Meaning |
|---|---|
| `ACCOUNT_EXISTS` | Signup: already registered → sign in |
| `ACCOUNT_NOT_FOUND` | Signin: no account → sign up |
| `ACCOUNT_BLOCKED` | Barred account |
| `EMAIL_IN_USE` | Email belongs to another account |
| `INVALID_MOBILE_NUMBER` | Bad number format |
| `VALIDATION_ERROR` | See `e.fieldErrors` — field → message |
| `OTP_INCORRECT` | See `e.attemptsRemaining` |
| `OTP_EXPIRED` / `OTP_NOT_REQUESTED` | Offer Resend |
| `OTP_ATTEMPTS_EXCEEDED` | Force a Resend |
| `OTP_SEND_FAILED` | Gateway problem — "try again shortly" |
| `RATE_LIMITED` | See `e.retryAfterSeconds` |
| `UNAUTHORIZED` / `INVALID_TOKEN` | Handled automatically by refresh |
| `REFRESH_TOKEN_INVALID` / `REFRESH_TOKEN_REUSED` | Session over → login screen |
| `NETWORK_ERROR` | No connection (client-side only) |

`ApiException.isSessionEnded` is a shortcut for the unrecoverable ones.

## 8. Notes

- The server accepts `9876543210`, `+919876543210`, `+91 98765 43210` and
  `09876543210` — all normalise to the same account, so you don't need to strip
  formatting before sending.
- `devOtp` is only returned while the server runs with `ENV=development`. It is
  null in production, and real SMS is not wired up yet, so build against
  `devOtp` for now.
- Set `enableLogging = false` in release builds. `Level.BODY` prints tokens and
  OTPs to logcat.
