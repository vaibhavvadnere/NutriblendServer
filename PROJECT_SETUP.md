# Nutriblend API — Setup & Progress Log

This file is kept up to date as the project grows. It records what has been
built, how to run it, and what's planned next.

Last updated: 2026-09-24

---

## 1. What this server does (current plan)

- Users are identified by **mobile number**, not email/password.
- **All endpoints are versioned** under `/api/v1`. `/api/health` is deliberately unversioned — it is infrastructure, not API surface.
- **Sign up**: `POST /api/v1/auth/signup` `{ name, mobile_number, email?, state? }` — creates a **pending** account and sends an OTP. The account is unusable until the OTP is verified, so nobody can reserve a number they don't control.
- **Sign in**: `POST /api/v1/auth/signin` `{ mobile_number }` — sends an OTP to an existing account.
- **Both** end at `POST /api/v1/auth/verify-otp`, which activates the account and returns an **access token + refresh token**. One OTP screen in the app, one "I'm logged in" code path.
- **Account lifecycle**: `pending` → `active` → (`blocked`, admin only). Abandoned pending signups are auto-deleted after `PENDING_USER_TTL_HOURS`, so no number is locked up forever.
- **Rate limiting**: every OTP-sending endpoint is throttled per mobile number and per IP, with `429` + `Retry-After`. Quota is consumed *before* the account lookup, so probing for registered numbers costs the attacker quota.
- **Responses**: every endpoint returns `{"success", "message", "data"}` on success and `{"success": false, "message", "error": {"code", "details?"}}` on failure. Branch on `error.code`, never on the message text.
- **Token model**:
  - *Access token* — short-lived JWT (30 min by default), sent as `Authorization: Bearer <token>` on every protected request. Stateless.
  - *Refresh token* — long-lived (30 days), opaque random string, stored **hashed** in MongoDB. Exchanged at `POST /api/auth/refresh` for a new access token, and **rotated on every use**. Replaying an already-used refresh token revokes the entire session (theft detection).
  - `POST /api/auth/logout` ends one session; `POST /api/auth/logout-all` ends all of them.
- **SMS delivery**: pluggable. `SMS_PROVIDER` in `.env` picks between `mock` (logs only), `msg91`, `twilio` and `fast2sms`. No code changes to switch.
- **Database**: MongoDB via the async `motor` driver. Works unchanged against local mongod, Docker, or Atlas — only `MONGO_URI` differs.
- **CORS**: enabled for all origins for development. Tighten before production.

---

## 2. Project structure

```
NutriblendServer/
├── app/
│   ├── main.py              # FastAPI entrypoint, lifespan (DB connect), CORS, routers
│   ├── core/                # Cross-cutting plumbing
│   │   ├── config.py        # Settings loaded from .env (pydantic-settings)
│   │   ├── database.py      # MongoDB connection (motor), ping, index setup, Atlas TLS
│   │   ├── security.py      # Access-token JWTs, refresh-token generation, OTP hashing
│   │   ├── exceptions.py    # ErrorCode + domain errors raised by services (no HTTP)
│   │   ├── errors.py        # Domain error -> HTTP status mapping; one error shape for all
│   │   └── deps.py          # get_current_user (JWT auth) + client IP resolution
│   ├── routers/             # HTTP only: validate input, call a service, return a schema
│   │   ├── auth.py          # signup, signin, resend-otp, verify-otp, refresh, logout(-all)
│   │   └── users.py         # GET/PATCH /users/me, GET /users/{id}, POST /users (deprecated)
│   ├── services/            # All business rules (no FastAPI, no Mongo queries)
│   │   ├── auth_service.py  # OTP issue/verify flow, token refresh, logout
│   │   ├── user_service.py  # Account lifecycle: create/refresh pending, activate, block
│   │   ├── token_service.py # Refresh-token lifecycle: issue / rotate / revoke / families
│   │   └── rate_limiter.py  # Fixed-window limits and the OTP rule sets
│   ├── repositories/        # The ONLY code that touches MongoDB, one module per collection
│   │   ├── user_repo.py
│   │   ├── otp_repo.py
│   │   ├── refresh_token_repo.py
│   │   └── rate_limit_repo.py
│   ├── providers/
│   │   └── sms/             # send_otp_sms() + one module per gateway, same interface
│   │       ├── base.py      # SMSProvider ABC + SMSDeliveryError + number formatting
│   │       ├── mock.py      # Default — logs the OTP, sends nothing
│   │       ├── msg91.py
│   │       ├── twilio.py
│   │       └── fast2sms.py
│   ├── schemas/
│   │   ├── user.py          # UserCreate, UserUpdate, UserOut + shared field validators
│   │   └── auth.py          # SignupRequest, SigninRequest, OTPVerify, TokenResponse, ...
│   └── utils/
│       └── phone.py         # Mobile-number normalisation (+91 / 0 / spaces / dashes)
├── scripts/
│   ├── check_db.py        # Verify the MONGO_URI in .env before starting the server
│   └── verify_atlas.py    # Run the real signup/signin flow against the live database
├── requirements.txt
├── .env / .env.example     # .env is generated from the example; never commit it
├── .gitignore
├── clients/
│   └── android/             # Drop-in Kotlin API client for the mobile app
│       ├── README.md        #   wiring, base URLs, flows, error-code reference
│       └── api/*.kt         #   Retrofit client, encrypted token store, auto-refresh
├── README.md
├── API_ROADMAP.md           # Design decisions + the product roadmap ahead
└── PROJECT_SETUP.md         # this file
```

