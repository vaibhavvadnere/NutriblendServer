# Nutriblend Admin Dashboard

A small Streamlit app for the Nutriblend admin (you). It is **independent of the
server**: its own virtual env, its own `requirements.txt`, its own `.env`. It talks
to the Nutriblend API over HTTP only and never imports anything from `app/`, so this
folder can be moved to its own repo at any time.

## Run it

Start the API server first (from the repo root), then in a second terminal:

```bash
cd dashboard
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env              # already done once; edit SERVER_URL if needed
.venv/bin/streamlit run main.py   # http://localhost:8501
```

Sign in with your mobile number + OTP. With the server in development mode and
`SMS_PROVIDER=mock`, the OTP is shown on the screen (and in the server log).

## Uploading videos

Uploads run in your browser, so they work from any computer and for any file
size: drag a file into the box on **Upload video** (or Browse) → it is
fingerprinted ("Checking file…") → **Upload**. The file is handed to the
**Uploads** panel in the sidebar and the form clears — you can start another
upload or move to any other page while it uploads (keep the browser tab open).
The panel shows speed / time left with **Stop**, and **Retry** / **Dismiss**
if something goes wrong. When it finishes — whatever page is open — the
thumbnail is saved, the video is published if you asked, and a note appears in
the sidebar (and on the Upload page).

Pieces go straight to storage, 4 at a time; documents (PDF, Word, PowerPoint)
go the same way, from the Upload page ("Attach a document") or a video's page.
An interrupted upload can be resumed from the video's page (choose the same
file — only the missing parts are sent). Files are never read from the disk of
the computer the dashboard runs on.

**Checks before upload.** The moment you choose a video, the box reads its
header in the browser (milliseconds, even for 4 GB; nothing is uploaded) and
shows e.g. `H.264 High · 1920×1080 · 12:34 · fast-start`. It warns — without
blocking — when the video: isn't H.264 (HEVC/MPEG-4 won't play on many Android
phones), is 10-bit / 4:2:2, has non-AAC sound, has a very high bitrate, or
isn't *fast-start* (index at the end, so playback can't begin until the whole
file downloads; fix: `ffmpeg -i in.mp4 -c copy -movflags +faststart out.mp4`).
Files that look broken (not an MP4, cut short, no index, no picture) need a
"Upload it anyway" tick. The length and codec it read are saved with the video
until the server can probe the file itself (ffprobe), which then takes over.
Code: `components/browser_upload/probe.js` (tests: `tests/test_probe_js.py`, needs node + ffmpeg).

**Publishing and documents.** A video with a document goes live only when the
document is ready. "Publish right after upload" (and the **Publish** button)
therefore wait for it: the video stays a draft marked *publishes when ready* and
goes live by itself; **Cancel** keeps it a draft. A failed document blocks
publishing until you retry or remove it. A published video whose document
isn't ready is shown as *Published · hidden* and is not visible in the app.

**Cover picture.** The browser also takes a frame from the video (10% in,
`frame.js`; decoded locally, nothing uploaded) and shows it in the file box. If
you don't choose a thumbnail, that picture is saved as the cover when the
upload finishes. If the browser can't decode the video (some HEVC files), no
cover is made — upload isn't affected; add a thumbnail on the video's page.

**Title and category checks.** While you type, the page asks the server whether
another video has the same title (capitals and extra spaces ignored) and
whether a new category looks like a typo of an existing one. A same title needs
the tick *Use the same title anyway*; a new-looking category offers *Use “Recipes”*
or the tick *… is correct — create it as a new category*. A category that differs
from an existing one only by capitals is saved with the existing spelling.
The same checks run when you edit a video's details.

**Replacing the video file.** On a video's page, *Replace video file* uploads a
new file for the same record. Title, cover, document, category, order, status
and app links stay; the old file keeps playing until the new one is complete
and verified, then it is swapped in and the old file is deleted from storage.
Same file again resumes the unfinished replacement; a different file discards
it (or use *Discard the unfinished replacement*). The same checks (duplicate,
broken-file gate) apply.

Code: `dashboard/components/browser_upload/` (mode "box" on pages, mode
"manager" in the sidebar; they pass the file over a BroadcastChannel) and
`dashboard/services/uploads.py` (tasks, follow-ups, results).

**Automatic optimization.** After an upload the server converts the video into a much smaller file and
stores only that (the original is not kept). The Upload page says so (or warns that ffmpeg is missing on the
server). The video stays a draft showing **Optimizing N%**; its page has a progress panel (**Retry
optimization** after a failure, or **Discard the new file**), no preview until it is done, and then
*Optimized: X MB uploaded → Y MB stored (N% smaller)*. "Publish right after upload" waits and publishes by
itself when done. Replace video file works the same way (the old file keeps playing until the swap).

