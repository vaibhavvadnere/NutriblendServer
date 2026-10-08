"""
pages/user_detail.py — One user's profile, sessions, and block / unblock.

Opened from the Users table with ?id=<user id>.
Data: GET /admin/users/{id}; action: PATCH /admin/users/{id}/status.
"""

from __future__ import annotations

import streamlit as st

from dashboard import navigation
from dashboard.api import admin as admin_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components.layout import api_errors
from dashboard.models import UserDetail
from dashboard.utils.formatting import fmt_datetime

# Status colors carry a text label too, never color alone.
_STATUS_BADGE = {
    "active": ("Active", "green", ":material/check_circle:"),
    "pending": ("Pending", "orange", ":material/schedule:"),
    "blocked": ("Blocked", "red", ":material/block:"),
}


def _back() -> None:
    if st.button("Back to users", icon=":material/arrow_back:"):
        st.switch_page(navigation.pages().users)


def _field(col, label: str, value: str) -> None:
    col.caption(label)
    col.markdown(value if value else "—")


@st.dialog("Block this user?")
def _confirm_block(user: UserDetail) -> None:
    st.write(
        f"**{user.name}** ({user.mobile_number}) will be signed out on every device "
        "and won't be able to sign in until you unblock them."
    )
    left, right = st.columns(2)
    if left.button("Cancel", width="stretch"):
        st.rerun()
    if right.button("Block user", type="primary", icon=":material/block:", width="stretch"):
        _change_status(user, "blocked")


def _change_status(user: UserDetail, new_status: str) -> None:
    try:
        updated = admin_api.set_user_status(session.get_client(), user.id, new_status)
    except ApiError as exc:
        st.session_state["user_detail.flash"] = ("error", exc.message)
    else:
        verb = "blocked" if new_status == "blocked" else "unblocked"
        st.session_state["user_detail.flash"] = ("success", f"{updated.name} is {verb} (status: {updated.status}).")
    st.rerun()


def render() -> None:
    _back()

    user_id = st.query_params.get("id")
    if not user_id:
        st.info("Pick a user from the Users page.", icon=":material/person_search:")
        return

    with api_errors("load this user"):
        user = admin_api.get_user(session.get_client(), user_id)

    flash = st.session_state.pop("user_detail.flash", None)
    if flash:
        kind, text = flash
        (st.success if kind == "success" else st.error)(text)

    label, color, icon = _STATUS_BADGE.get(user.status, (user.status.title(), "gray", None))
    st.title(user.name)
    badges = st.container(horizontal=True)
    with badges:
        st.badge(label, color=color, icon=icon)
        if user.is_admin:
            st.badge("Admin", color="violet", icon=":material/shield_person:")

    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        _field(c1, "Mobile number", user.mobile_number)
        _field(c2, "Email", user.email or "")
        _field(c3, "State", user.state or "")
        c1, c2, c3 = st.columns(3)
        _field(c1, "Joined", fmt_datetime(user.created_at))
        _field(c2, "Verified", fmt_datetime(user.verified_at))
        _field(c3, "Last sign-in", fmt_datetime(user.last_login_at))
        c1, c2, c3 = st.columns(3)
        _field(c1, "Signed in on", f"{user.active_sessions} device{'s' if user.active_sessions != 1 else ''}")
        _field(c2, "Role", user.role.title())
        _field(c3, "User ID", f"`{user.id}`")

    st.subheader("Access")
    if user.is_admin:
        st.caption("Admin accounts can't be blocked from the dashboard.")
    elif user.status == "blocked":
        st.caption("This user is blocked and can't sign in.")
        if st.button("Unblock user", icon=":material/lock_open:", type="primary"):
            _change_status(user, "active")
    else:
        st.caption("Blocking signs the user out everywhere and stops them signing in.")
        if st.button("Block user", icon=":material/block:"):
            _confirm_block(user)