**Architecture — Router → Service → Repository.** A request flows one way:
`routers/` (HTTP) → `services/` (business rules) → `repositories/` (MongoDB).
Services raise domain errors from `core/exceptions.py`; `core/errors.py` is the only
place that turns them into HTTP status codes. External gateways (SMS today) sit
behind a provider interface in `providers/`, chosen from `.env`.


---

## 3. Setup steps completed so far

1. ✅ Project folder structure (`app/`, `routers/`, `schemas/`, `utils/`).
2. ✅ Dependencies installed: `fastapi`, `uvicorn`, `motor`, `pymongo`, `pydantic-settings`,
   `python-jose` (JWT), `python-dotenv`, `email-validator`, `httpx`, `certifi`.
3. ✅ User model + `POST /api/users` (mobile number validated as a 10-digit Indian number).
4. ✅ OTP login flow (`request-otp` / `verify-otp`) with hashed OTPs, expiry, and a max-attempts lockout.
5. ✅ JWT issuance on OTP verification + `get_current_user` dependency for protected routes.
6. ✅ CORS enabled for browser access from any origin.
7. ✅ **Pluggable SMS provider layer** — `SMS_PROVIDER` selects mock / MSG91 / Twilio / Fast2SMS.
   A misconfigured provider fails loudly at startup instead of silently dropping OTPs, and a
   gateway failure returns `502` with the pending OTP deleted (no "OTP sent" lie).
8. ✅ **Refresh tokens + revocation** — short access tokens, rotating refresh tokens stored hashed,
   token families with reuse detection, `/logout` and `/logout-all`.
9. ✅ **Atlas-ready database layer** — certifi CA bundle for `mongodb+srv://`, fast-failing server
   selection timeout, startup ping with an actionable error message, `scripts/check_db.py`.
10. ✅ `/api/health` now reports database reachability and the active SMS provider.
11. ✅ Security hardening: OTPs generated with `secrets` (not `random`), OTP comparison is
    constant-time, access tokens carry a `type` claim so no other token can be replayed as one.
12. ✅ Full flow re-verified end-to-end with an automated smoke test against an in-memory mock
    Mongo — 39 checks, all passing (see section 9). No test files left in the project.
13. ✅ **Live MongoDB Atlas connection** — cluster `Nutri-1`, database `nutriblend`, connected and
    verified with `scripts/check_db.py` (MongoDB 8.0.32). All three collections (`users`, `otps`,
    `refresh_tokens`) exist with their indexes built. The server now has a real database.
14. ✅ **Sign up / sign in APIs** — `POST /auth/signup` and `POST /auth/signin`, both feeding the
    shared `verify-otp`. Accounts are created `pending` and only activated by OTP verification,
    which closes the number-squatting hole in the old `POST /api/users`.
15. ✅ **Account lifecycle** — `status` (pending/active/blocked), `verified_at`, `last_login_at`,
    `updated_at`. Sparse unique index on `email`. TTL cleanup of abandoned signups.
