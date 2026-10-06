"""
pages/videos.py — All videos (drafts and unfinished uploads included).

Data: GET /admin/videos. Click a row to open Video details.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import navigation
from dashboard.api import videos as videos_api
from dashboard.auth import session
from dashboard.components.layout import api_errors, page_header
from dashboard.utils.formatting import fmt_bytes, fmt_datetime, fmt_duration

_STATUS = {"All statuses": None, "Published": "published", "Draft": "draft", "Uploading": "uploading"}
_ALL = "All categories"
_PAGE = "videos.page"
_SIG = "videos.filters"


def _status_text(v) -> str:
    if v.status == "uploading" and v.upload:
        return f"Uploading ({v.upload.fraction:.0%})"
    return v.status.title()


def render() -> None:
    head, action = st.columns([4, 1], vertical_alignment="bottom")
    with head:
        page_header("Videos", "Everything you've uploaded. Only published videos appear in the app.")
    if action.button("Upload video", icon=":material/upload:", type="primary", use_container_width=True):
        st.switch_page(navigation.pages().video_upload)

    flash = st.session_state.pop("videos.flash", None)
    if flash:
        st.success(flash)

    client = session.get_client()
    with api_errors("load categories"):
        categories = videos_api.categories(client)

    q_col, status_col, cat_col = st.columns([3, 1.2, 1.4])
    query = q_col.text_input("Search", key="videos.q", placeholder="Title, description or category",
                             label_visibility="collapsed", max_chars=60).strip()
    status = _STATUS[status_col.selectbox("Status", list(_STATUS), key="videos.status", label_visibility="collapsed")]
    cat_choice = cat_col.selectbox("Category", [_ALL, *categories], key="videos.category", label_visibility="collapsed")
    category = None if cat_choice == _ALL else cat_choice

    signature = (query, status, category)
    if st.session_state.get(_SIG) != signature:
        st.session_state[_SIG] = signature
        st.session_state[_PAGE] = 1
    page = st.session_state.get(_PAGE, 1)

    with api_errors("load videos"):
        result = videos_api.list_videos(client, query or None, status, category, page, 20)

    if result.page > result.pages:
        st.session_state[_PAGE] = result.pages
        st.rerun()
    if result.total == 0:
        st.info("No videos match these filters." if any(signature) else "No videos yet — upload your first one.",
                icon=":material/video_library:")
        return

    df = pd.DataFrame(
        [
            {
                "id": v.id,
                "Title": v.title,
                "Category": v.category or "—",
                "Status": _status_text(v),
                "Duration": fmt_duration(v.duration_seconds),
                "Size": fmt_bytes(v.file_size),
                "Document": (v.admin_document.file_type.upper() if v.admin_document else "—"),
                "Order": v.sort_order,
                "Added": fmt_datetime(v.created_at, with_time=False),
            }
            for v in result.items
        ]
    )
    event = st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
        column_order=["Title", "Category", "Status", "Document", "Duration", "Size", "Order", "Added"],
        height=(len(df) + 1) * 35 + 3,
        on_select="rerun",
        selection_mode="single-row",
        key=f"videos.table.{page}.{hash(signature)}",
    )
    st.caption("Select a row to preview, edit, publish or delete the video.")
    if event and event.selection.rows:
        st.switch_page(navigation.pages().video_detail, query_params={"id": df.iloc[event.selection.rows[0]]["id"]})

    first = (result.page - 1) * result.page_size + 1
    info, prev_col, next_col = st.columns([4, 1, 1], vertical_alignment="center")
    info.caption(f"Showing {first}–{first + len(result.items) - 1} of {result.total} videos · page {result.page} of {result.pages}")
    if prev_col.button("Previous", icon=":material/chevron_left:", disabled=result.page <= 1, use_container_width=True, key="videos.prev"):
        st.session_state[_PAGE] = result.page - 1
        st.rerun()
    if next_col.button("Next", icon=":material/chevron_right:", disabled=result.page >= result.pages, use_container_width=True, key="videos.next"):
        st.session_state[_PAGE] = result.page + 1
        st.rerun()
