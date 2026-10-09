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
| PATCH | `/api/v1/admin/videos/{id}/status` | Admin | Draft / published (`when_ready` waits for the document — see below) |
| GET | `/api/v1/admin/videos/optimization` | Admin | Is automatic optimization on (and is ffmpeg there)? |
| POST | `/api/v1/admin/videos/{id}/optimization/retry` | Admin | Queue a failed optimization again |
| GET | `/api/v1/admin/videos/check-details` | Admin | Same-title and category-spelling hints (nothing saved) |
| PUT · DELETE | `/api/v1/admin/videos/{id}/thumbnail` | Admin | Cover image |
| GET | `/api/v1/videos` · `/videos/{id}` · `/videos/categories` | Bearer | Published videos for the app, with expiring links |
| GET | `/media/{key}?exp=&sig=` | Signed link | Video/image file (Range supported). With R2: 302 to a short-lived R2 link |
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

## Storage check

`GET /api/v1/admin/videos/storage-check` (and the dashboard's Videos page) lists
videos whose files are missing from the current storage — e.g. uploaded while
`STORAGE_PROVIDER=local`, before switching to `r2`. From Terminal:

```bash
.venv/bin/python3 scripts/check_media.py           # report
.venv/bin/python3 scripts/check_media.py --delete  # delete those records (asks first)
```

## Browser uploads (from any computer)

The dashboard's upload box runs in the admin's browser and sends the video
**straight to R2** — the bytes never pass through this server or the dashboard:

1. `POST /admin/videos` (with `sha256`) → `POST /admin/videos/{id}/upload/ticket`
   (an upload ticket for that one video, valid `UPLOAD_TICKET_TTL_HOURS`; outlives
   the 15-minute dashboard session).
2. For each piece: `POST …/upload/part-urls` (piece number + MD5) → a signed
   R2 link with Content-MD5 in the signature → the browser PUTs the piece there
   (4 at a time). Piece 0 always goes through `PUT …/upload/chunks/0` (MP4 check).
3. `GET …/upload` reads what R2 holds (size + MD5 per piece) to know what's
   missing; `POST …/upload/complete` re-checks everything.

The upload endpoints accept `X-Upload-Ticket` instead of the admin session.

**Documents** go the same way: `POST /admin/videos/{id}/document/ticket`
(dashboard) → `POST …/document/upload` (file name, size, SHA-256) → the browser
PUTs the whole file to the signed link (size and type are part of the
signature) → `POST …/document/upload/{upload_id}/complete` (size + file-type
check; the old document stays until this succeeds) → pages are prepared after
a SHA-256 check. `PUT …/document` (through the server, `X-File-SHA256`
checked) remains for local storage and as the fallback. Tickets are per
purpose: a document ticket can't send video pieces and vice versa.
**One-time R2 setup** — allow browsers on the dashboard's address to upload:

```bash
.venv/bin/python3 scripts/r2_cors.py           # show current rules
.venv/bin/python3 scripts/r2_cors.py --apply   # allow PUT from DASHBOARD_ORIGINS
```

`DASHBOARD_ORIGINS` (in `.env`, default `["http://localhost:8501","http://127.0.0.1:8501"]`)
must list every address the dashboard is opened from; re-run `--apply` when it
changes. If the R2 key may only read/write files (the usual, safer setup),
`--apply` can't change bucket settings and instead prints the JSON to paste in
Cloudflare: R2 → bucket → Settings → CORS Policy. Without these rules the box
falls back to sending pieces through the server (slower, still verified).
`UPLOAD_DIRECT_TO_STORAGE=false` forces that.

## Upload integrity & duplicates

- `POST /admin/videos` takes an optional `sha256` (whole-file fingerprint). If a
  video with the same fingerprint exists → `409 DUPLICATE_VIDEO` with
  `details: {video_id, title, status, created_at, uploaded_fraction}`; send
  `allow_duplicate: true` to upload it anyway.
- Each chunk may carry a `Content-MD5` header. The server checks it
  (`400 UPLOAD_CHECKSUM_MISMATCH` → send the chunk again) and passes it to R2,
  which checks it once more.
- `POST …/upload/complete` re-checks what storage holds (R2 part sizes + MD5s;
  locally, the bytes on disk and the whole-file SHA-256). Damaged chunks →
  `409 UPLOAD_INCOMPLETE` with `reason: "verification_failed"`; they are
  forgotten, so the client just sends them again. The video then has
  `integrity_verified: true`.
- The dashboard does all of this automatically ("Checking file…", then upload).

## Documents (view-only)

Each video can have one PDF, Word or PowerPoint document. The original is never
served; the app is shown page images. Word/PowerPoint need **LibreOffice** on the
server (`brew install --cask libreoffice`); PDFs work without it.

**A video with a document is only shown in the app once the document is ready.**
`PATCH …/status {"status": "published"}` while the document is being prepared
answers `409 DOCUMENT_NOT_READY` (`details.can_publish_when_ready`); with
`"when_ready": true` the video stays a draft (`publish_when_ready`) and is
published automatically the moment the document is ready. A failed document
blocks publishing (`409`, retry or remove it) and cancels the wait. The app's
video list, video details, categories and document pages also hide a published
video whose document isn't ready (admin responses show `visible_in_app`) — for
example for a minute while a document is being replaced.

**Title and category checks.** `POST /admin/videos` and `PATCH /admin/videos/{id}`
answer `409 DUPLICATE_TITLE` (`details.videos`) when another video has the same
title (case/space-insensitive); `allow_duplicate_title: true` overrides.
`GET /admin/videos/check-details?title=&category=&exclude_id=` returns the same
information without saving (`same_title`, `category.canonical` / `similar`). A
category that differs from an existing one only by capitals or spaces is stored
with the existing spelling.

**Replacing a video's file.** `POST /admin/videos/{id}/replace-file` (same body
as a new upload plus the usual duplicate check) creates a hidden staging upload
(`replaces`) and marks the target with `replacement`. The staging upload uses
the normal ticket / part-urls / chunks / complete endpoints; on complete the
target's `file` is swapped for the new one, the staging record is deleted, then
the old object is deleted from storage. The old file plays until the swap.
Deleting the staging record cancels the replacement; deleting the video drops
its staging record too. Staging records never appear in lists or counts.

