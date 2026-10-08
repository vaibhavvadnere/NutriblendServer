"""
pages/overview.py — Dashboard home: headline numbers and signups over time.

Data: GET /admin/stats (admins are excluded server-side) and GET /api/health.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.api import admin as admin_api
from dashboard.api import system as system_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components.charts import daily_counts_bar
from dashboard.components.layout import api_errors, page_header
from dashboard.config import settings


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%} of all users" if whole else "—"


def render() -> None:
    user = session.current_user()
    page_header("Dashboard", f"Welcome, {user.name}" if user else None)

    client = session.get_client()
    with api_errors("load the statistics"):
        stats = admin_api.get_stats(client)

    total = stats.total_users
    by_status = stats.users_by_status

    # ── Headline numbers ──────────────────────────────────────────────────────
    st.subheader("Users")
    c1, c2, c3, c4 = st.columns(4, border=True)
    c1.metric("Total users", f"{total:,}")
    c2.metric("Active", f"{by_status.active:,}", help="Verified their number and can sign in.")
    c2.caption(_pct(by_status.active, total))
    c3.metric("Pending", f"{by_status.pending:,}", help="Signed up but never verified the OTP. Removed automatically after 24 hours.")
    c3.caption(_pct(by_status.pending, total))
    c4.metric("Blocked", f"{by_status.blocked:,}", help="Barred from signing in.")
    c4.caption(_pct(by_status.blocked, total))

    st.subheader("Activity")
    a1, a2, a3 = st.columns(3, border=True)
    a1.metric("Signups today", f"{stats.signups_today:,}")
    a2.metric("Signups, last 7 days", f"{stats.signups_last_7_days:,}")
    a3.metric("Signed in, last 7 days", f"{stats.active_last_7_days:,}", help="Users whose last sign-in was in the last 7 days.")

    if stats.videos:
        st.subheader("Videos")
        v1, v2, v3 = st.columns(3, border=True)
        v1.metric("Published", f"{stats.videos.get('published', 0):,}", help="Visible in the app.")
        v2.metric("Drafts", f"{stats.videos.get('draft', 0):,}", help="Uploaded but hidden from the app.")
        v3.metric("Uploads in progress", f"{stats.videos.get('uploading', 0):,}", help="Unfinished uploads can be resumed from the Videos page.")

    # ── Signups per day ───────────────────────────────────────────────────────
    df = pd.DataFrame([{"date": pd.Timestamp(d.date), "count": d.count} for d in stats.signups_by_day])
    days = len(df)
    st.subheader(f"Signups per day — last {days} days")
    if df["count"].sum() == 0:
        st.info(f"No signups in the last {days} days.", icon=":material/insights:")
    else:
        st.altair_chart(daily_counts_bar(df, "Signups"), width="stretch")
        busiest = df.loc[df["count"].idxmax()]
        st.caption(
            f"{int(df['count'].sum()):,} signups in {days} days · busiest day "
            f"{busiest['date']:%d %b} ({int(busiest['count'])}) · days in {stats.timezone}"
        )
    with st.expander("Show as table"):
        table = df.sort_values("date", ascending=False).assign(date=lambda d: d["date"].dt.strftime("%a, %d %b %Y"))
        st.dataframe(
            table.rename(columns={"date": "Date", "count": "Signups"}),
            hide_index=True,
            width="stretch",
        )

    # ── Server ────────────────────────────────────────────────────────────────
    try:
        health = system_api.health(client)
        db = "OK" if health.database == "ok" else "unavailable"
        st.caption(
            f"Server {settings.server_url} · {health.status} · database {db} · "
            f"{health.env} · SMS provider: {health.sms_provider}"
        )
    except ApiError as exc:
        st.caption(f"Server health unavailable: {exc.message}")
