"""
pages/videos.py — All videos (drafts and unfinished uploads included).

Data: GET /admin/videos. Click a row to open Video details.
"""

from __future__ import annotations

import streamlit as st

from dashboard import navigation
from dashboard.api import videos as videos_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components import video_table
from dashboard.components.layout import api_errors, page_header
from dashboard.utils.formatting import fmt_bytes, fmt_datetime, fmt_duration

_STATUS = {"All statuses": None, "Published": "published", "Draft": "draft", "Uploading": "uploading"}
_ALL = "All categories"
_PAGE = "videos.page"
_SIG = "videos.filters"
_TABLE = "videos.table_key"     # widget key of the table as last drawn


def _status_text(v, broken_ids: set[str] = frozenset()) -> str:
    if v.id in broken_ids:
        return f"{v.status.title()} · file missing"
    if v.status == "uploading" and v.upload:
        return f"Uploading ({v.upload.fraction:.0%})"
    if v.optimizing:
        text = "Draft · optimizing" + (f" {v.optimization.progress:.0%}" if v.optimization.state == "running" else "")
    elif v.optimization_failed:
        text = "Draft · optimization failed"
    elif v.hidden_in_app:
        text = "Published · hidden (document not ready)"
    elif v.publish_when_ready and v.status == "draft":
        text = "Draft · publishes when ready"
    else:
        text = v.status.title()
    if v.replacement:
        text += " · new file optimizing" if v.replacement.optimization else " · new file uploading"
    return text


def _tone(v, broken_ids: set[str] = frozenset()) -> str:
    """Colour of the dot in front of the status."""
    if v.id in broken_ids or v.optimization_failed:
        return "problem"
    if v.status == "uploading":
        return "uploading"
    if v.optimizing:
        return "optimizing"
    if v.status == "published" and not v.hidden_in_app:
        return "published"
    return "draft"


_CHECK = "videos.storage_check"     # last StorageCheck result (this session)
_MISSING_LABEL = {"video": "video file", "thumbnail": "thumbnail", "document": "document"}


@st.dialog("Delete videos with missing files?")
def _confirm_delete_broken(ids_titles: list[tuple[str, str]]) -> None:
    st.write(f"These {len(ids_titles)} video record(s) will be deleted permanently. "
             "Their files are already missing, so they can't be played anyway:")
    for _, title in ids_titles:
        st.markdown(f"- {title}")
    left, right = st.columns(2)
    if left.button("Cancel", width="stretch"):
        st.rerun()
    if right.button("Delete", type="primary", icon=":material/delete:", width="stretch"):
        delete_records(ids_titles)
        st.rerun()


def delete_records(ids_titles: list[tuple[str, str]]) -> None:
    """Delete the given video records; leaves a summary for the next run and forces a re-check."""
    client = session.get_client()
    failed = []
    for vid, title in ids_titles:
        try:
            videos_api.delete_video(client, vid)
        except ApiError as exc:
            failed.append(f"{title}: {exc.message}")
    st.session_state.pop(_CHECK, None)        # re-check on the next run
    st.session_state["videos.flash"] = (
        f"Deleted {len(ids_titles) - len(failed)} video record(s)." + (" Failed: " + "; ".join(failed) if failed else "")
    )


