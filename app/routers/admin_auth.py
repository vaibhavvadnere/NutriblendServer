"""
routers/admin_auth.py — Dashboard sign-in (admin sessions).

    POST /admin/auth/signin      { mobile_number } -> OTP sent (admin accounts only)
    POST /admin/auth/resend-otp  { mobile_number } -> same, for the "Resend" button

Only the first step is separate. The rest is SHARED with the mobile app:

    POST /auth/verify-otp  -> issues an ADMIN session, because the stored OTP was
                              requested here (its purpose is "admin")
    POST /auth/refresh     -> an admin session stays an admin session
    POST /auth/logout

Admin sessions work only on /admin endpoints (and GET /users); app sessions
work only on app endpoints. See core/deps.py.
"""

from fastapi import APIRouter, Request

from app.core.config import settings
from app.core.deps import client_ip
from app.schemas.auth import OTPSentResponse, SigninRequest
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.services import auth_service

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/admin/auth", tags=["admin auth"], responses=ERROR_RESPONSES
)


@router.post("/signin", response_model=ApiResponse[OTPSentResponse])
async def admin_signin(payload: SigninRequest, request: Request):
    """
    Step 1 of the dashboard login: send an OTP to an **admin** account.
    Then call the shared `POST /auth/verify-otp`, which returns a dashboard session
    (`scope: "admin"`).

    - `404 ACCOUNT_NOT_FOUND` — no account with this number.
    - `403 FORBIDDEN` — the account is not an admin (no OTP is sent).
    - `403 ACCOUNT_BLOCKED` / `429 RATE_LIMITED` — as for the app sign-in.
    """
    return ok(await auth_service.start_admin_signin(payload.mobile_number, client_ip(request)), "OTP sent successfully")


@router.post("/resend-otp", response_model=ApiResponse[OTPSentResponse])
async def admin_resend_otp(payload: SigninRequest, request: Request):
    """Resend the dashboard OTP (same checks and cooldown as sign-in)."""
    return ok(await auth_service.start_admin_signin(payload.mobile_number, client_ip(request)), "OTP sent successfully")
