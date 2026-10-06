"""
api/auth.py — Sign-in flow (mobile number + OTP), token refresh and logout.
"""

from __future__ import annotations

import logging

from dashboard.api.client import ApiClient
from dashboard.api.errors import ApiError
from dashboard.config import settings
from dashboard.models import LoginResult, OtpSent

logger = logging.getLogger("dashboard.api.auth")


def request_otp(client: ApiClient, mobile_number: str) -> OtpSent:
    """Send a dashboard OTP (POST /admin/auth/signin). Non-admins get Forbidden here."""
    data = client.post(settings.api_path("/admin/auth/signin"), auth=False, json={"mobile_number": mobile_number})
    return OtpSent.model_validate(data)


def resend_otp(client: ApiClient, mobile_number: str) -> OtpSent:
    data = client.post(settings.api_path("/admin/auth/resend-otp"), auth=False, json={"mobile_number": mobile_number})
    return OtpSent.model_validate(data)


def verify_otp(client: ApiClient, mobile_number: str, otp: str) -> LoginResult:
    """Verify the OTP (shared POST /auth/verify-otp). Because the OTP came from the
    admin sign-in, the server returns an admin session. Tokens are stored on the
    client's TokenStore."""
    data = client.post(
        settings.api_path("/auth/verify-otp"),
        auth=False,
        json={"mobile_number": mobile_number, "otp": otp},
    )
    result = LoginResult.model_validate(data)
    client.tokens.set_tokens(result.access_token, result.refresh_token, result.expires_in)
    return result


def logout(client: ApiClient) -> None:
    """Revoke this session on the server (best effort) and forget the tokens locally."""
    refresh_token = client.tokens.get_refresh()
    try:
        if refresh_token:
            client.post(settings.api_path("/auth/logout"), auth=False, json={"refresh_token": refresh_token})
    except ApiError as exc:
        # Logging out must always succeed locally, even if the server is down.
        logger.warning("Server-side logout failed: %r", exc)
    finally:
        client.tokens.clear()
