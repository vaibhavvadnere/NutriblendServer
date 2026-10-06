# Nutriblend API

FastAPI + MongoDB backend for the Nutriblend app. Login is by **mobile number + OTP**
— no passwords.

## Endpoints

All endpoints are versioned under `/api/v1`. `/api/health` is unversioned.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/health` | — | Liveness + DB and SMS provider status |
| POST | `/api/v1/auth/signup` | — | Create an account (pending) and send an OTP |
| POST | `/api/v1/auth/signin` | — | Send an OTP to an existing account |
| POST | `/api/v1/auth/resend-otp` | — | Resend an OTP (same cooldown applies) |
| POST | `/api/v1/auth/verify-otp` | — | Verify the OTP → activate + issue tokens |
| POST | `/api/v1/auth/refresh` | — | Exchange a refresh token (rotates it) |
| POST | `/api/v1/auth/logout` | — | Revoke this session |
| POST | `/api/v1/auth/logout-all` | Bearer | Revoke every session |
| GET | `/api/v1/users/me` | Bearer | Own profile |
| PATCH | `/api/v1/users/me` | Bearer | Update name / email |
| GET | `/api/v1/users/{id}` | Bearer | Fetch a user by id |
| POST | `/api/v1/users` | — | *Deprecated* — admin/seeding only |
| POST | `/api/v1/admin/auth/signin` | — | Dashboard sign-in (admins only), then `/auth/verify-otp` |
| POST | `/api/v1/admin/admins` | — | Create an admin (open in development; `ADMIN_CREATE_ENABLED`) |
| GET | `/api/v1/users` | Admin | List app users (admins excluded) |
| GET | `/api/v1/admin/stats` · `/admin/users/{id}` | Admin | Dashboard stats · one user |
| PATCH | `/api/v1/admin/users/{id}/status` | Admin | Block / unblock |
| POST | `/api/v1/admin/videos` | Admin | Start a video upload (details + file size) |
| PUT | `/api/v1/admin/videos/{id}/upload/chunks/{n}` | Admin | Upload one chunk (raw bytes) |
| GET · POST | `/api/v1/admin/videos/{id}/upload` · `/upload/complete` | Admin | Upload progress · finish |
| GET · PATCH · DELETE | `/api/v1/admin/videos[/{id}]` | Admin | List / edit / delete videos |
| PATCH | `/api/v1/admin/videos/{id}/status` | Admin | Draft / published |
| PUT · DELETE | `/api/v1/admin/videos/{id}/thumbnail` | Admin | Cover image |
| GET | `/api/v1/videos` · `/videos/{id}` · `/videos/categories` | Bearer | Published videos for the app, with expiring links |
| GET | `/media/{key}?exp=&sig=` | Signed link | Video/image file (Range supported) |
| PUT · DELETE | `/api/v1/admin/videos/{id}/document` | Admin | Attach (raw body + `X-File-Name`) / remove the video's PDF, Word or PowerPoint |
| POST · GET | `/api/v1/admin/videos/{id}/document/retry` · `/document/pages` | Admin | Re-prepare · page image links |
| GET | `/api/v1/videos/{id}/document` | Bearer | The document as page images (view-only; the original is never served) |

Sign up and sign in both end at `verify-otp`, so the app needs one OTP screen.
Every response uses one envelope, and HTTP status codes stay meaningful:

- Success: `{"success": true, "message": "...", "data": {...}}`
- Failure: `{"success": false, "message": "...", "error": {"code": "...", "details": {...}}}` — branch on `error.code`.

## Privacy policy (Google Play)

Public, no login: **`/privacy-policy`** (use this URL in Play Console and in the app)
and **`/account-deletion`** (Play Console → Data safety → Data deletion). The text is
`app/legal/privacy_policy.md`, filled from the `LEGAL_*` values in `.env`; the
dashboard's **Privacy policy** page shows it with a Google Play readiness checklist.

## Documents (view-only)

Each video can have one PDF, Word or PowerPoint document. The original is never
served; the app is shown page images. Word/PowerPoint need **LibreOffice** on the
server (`brew install --cask libreoffice`); PDFs work without it.

## Quick start

```bash
.venv/bin/python3 -m pip install -r requirements.txt
.venv/bin/python3 scripts/check_db.py          # verify MONGO_URI in .env
.venv/bin/python3 -m uvicorn app.main:app --reload
```

Interactive docs: http://localhost:8000/docs

See **PROJECT_SETUP.md** for the full guide — Atlas setup, SMS provider setup,
every environment variable, and what's still on the to-do list.

## Admin dashboard

A separate Streamlit app lives in `dashboard/` (own venv and requirements; talks to
this API over HTTP only). See **dashboard/README.md**.
