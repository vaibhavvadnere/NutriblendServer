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

## Tests

```bash
.venv/bin/python -m pytest
```

No server or database needed — the tests use a fake server.

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