16. ✅ **Mobile number normalisation** — `+91 98765 43210`, `09876543210`, `91-9876543210` and
    `9876543210` are all now the same account. Previously only the last form was accepted.
17. ✅ **Rate limiting** — per-number cooldown plus hourly/daily caps, and per-IP caps on both
    OTP sending and verification. Mongo-backed, so it works across multiple workers. Returns
    `429` with `Retry-After`.
18. ✅ **Machine-readable error codes** — every error carries `error.code` inside the standard response envelope, so the
    app can branch on `ACCOUNT_EXISTS` vs `ACCOUNT_NOT_FOUND` without matching English strings,
    and messages can be reworded or translated without an app release.
19. ✅ **API versioning** — everything moved under `/api/v1`.
20. ✅ **`POST /users/{id}` and `PATCH /users/me`** — profile read/update; `GET /users/{id}` now
    requires authentication (it was previously open, which let anyone walk the user table).
21. ✅ Re-verified end-to-end: **67 checks, all passing**, covering every edge case in
    `API_ROADMAP.md` §5.
22. ✅ **Verified against the live Atlas database** — `scripts/verify_atlas.py`, 20 checks, all
    passing. This is what proves the guarantees mock Mongo cannot: the unique index really
    rejects a duplicate mobile number, the sparse unique email index really rejects a duplicate
    address *while still allowing many accounts with no email at all*, TTL indexes exist on all
    four collections, and `pending_expires_at` is really removed on activation.

## 4. What's NOT done yet (planned next)

- [ ] Real SMS credentials — the provider code is written; it needs an account + API key
      (and, for India, a DLT-registered sender/template).
- [ ] Production CORS lock-down (replace `allow_origins=["*"]` with your real frontend domain).
- [ ] Set `TRUST_PROXY_HEADERS=true` **at deploy time**, once the app is behind a proxy you
      control — until then per-IP rate limits see the proxy's IP, not the caller's.
- [ ] Commit a `tests/` suite (currently smoke tests are throwaway — see `API_ROADMAP.md` §8).
- [ ] Deployment (VPS / Render / Railway / Docker).
- [ ] Product features — see `API_ROADMAP.md` Part B for the phased plan.

---

## 5. How to run it locally

### Prerequisites
- Python 3.11+ — this project uses Homebrew's `python@3.13`, not the macOS system Python.
- A MongoDB instance — Atlas is already connected (see section 6).

### Steps
```bash
cd NutriblendServer

# 1. Activate the virtual environment — do this once per terminal session.
#    After this, plain `python3` and `pip` point inside the project.
source .venv/bin/activate

# 2. Dependencies (only needed after requirements.txt changes)
pip install -r requirements.txt

# 3. Environment — .env already exists with a generated JWT_SECRET and the
#    live Atlas MONGO_URI. Edit it to add SMS settings when you're ready.

# 4. Check the database connection before starting
python3 scripts/check_db.py

# 5. Run the server
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

> **Always activate the venv first.** Without it, `python3` is the macOS system
> interpreter, which does not have this project's dependencies and will fail with
> `ModuleNotFoundError`. If you'd rather not activate, prefix every command with
> `./.venv/bin/` — including the leading dot.
>
> To rebuild the venv from scratch:
> `rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`

Then open:
- http://localhost:8000/docs — interactive Swagger UI
- http://localhost:8000/api/health — health check (also reports DB + SMS provider status)

### Example request flow (via curl)
```bash
BASE=http://localhost:8000/api/v1

# ── SIGN UP ──────────────────────────────────────────────────────────────────
# 1. Create the account and send an OTP. Any of these number formats work:
#    9876543210 | +919876543210 | +91 98765 43210 | 09876543210
curl -X POST $BASE/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"mobile_number": "+91 98765 43210", "name": "Boss", "email": "boss@example.com", "state": "Maharashtra"}'
# -> 201 {"message":"OTP sent successfully","mobile_number":"98XXXXXX10",
#         "expires_in_minutes":5,"resend_available_in_seconds":60,
#         "is_new_account":true,"dev_otp":"482913"}

# ── SIGN IN ──────────────────────────────────────────────────────────────────
curl -X POST $BASE/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"mobile_number": "9876543210"}'

# Resend (subject to the same 60s cooldown)
curl -X POST $BASE/auth/resend-otp \
  -H "Content-Type: application/json" \
  -d '{"mobile_number": "9876543210"}'

