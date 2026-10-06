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

Sign up and sign in both end at `verify-otp`, so the app needs one OTP screen.
Every response uses one envelope, and HTTP status codes stay meaningful:

- Success: `{"success": true, "message": "...", "data": {...}}`
- Failure: `{"success": false, "message": "...", "error": {"code": "...", "details": {...}}}` — branch on `error.code`.

## Quick start

```bash
.venv/bin/python3 -m pip install -r requirements.txt
.venv/bin/python3 scripts/check_db.py          # verify MONGO_URI in .env
.venv/bin/python3 -m uvicorn app.main:app --reload
```

Interactive docs: http://localhost:8000/docs

See **PROJECT_SETUP.md** for the full guide — Atlas setup, SMS provider setup,
every environment variable, and what's still on the to-do list.