def _storage_check_panel(client) -> None:
    """Warn about videos whose files aren't in the server's storage (checked once per visit)."""
    if _CHECK not in st.session_state:
        try:
            with st.spinner("Checking video files in storage…"):
                st.session_state[_CHECK] = videos_api.storage_check(client)
        except ApiError as exc:
            st.caption(f"Storage check unavailable: {exc.message}")
            return
    result = st.session_state[_CHECK]
    if result.errors:
        st.warning(f"{result.errors} video(s) couldn't be checked — storage ({result.storage}) didn't respond. "
                   "Try again later.", icon=":material/cloud_off:")
    if not result.broken:
        return
    with st.container(border=True):
        st.error(
            f"{len(result.broken)} of {result.checked} videos have files missing from storage ({result.storage}). "
            "They can't be played or published. This usually means they were uploaded before the storage "
            "was switched (e.g. local → R2). Delete them and upload the videos again.",
            icon=":material/report:",
        )
        for b in result.broken:
            missing = ", ".join(_MISSING_LABEL.get(m, m) for m in b.missing_files)
            st.markdown(f"- **{b.title}** · {b.status} · added {fmt_datetime(b.created_at, with_time=False)} · missing: {missing}")
        left, right, _ = st.columns([1.3, 1, 2])
        if left.button(f"Delete {len(result.broken)} broken video(s)", type="primary", icon=":material/delete:",
                       width="stretch", key="videos.delete_broken"):
            _confirm_delete_broken([(b.id, b.title) for b in result.broken])
        if right.button("Check again", icon=":material/refresh:", width="stretch", key="videos.recheck"):
            st.session_state.pop(_CHECK, None)
            st.rerun()


_SEEN = "videos.table_seen"     # the last click already acted on


def _open_clicked_video() -> None:
    """A click on a row reruns the page. Open the video straight away, before the page asks the server for
    anything (on a slow connection that used to take seconds)."""
    key = st.session_state.get(_TABLE)
    event = st.session_state.get(key) if key else None
    if not isinstance(event, dict) or not event.get("id") or event.get("seq") == st.session_state.get(_SEEN):
        return
    st.session_state[_SEEN] = event["seq"]
    st.switch_page(navigation.pages().video_detail, query_params={"id": event["id"]})


def render() -> None:
    _open_clicked_video()
    head, action = st.columns([4, 1], vertical_alignment="bottom")
    with head:
        page_header("Videos", "Everything you've uploaded. Only published videos appear in the app.")
    if action.button("Upload video", icon=":material/upload:", type="primary", width="stretch"):
        st.switch_page(navigation.pages().video_upload)

    flash = st.session_state.pop("videos.flash", None)
    if flash:
        st.success(flash)

    client = session.get_client()
    if st.session_state.get("_nav_previous") != "videos":
        st.session_state.pop(_CHECK, None)        # fresh check each time you open this page
    _storage_check_panel(client)

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

    check = st.session_state.get(_CHECK)
    broken_ids = {b.id for b in check.broken} if check else set()
    rows = [
        {
            "id": v.id,
            "Title": v.title,
            "Category": v.category or "—",
            "Status": _status_text(v, broken_ids),
            "tone": _tone(v, broken_ids),
            "Duration": fmt_duration(v.duration_seconds),
            "Size": fmt_bytes(v.file_size),
            "Document": (v.admin_document.file_type.upper() if v.admin_document else "—"),
            "Order": v.sort_order,
            "Added": fmt_datetime(v.created_at, with_time=False),
            "sort": {"Duration": v.duration_seconds or 0, "Size": v.file_size or 0,
                     "Added": v.created_at.timestamp() if v.created_at else 0},
        }
        for v in result.items
    ]
    table_key = f"videos.table.{page}.{hash(signature)}"
    st.session_state[_TABLE] = table_key
    event = video_table.render(key=table_key, rows=rows)
    st.caption("Click a video to preview, edit, publish or delete it.")
    if isinstance(event, dict) and event.get("id") and event.get("seq") != st.session_state.get(_SEEN):
        st.session_state[_SEEN] = event["seq"]
        st.switch_page(navigation.pages().video_detail, query_params={"id": event["id"]})

    first = (result.page - 1) * result.page_size + 1
    info, prev_col, next_col = st.columns([4, 1, 1], vertical_alignment="center")
    info.caption(f"Showing {first}–{first + len(result.items) - 1} of {result.total} videos · page {result.page} of {result.pages}")
    if prev_col.button("Previous", icon=":material/chevron_left:", disabled=result.page <= 1, width="stretch", key="videos.prev"):
        st.session_state[_PAGE] = result.page - 1
        st.rerun()
    if next_col.button("Next", icon=":material/chevron_right:", disabled=result.page >= result.pages, width="stretch", key="videos.next"):
        st.session_state[_PAGE] = result.page + 1
        st.rerun()
