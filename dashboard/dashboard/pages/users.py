"""
pages/users.py — Searchable, filterable, paginated list of app users.

Data: GET /api/v1/users (admins are never listed). Click a row to open the
User details page.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import navigation
from dashboard.api import admin as admin_api
from dashboard.auth import session
from dashboard.components.layout import api_errors, page_header
from dashboard.utils.formatting import fmt_datetime

_STATUS_OPTIONS = {"All statuses": None, "Active": "active", "Pending": "pending", "Blocked": "blocked"}
_PAGE_SIZES = [20, 50, 100]

_PAGE = "users.page"
_FILTER_SIG = "users.filters"


def _filters_row() -> tuple[str, str | None, int]:
    q_col, status_col, size_col = st.columns([3, 1.2, 1])
    query = q_col.text_input(
        "Search", key="users.q", placeholder="Name, email or part of a mobile number",
        label_visibility="collapsed", max_chars=60,
    )
    status_label = status_col.selectbox("Status", list(_STATUS_OPTIONS), key="users.status", label_visibility="collapsed")
    page_size = size_col.selectbox(
        "Rows per page", _PAGE_SIZES, key="users.size", label_visibility="collapsed",
        format_func=lambda n: f"{n} per page",
    )
    return query.strip(), _STATUS_OPTIONS[status_label], page_size


def render() -> None:
    page_header("Users", "App users only — admin accounts are not listed.")

    query, status, page_size = _filters_row()

    # Any filter change goes back to page 1.
    signature = (query, status, page_size)
    if st.session_state.get(_FILTER_SIG) != signature:
        st.session_state[_FILTER_SIG] = signature
        st.session_state[_PAGE] = 1
    page = st.session_state.get(_PAGE, 1)

    with api_errors("load users"):
        result = admin_api.list_users(session.get_client(), query or None, status, page, page_size)

    if result.page > result.pages:  # e.g. the last user on the last page was blocked away
        st.session_state[_PAGE] = result.pages
        st.rerun()

    if result.total == 0:
        st.info("No users match these filters." if (query or status) else "No users yet.", icon=":material/person_search:")
        return

    df = pd.DataFrame(
        [
            {
                "id": u.id,
                "Name": u.name,
                "Mobile": u.mobile_number,
                "Email": u.email or "—",
                "State": u.state or "—",
                "Status": u.status.title(),
                "Joined": fmt_datetime(u.created_at, with_time=False),
                "Last sign-in": fmt_datetime(u.last_login_at),
            }
            for u in result.items
        ]
    )

    event = st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_order=["Name", "Mobile", "Email", "State", "Status", "Joined", "Last sign-in"],
        height=(len(df) + 1) * 35 + 3,  # show the whole page, no inner scrolling
        on_select="rerun",
        selection_mode="single-row",
        key=f"users.table.{page}.{hash(signature)}",
    )
    st.caption("Select a row to open the user's details.")

    rows = event.selection.rows if event else []
    if rows:
        st.switch_page(navigation.pages().user_detail, query_params={"id": df.iloc[rows[0]]["id"]})

    # ── Pagination ────────────────────────────────────────────────────────────
    first = (result.page - 1) * result.page_size + 1
    last = first + len(result.items) - 1
    info, prev_col, next_col = st.columns([4, 1, 1], vertical_alignment="center")
    info.caption(f"Showing {first:,}–{last:,} of {result.total:,} users · page {result.page} of {result.pages}")
    if prev_col.button("Previous", icon=":material/chevron_left:", disabled=result.page <= 1, width="stretch"):
        st.session_state[_PAGE] = result.page - 1
        st.rerun()
    if next_col.button("Next", icon=":material/chevron_right:", disabled=result.page >= result.pages, width="stretch"):
        st.session_state[_PAGE] = result.page + 1
        st.rerun()