# ── VERIFY (used by BOTH flows) ──────────────────────────────────────────────
curl -X POST $BASE/auth/verify-otp \
  -H "Content-Type: application/json" \
  -d '{"mobile_number": "9876543210", "otp": "482913"}'
# -> 200 { access_token, refresh_token, expires_in, user: {...} }

# ── AUTHENTICATED ────────────────────────────────────────────────────────────
curl $BASE/users/me -H "Authorization: Bearer <access_token>"

curl -X PATCH $BASE/users/me \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "Boss V"}'

# ── TOKENS ───────────────────────────────────────────────────────────────────
# The response contains a NEW refresh token. Store it and discard the old one —
# sending the old one again is treated as theft and kills the session.
curl -X POST $BASE/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "<refresh_token>"}'

curl -X POST $BASE/auth/logout \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "<refresh_token>"}'

curl -X POST $BASE/auth/logout-all -H "Authorization: Bearer <access_token>"
```

### Response format

Every endpoint uses the same envelope (defined in `app/schemas/common.py`). HTTP status
codes stay meaningful — `success` is a convenience, not a replacement for the status.

Success:

```json
{ "success": true,
  "message": "OTP sent successfully",
  "data": { "mobile_number": "98XXXXXX10", "expires_in_minutes": 5, "...": "..." } }
```

Failure:

```json
{ "success": false,
  "message": "An account with this mobile number already exists. Please sign in instead.",
  "error": { "code": "ACCOUNT_EXISTS" } }
```

`error.details` appears only when there is something to add — e.g.
`attempts_remaining`, `retry_after_seconds`, or `fields` for validation errors.

**Branch on `error.code`, never on `message`** — messages will be reworded and eventually
translated; codes are a contract. The ones the sign-up / sign-in screens need:

| Code | HTTP | What the app should do |
|---|---|---|
| `ACCOUNT_EXISTS` | 409 | Signup: send them to the sign-in screen |
| `ACCOUNT_NOT_FOUND` | 404 | Signin: send them to the sign-up screen |
| `ACCOUNT_BLOCKED` | 403 | Show a "contact support" message |
| `EMAIL_IN_USE` | 409 | Highlight the email field |
| `INVALID_MOBILE_NUMBER` | 422 | Highlight the number field |
| `VALIDATION_ERROR` | 422 | `details.fields` maps field name → message |
| `OTP_INCORRECT` | 400 | `details.attempts_remaining` says how many tries are left |
| `OTP_EXPIRED` | 400 | Offer "Resend OTP" |
| `OTP_NOT_REQUESTED` | 400 | Go back to the number screen |
| `OTP_ATTEMPTS_EXCEEDED` | 429 | Force a resend |
| `OTP_SEND_FAILED` | 502 | "Try again in a moment" |
| `RATE_LIMITED` | 429 | `Retry-After` header + `details.retry_after_seconds` → countdown |
| `UNAUTHORIZED` / `INVALID_TOKEN` | 401 | Try refresh; if that fails, sign out |
| `REFRESH_TOKEN_INVALID` / `REFRESH_TOKEN_REUSED` | 401 | Sign out, return to login |

### Client-side rule of thumb
Store both tokens. Send the access token on every request. On a `401`, call
`/api/auth/refresh` once with the stored refresh token, **replace both stored
tokens with what comes back**, and retry the original request. If the refresh
also returns `401`, send the user back to the login screen.

---

## 6. MongoDB Atlas setup

1. Create a free cluster at https://www.mongodb.com/atlas (M0 tier is fine).
2. **Database Access** → add a database user (username + password). Use a password without
   exotic characters, or URL-encode it in the URI (`@` → `%40`, `:` → `%3A`, `/` → `%2F`).
3. **Network Access** → add your current IP, or `0.0.0.0/0` while developing.
4. **Connect → Drivers → Python** → copy the connection string. It looks like:
   `mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority`
5. Paste it into `.env` as `MONGO_URI` and set `MONGO_DB_NAME=nutriblend`.
6. Verify: `.venv/bin/python3 scripts/check_db.py` — it prints the server version, the
   collections and the indexes, or an error telling you which of the above went wrong.

No code changes are needed — `database.py` already passes certifi's CA bundle for
`mongodb+srv://` URIs, which is what otherwise causes "certificate verify failed" on macOS.

