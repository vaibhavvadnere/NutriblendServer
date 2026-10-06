"""
pages/login.py — Sign in with mobile number + OTP.

Two steps, both on this page:
  1. Enter mobile number  -> POST /admin/auth/signin (admin accounts only; anyone
                             else is refused here, before an OTP is sent)
  2. Enter OTP            -> POST /auth/verify-otp (shared with the app; returns an
                             admin session because the OTP came from step 1)

Safety net: right after sign-in the page calls GET /admin/me and signs out
if it answers 403.
"""

from __future__ import annotations

import time

import streamlit as st

from dashboard.api import admin as admin_api
from dashboard.api import auth as auth_api
from dashboard.api.errors import ApiError, Forbidden, NetworkError, RateLimited
from dashboard.auth import session
from dashboard.config import settings
from dashboard.models import OtpSent
from dashboard.utils.formatting import clean_mobile, mask_mobile

# Session keys for the in-progress login (not the signed-in state).
_STEP = "login.step"          # "mobile" | "otp"
_MOBILE = "login.mobile"
_SENT = "login.otp_sent"      # OtpSent
_SENT_AT = "login.sent_at"    # time.time() when the OTP was sent


def _reset() -> None:
    for key in (_STEP, _MOBILE, _SENT, _SENT_AT):
        st.session_state.pop(key, None)


def _show_error(exc: ApiError) -> None:
    if isinstance(exc, RateLimited) and exc.retry_after:
        st.error(f"{exc.message} Try again in {exc.retry_after}s.")
    elif isinstance(exc, NetworkError):
        st.error(exc.message, icon=":material/cloud_off:")
    else:
        st.error(exc.message)


def _otp_sent(sent: OtpSent, mobile: str) -> None:
    st.session_state[_STEP] = "otp"
    st.session_state[_MOBILE] = mobile
    st.session_state[_SENT] = sent
    st.session_state[_SENT_AT] = time.time()


_COOLDOWN = "login.cooldown"   # (mobile, retry_after_seconds) after "please wait" on Send OTP


def _mobile_step() -> None:
    with st.form("login-mobile", border=False):
        mobile = st.text_input("Mobile number", placeholder="10-digit mobile number", max_chars=15, key="login.mobile_input")
        submitted = st.form_submit_button("Send OTP", type="primary", use_container_width=True)

    if submitted:
        st.session_state.pop(_COOLDOWN, None)
        mobile = clean_mobile(mobile)
        if len(mobile) != 10:
            st.error("Enter a valid 10-digit mobile number.")
            return
        try:
            with st.spinner("Sending OTP…"):
                sent = auth_api.request_otp(session.get_client(), mobile)
        except RateLimited as exc:
            if exc.retry_after and exc.retry_after <= 120:
                # Cooldown: an OTP was sent a moment ago and is still valid.
                st.session_state[_COOLDOWN] = (mobile, exc.retry_after)
            else:
                _show_error(exc)
                return
        except ApiError as exc:
            _show_error(exc)
            return
        else:
            _otp_sent(sent, mobile)
            st.rerun()

    cooldown = st.session_state.get(_COOLDOWN)
    if cooldown:
        mobile, wait = cooldown
        st.warning(
            f"An OTP was already sent to this number less than a minute ago, so a new one "
            f"can be requested in about {wait}s. That OTP is still valid for a few minutes. "
            "(Development: it is printed in the server terminal as [DEV OTP].)",
            icon=":material/hourglass_top:",
        )
        if st.button("Enter the OTP I already have", type="primary", use_container_width=True):
            st.session_state.pop(_COOLDOWN, None)
            _otp_sent(
                OtpSent(mobile_number=mask_mobile(mobile), expires_in_minutes=5, resend_available_in_seconds=wait),
                mobile,
            )
            st.rerun()


def _otp_step() -> None:
    sent: OtpSent = st.session_state[_SENT]
    mobile: str = st.session_state[_MOBILE]

    st.info(
        f"OTP sent to **{sent.mobile_number}**. It is valid for {sent.expires_in_minutes} minutes.",
        icon=":material/sms:",
    )
    if sent.dev_otp:
        st.caption(f"Development server — OTP is **{sent.dev_otp}**")

    with st.form("login-otp", border=False):
        otp = st.text_input("OTP", max_chars=10, placeholder="Enter the code")
        submitted = st.form_submit_button("Verify & sign in", type="primary", use_container_width=True)

    if submitted:
        otp = otp.strip()
        if not otp.isdigit():
            st.error("The OTP contains digits only.")
            return
        client = session.get_client()
        try:
            with st.spinner("Verifying…"):
                result = auth_api.verify_otp(client, mobile, otp)
                admin_api.confirm_admin(client)
        except Forbidden as exc:
            # Show the server's reason (not an admin / wrong session type) as-is.
            auth_api.logout(client)
            _reset()
            st.session_state["flash"] = f"Signed out: {exc.message}"
            st.rerun()
        except ApiError as exc:
            client.tokens.clear()
            _show_error(exc)
            return
        session.set_current_user(result.user)
        _reset()
        st.rerun()

    wait = int(sent.resend_available_in_seconds - (time.time() - st.session_state[_SENT_AT]))
    left, right = st.columns(2)
    with left:
        if st.button("Change number", use_container_width=True):
            _reset()
            st.rerun()
    with right:
        label = "Resend OTP" if wait <= 0 else f"Resend in {wait}s"
        if st.button(label, disabled=wait > 0, use_container_width=True):
            try:
                sent = auth_api.resend_otp(session.get_client(), mobile)
            except ApiError as exc:
                _show_error(exc)
                return
            _otp_sent(sent, mobile)
            st.rerun()


def render() -> None:
    _, center, _ = st.columns([1, 2, 1])
    with center:
        st.title(settings.APP_TITLE)
        st.caption("Sign in with your mobile number")

        flash = st.session_state.pop("flash", None)
        if flash:
            st.warning(flash)

        if st.session_state.get(_STEP) == "otp":
            _otp_step()
        else:
            _mobile_step()
