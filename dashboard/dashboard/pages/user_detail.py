"""
pages/user_detail.py — One user's profile, sessions, block / unblock, and
subscription (grant / revoke access, payments).

Opened from the Users or Payments table with ?id=<user id>.
Data: GET /admin/users/{id}, GET /admin/users/{id}/subscription, GET /admin/payments?user_id=;
actions: PATCH /admin/users/{id}/status, POST /admin/users/{id}/subscription/grant|revoke.
"""

from __future__ import annotations

import streamlit as st

from dashboard import navigation
from dashboard.api import admin as admin_api
from dashboard.api import payments as payments_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components.layout import api_errors
from dashboard.models import UserDetail
from dashboard.utils.formatting import fmt_datetime, fmt_rupees
from pydantic import ValidationError

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


@st.dialog("End this user's access?")
def _confirm_revoke(user: UserDetail) -> None:
    st.write(f"**{user.name}** loses access to paid content right away. This does **not** refund money — "
             "refunds are made in the Razorpay dashboard.")
    note = st.text_input("Reason (optional)", max_chars=200, key="user_detail.revoke_note")
    left, right = st.columns(2)
    if left.button("Cancel", width="stretch"):
        st.rerun()
    if right.button("End access", type="primary", icon=":material/remove_circle:", width="stretch"):
        _sub_action(lambda c: payments_api.revoke(c, user.id, note.strip() or None), "Access ended.")


def _sub_action(call, success: str) -> None:
    try:
        call(session.get_client())
    except ApiError as exc:
        st.session_state["user_detail.flash"] = ("error", exc.message)
    else:
        st.session_state["user_detail.flash"] = ("success", success)
    st.rerun()


_HISTORY_ACTION = {"extend": "Added", "refund": "Refund", "end": "Ended"}


def _subscription_section(user: UserDetail) -> None:
    st.subheader("Subscription")
    try:
        data = payments_api.get_user_subscription(session.get_client(), user.id)
        orders = payments_api.list_payments(session.get_client(), user_id=user.id, page_size=10)
    except (ApiError, ValidationError) as exc:
        # Older server without payments, or a hiccup: the rest of the page still works.
        st.caption(f"Subscription details unavailable ({getattr(exc, 'message', 'unexpected reply')}).")
        return

    sub = data.subscription
    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        if sub.is_active:
            c1.badge("Active", color="green", icon=":material/workspace_premium:")
        else:
            c1.badge("No access", color="gray", icon=":material/lock:")
        _field(c2, "Plan", sub.plan_name or "")
        _field(c3, "Valid till", fmt_datetime(sub.expires_at) if sub.expires_at else "")
        if sub.is_active:
            st.caption(f"{sub.days_left} day{'s' if sub.days_left != 1 else ''} left · since {fmt_datetime(sub.started_at, with_time=False)}")
        elif data.ended_reason:
            st.caption(f"Ended early: {data.ended_reason.replace('_', ' ')}")

    with st.form("user_detail.grant", clear_on_submit=True, border=True):
        st.markdown("**Give access without a payment** (dealer bundle, support)")
        g1, g2 = st.columns([1, 3])
        days = g1.number_input("Days", min_value=1, max_value=3660, value=30, step=1)
        note = g2.text_input("Note (shown in history)", max_chars=200, placeholder="e.g. Bought 10 bags at Pune dealer")
        st.caption("Adds to any access the user already has.")
        if st.form_submit_button("Give access", icon=":material/add_circle:"):
            _sub_action(lambda c: payments_api.grant(c, user.id, int(days), note.strip() or None),
                        f"Added {int(days)} days of access.")
    if sub.is_active and st.button("End access now", icon=":material/remove_circle:"):
        _confirm_revoke(user)

    if orders.items:
        st.markdown("**Payments**")
        st.dataframe(
            [{"Date": fmt_datetime(o.paid_at or o.created_at), "Plan": o.plan_name, "Amount": fmt_rupees(o.amount),
              "Status": o.status.title() if o.status != "created" else "Started", "Razorpay payment": o.razorpay_payment_id or "—"}
             for o in orders.items],
            hide_index=True, width="stretch",
        )
    if data.history:
        with st.expander(f"History ({len(data.history)})"):
            st.dataframe(
                [{"When": fmt_datetime(h.at), "Change": _HISTORY_ACTION.get(h.action, h.action),
                  "Days": f"{h.days:+d}" if h.days is not None else "—",
                  "By": h.by or ("Razorpay" if h.source == "payment" else "—"),
                  "Details": h.note or h.reason or h.order_id or ""}
                 for h in data.history],
                hide_index=True, width="stretch",
            )


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

    if not user.is_admin:
        _subscription_section(user)