**Status: done.** Connected to cluster `Nutri-1` (`nutri-1.o9jjeml.mongodb.net`), database
`nutriblend`, verified 2026-09-21 against MongoDB 8.0.32.

Gotchas hit during setup, for next time:
- Run the venv's Python explicitly — `./.venv/bin/python3 scripts/check_db.py`, with the leading
  dot — or `source .venv/bin/activate` first. Plain `python3` uses the system interpreter, which
  does not have this project's dependencies.
- After pulling new dependencies into `requirements.txt`, install them into the venv with
  `./.venv/bin/python3 -m pip install -r requirements.txt` (the `-m pip` form targets the venv
  regardless of what a bare `pip` points at).

---

## 7. SMS / OTP provider setup

Pick one with `SMS_PROVIDER` in `.env`. `mock` is the default and sends nothing.

| Provider | Env vars needed | Notes |
|---|---|---|
| `mock` | — | Logs the OTP to the console. Default for development. |
| `msg91` | `MSG91_AUTH_KEY`, `MSG91_TEMPLATE_ID`, `MSG91_SENDER_ID` (optional) | India-focused OTP API. Needs a DLT-approved template containing `##OTP##`. |
| `twilio` | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Global, great docs. Pricier for Indian numbers, and India still requires DLT registration. |
| `fast2sms` | `FAST2SMS_API_KEY`, plus `FAST2SMS_SENDER_ID` + `FAST2SMS_MESSAGE_ID` if `FAST2SMS_ROUTE=dlt` | Cheap Indian provider. The default `otp` route needs no DLT template. |

Notes:
- Required credentials are checked the first time a provider is used. Missing ones raise a
  clear error naming the exact `.env` keys, rather than failing silently.
- If the gateway rejects the message, `request-otp` returns `502` and the pending OTP is
  deleted, so the user is never told "OTP sent" when it wasn't.
- To add another gateway: write a class in `app/providers/sms/` that subclasses
  `SMSProvider`, register it in that package's `_REGISTRY`. Nothing else changes.
- **DLT registration (India):** any provider sending to Indian numbers needs your sender ID
  and message template registered with the telecom regulator. Budget a few days for this.

---

## 8. Environment variables (`.env`)