**If the connection is slow.** Page refreshes talk to the server, so on a slow network they can take seconds. The
Upload button remembers a click even if the page restarts (e.g. you edit a field right after clicking) and
the upload also starts by itself if you click before "Checking file…" has finished. Small lookups
(categories, title check, optimization status) are cached for 20–60 s per session
(`dashboard/services/memo.py`). Stay on the page until the upload shows in **Uploads**; if you leave too
early, open the video from Videos and **Resume upload** with the same file.

Settings (`.env`): `PUBLIC_SERVER_URL` — the API address as your *browser*
reaches it (empty = `SERVER_URL`); `MAX_VIDEO_SIZE_MB` (default 4096).
The R2 bucket needs a CORS policy for the dashboard's address (see the server README).

**Signed in across refreshes.** A browser refresh keeps you signed in; closing the tab (or the browser) and opening the
dashboard again asks for the OTP. The browser keeps only a random key in `sessionStorage`; the tokens stay inside the
dashboard process (`dashboard/auth/remember.py`, `components/session_keeper`). The key stops working after
`SESSION_REMEMBER_HOURS` (12) without use, on Log out, and when the dashboard restarts. `0` turns it off. Refreshing
still stops an upload in progress.

## Tests

```bash
.venv/bin/python -m pytest
```

No server or database needed — the tests use a fake server (127 tests).

## Layout

```
dashboard/
├── main.py                  entry point: login gate + navigation
├── .env / .env.example      SERVER_URL, API_PREFIX, timeouts, timezone
├── .streamlit/config.toml   theme, upload size, no tracebacks in the browser
├── dashboard/
│   ├── config.py            settings from .env
│   ├── models.py            typed API payloads (User, OtpSent, ...)
│   ├── api/
│   │   ├── client.py        the ONLY HTTP code: tokens, auto-refresh, envelope, errors
│   │   ├── errors.py        ApiError, NetworkError, SessionExpired, Forbidden, ...
│   │   ├── auth.py          signin / resend / verify-otp / logout
│   │   ├── admin.py         /admin/me, stats, users, user detail, block/unblock
│   │   ├── videos.py        /admin/videos: list, upload chunks, edit, publish, thumbnail, delete
│   │   └── system.py        /api/health
│   ├── navigation.py        signed-in pages (Dashboard, Users, Videos, Upload video + hidden detail pages)
│   ├── services/uploader.py resumable chunked upload (retries, resume, progress)
│   ├── services/memo.py     tiny per-session cache for repeated lookups
│   ├── auth/session.py      tokens + user in st.session_state (memory only)
│   ├── components/          layout.py, charts.py (Altair), video_ui.py (badges, pickers, progress)
│   ├── pages/               one module per screen, each with render()
│   └── utils/               formatting, logging
└── tests/
```

**Rules of thumb**

- Pages never call `httpx`; they call functions in `dashboard/api/`.
- Wrap API calls in a page with `with api_errors("load users"):` for consistent messages.
- Add a new screen: create `dashboard/pages/<name>.py` with `render()`, then register it
  in `main.py` (signed-in section).

## Security

- Tokens live only in the browser session's memory; refreshing the tab signs you out.
- **Admin-only access is enforced by the server**: only numbers listed in the server's
  `ADMIN_MOBILE_NUMBERS` (.env) can call `/api/v1/admin/*`. Right after OTP the
  dashboard calls `/admin/me` and signs any other number straight back out.
- Streamlit listens on `localhost` only (`.streamlit/config.toml`), so other devices
  on your network cannot open it.

## Roadmap

1. ✅ Scaffold: config, API client, session, login, dashboard shell
2. ✅ Server: `ADMIN_MOBILE_NUMBERS` + `require_admin`, admin stats & users endpoints
3. ✅ Dashboard (stat tiles + signups chart), Users (search, filter, pages), User details (block / unblock)
4. ✅ Server: video storage + endpoints (chunked, resumable; local folder for now)
5. ✅ Videos list, Upload video (from a path on this Mac — any size — or browser upload up to 500 MB),
   Video details (preview, publish/unpublish, edit, thumbnail, resume an interrupted upload, delete)
6. ✅ One view-only document per video (PDF / Word / PowerPoint): attach on Upload, preview pages,
   replace, remove, retry on the video's page