## Automatic video optimization

Admins upload big files (≈ 800 MB for 5 minutes); phones don't need that. The server
converts every upload into a much smaller file and **stores only the converted file in R2 — the
original never reaches R2** (one exception below).

1. The upload goes to a local **staging** folder (`VIDEO_STAGING_ROOT`), not to R2. The video is a
   draft with `optimization.state = queued`; staged files are never linked or visible.
2. A background worker (started with the server; one job at a time by default) runs ffmpeg:
   H.264, CRF 21, preset `fast`, same resolution and frame rate, AAC 128 kbit/s, `+faststart`,
   low CPU priority. The result is checked (length, size, sound, H.264).
3. Only the result is uploaded to R2 and its size verified; then the staged original is deleted and,
   if the admin asked for it, the video is published (`publish_when_ready`).

Modes shown on the video: `compressed` (≥ `VIDEO_OPTIMIZE_MIN_SAVING_PERCENT` smaller),
`converted` (HEVC/10-bit etc. → H.264), `kept` (already-efficient H.264 up to
`VIDEO_OPTIMIZE_SKIP_BELOW_KBPS`, or a re-encode that wouldn't be clearly smaller — the uploaded file is
stored as it is; **the only case where an uploaded file reaches R2**). If conversion fails the video shows
*Optimization failed*, the original stays in staging (never sent to R2) and
`POST /admin/videos/{id}/optimization/retry` tries again.

Publishing while optimizing → `409 VIDEO_OPTIMIZING` (`details.can_publish_when_ready`);
`{"status":"published","when_ready":true}` waits. A failed optimization blocks publishing. Replacement files
(`replace-file`) are optimized too and swapped in afterwards. A re-upload of the same file is still caught
(`file.source_sha256` keeps the original's fingerprint).

Settings: see `.env.example` (`VIDEO_OPTIMIZE_*`, `VIDEO_STAGING_*`). Needs **ffmpeg + ffprobe**
(`brew install ffmpeg` on the Mac; already in the Docker image). Staging needs free disk — the upload is
refused (507) when `file × 1.5 + files still waiting + VIDEO_STAGING_MARGIN_MB` doesn't fit; plan ~50 GB on the
server. Real-world timing on the small AWS server (2 GB RAM) is still to be measured at deployment.

## Quick start

```bash
.venv/bin/python3 -m pip install -r requirements.txt
.venv/bin/python3 scripts/check_db.py          # verify MONGO_URI in .env
.venv/bin/python3 -m uvicorn app.main:app --reload
```

Interactive docs: http://localhost:8000/docs (turned off when `ENV=production`)

## Media storage

`STORAGE_PROVIDER=local` keeps files in `media/` (development).
`STORAGE_PROVIDER=r2` keeps them in a private Cloudflare R2 bucket (production):
fill `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` in
`.env`. The app's signed `/media` links then redirect to short-lived R2 links, so
video bytes go from R2 to the phone without passing through the server.

## Tests

```bash
.venv/bin/python3 -m pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest
```

They use a local fake S3 (moto) and an in-memory MongoDB, never real cloud
resources (92 tests). ffmpeg and LibreOffice make some of them more thorough (skipped if missing);
the optimization tests run real ffmpeg.

## Docker (production)

`Dockerfile` + `docker-compose.yml` run the API with LibreOffice and ffmpeg/ffprobe
included (staging lives in the `staging` volume): `docker compose up -d --build` (reads `.env`; listens on 127.0.0.1:8000).

The same compose file also runs the **admin dashboard** (`dashboard/Dockerfile`, reads `dashboard/.env`,
listens on 127.0.0.1:8501). Inside Docker the dashboard calls the API at `http://api:8000`; the browser
uses `PUBLIC_BASE_URL` from `.env` (empty = `http://localhost:8000`; on the server `https://api.nutriblend.co.in`).
Run either Docker or the plain `uvicorn`/`streamlit` commands, not both — they use the same ports.

See **PROJECT_SETUP.md** for the full guide — Atlas setup, SMS provider setup,
every environment variable, and what's still on the to-do list.

## Admin dashboard

A separate Streamlit app lives in `dashboard/` (own venv and requirements; talks to
this API over HTTP only). See **dashboard/README.md**.