| Variable | Purpose | Default |
|---|---|---|
| `APP_NAME` | Shown in docs + health | `Nutriblend API` |
| `ENV` | `development` or `production` | `development` — in dev, the OTP is echoed back as `dev_otp`. **Must be `production` before going live.** |
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB_NAME` | Database name | `nutriblend` |
| `MONGO_SERVER_SELECTION_TIMEOUT_MS` | How long to wait for a reachable server | `8000` |
| `JWT_SECRET` | Signs access tokens | generated into `.env` — never share or commit |
| `JWT_ALGORITHM` | JWT signing algorithm | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access token lifetime | `30` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Refresh token lifetime | `30` |
| `OTP_LENGTH` / `OTP_EXPIRE_MINUTES` / `OTP_MAX_ATTEMPTS` | OTP behaviour | `6` / `5` / `5` |
| `SMS_PROVIDER` | `mock`, `msg91`, `twilio`, `fast2sms` | `mock` |
| `SMS_COUNTRY_CODE` | Prepended for E.164 formatting | `91` |
| `SMS_TEMPLATE` | Message body; `{otp}`, `{minutes}`, `{app}` are substituted | see `.env.example` |
| `SMS_TIMEOUT_SECONDS` | HTTP timeout when calling the gateway | `10` |
| provider keys | see section 7 | unset |

---

## 9. Verification status

Last smoke test (2026-09-24, in-memory mock Mongo, **67 checks, all passed**). Covers the
signup/signin flows, every edge case in `API_ROADMAP.md` §5, rate limiting, blocked accounts,
token rotation and reuse detection, SMS failure handling, and production-mode OTP suppression.

**Also verified against the live Atlas database** (2026-09-24, `scripts/verify_atlas.py`,
**20 checks, all passed**) — signup, OTP verification, token rotation and reuse rejection,
rate limiting, and the index guarantees mock Mongo cannot prove:

| Guarantee | Result |
|---|---|
| Duplicate `mobile_number` rejected by the database itself | ✅ |
| Duplicate `email` rejected by the sparse unique index | ✅ |
| Many accounts with **no** email still allowed (sparse works) | ✅ |
| `pending_expires_at` removed on activation (so actives never expire) | ✅ |
| TTL indexes present on `users`, `otps`, `refresh_tokens`, `rate_limits` | ✅ |

Index inventory at time of verification: users 5, otps 3, refresh_tokens 5, rate_limits 2.

Previous smoke test (2026-09-21, 39 checks, all passed):

- Health endpoint, including SMS-provider reporting
- User creation, invalid mobile number rejected (422)
- OTP request for an unknown number rejected (404); `dev_otp` echoed in development
- Wrong OTP rejected (400) and the attempt counter incremented
- Correct OTP verified → access + refresh tokens issued, user marked verified, used OTP deleted
- `/me` works with the access token; rejected with a bad token and with no token
- Refresh returns a new access token **and** a rotated refresh token
- Replaying a used refresh token → 401 **and** the whole token family is revoked
- `logout` kills that session; `logout-all` kills every session and requires a valid token
- SMS gateway failure → 502, no stale OTP left behind, no provider details leaked to the client
- `ENV=production` suppresses `dev_otp`

---

## 10. Change log

- **2026-09-24 (client)** — Added `clients/android/`: a drop-in Kotlin API client
  (Retrofit + Moshi) with EncryptedSharedPreferences token storage and an OkHttp
  Authenticator that renews access tokens on 401. The authenticator serialises refreshes
  behind a lock — without that, simultaneous 401s would each call `/auth/refresh`, and the
  replayed (already-rotated) tokens would trip server-side reuse detection and sign the user
  out at random. Kept next to the server so the error-code contract stays in one repo.
- **2026-09-24 (verification)** — Full flow verified against the live Atlas database via
  `scripts/verify_atlas.py`: 20 checks, all passing, including real unique-index and sparse-index
  enforcement and TTL index presence. Two script bugs fixed on the way (a `.invalid` test email,
  which `email-validator` rejects as a special-use domain, and a cascading crash when a
  prerequisite step failed).
- **2026-09-24 (later)** — **Sign up / sign in APIs.** `POST /auth/signup` + `POST /auth/signin`
  sharing `verify-otp`; pending→active→blocked account lifecycle with TTL cleanup of abandoned
  signups; mobile-number normalisation; Mongo-backed rate limiting with `Retry-After`;
  machine-readable error codes on every response; all routes moved under `/api/v1`;
  `PATCH /users/me` added and `GET /users/{id}` closed off behind auth; `POST /users` deprecated
  and downgraded to creating pending accounts only. Two bugs found and fixed by the new tests
  (a signup retry silently erasing a previously supplied email; rate-limit quota not being
  consumed by failed lookups, which left number enumeration free). 67 checks passing.
  New: `API_ROADMAP.md`, `scripts/verify_atlas.py`.
- **2026-09-24** — Virtual environment rebuilt natively on macOS (Homebrew `python@3.13`);
  the previous `.venv` had been created inside a Linux sandbox and did not work on the Mac,
  which silently pushed installs into the system Python. Section 5 rewritten around
  `source .venv/bin/activate`.
- **2026-09-21 (later)** — MongoDB Atlas is live: `MONGO_URI` points at cluster `Nutri-1`,
  connection verified against MongoDB 8.0.32, all collections and indexes created on first
  connect. `certifi` and `httpx` installed into the project venv.
- **2026-09-21** — Pluggable SMS provider layer (mock/MSG91/Twilio/Fast2SMS via `SMS_PROVIDER`);
  refresh tokens with rotation, token families and reuse detection, plus `/logout` and
  `/logout-all`; access tokens shortened to 30 minutes and given a `type` claim; Atlas-ready
  database layer with certifi TLS, startup ping and `scripts/check_db.py`; `/api/health` now
  reports DB + SMS status; OTPs generated with `secrets` and compared in constant time;
  `.env` created with a generated `JWT_SECRET`. Re-verified end-to-end (39 checks).
- **2026-09-10** — Initial setup: project scaffolded, User model, OTP-based auth (request/verify),
  JWT issuance, CORS enabled, dependencies installed, end-to-end flow verified via automated
  smoke test with an in-memory mock database.
