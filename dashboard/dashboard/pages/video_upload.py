"""
pages/video_upload.py — Upload a new video.

Sends the file in chunks with a progress bar (services/uploader.py). If the
upload is interrupted, it can be resumed from the video's page.
"""

from __future__ import annotations

import streamlit as st

from dashboard import navigation
from dashboard.api import videos as videos_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components.layout import api_errors, page_header
from dashboard.components.video_ui import category_picker, run_with_progress, source_picker
from dashboard.services import uploader

_VERSION = "video_upload.version"   # bumping it gives every field a fresh, empty widget
_RESULT = "video_upload.result"     # (video id, title, notes) of the last successful upload


def _k(name: str) -> str:
    """Widget key for this form version (upload.<version>.<name>)."""
    return f"upload.{st.session_state.get(_VERSION, 0)}.{name}"


def _reset_form(keep_result: bool = False) -> None:
    """Empty every field. Old widget keys are dropped; new ones start blank."""
    st.session_state[_VERSION] = st.session_state.get(_VERSION, 0) + 1
    for key in [k for k in st.session_state if str(k).startswith("upload.")]:
        del st.session_state[key]
    if not keep_result:
        st.session_state.pop(_RESULT, None)


def _came_from_another_page() -> bool:
    return st.session_state.get(navigation.PREVIOUS_PAGE) != navigation.UPLOAD_PATH


def _last_result() -> None:
    result = st.session_state.get(_RESULT)
    if not result:
        return
    video_id, title, notes = result
    with st.container(border=True):
        st.success(f"“{title}” uploaded.", icon=":material/check_circle:")
        for note in notes:
            st.caption(note)
        left, right, _ = st.columns([1, 1, 3])
        if left.button("Open video", type="primary", icon=":material/movie:", use_container_width=True,
                       key="video_upload.open"):
            st.session_state.pop(_RESULT, None)
            st.switch_page(navigation.pages().video_detail, query_params={"id": video_id})
        if right.button("Dismiss", use_container_width=True, key="video_upload.dismiss"):
            st.session_state.pop(_RESULT, None)
            st.rerun()


def render() -> None:
    page_header("Upload video", "MP4 (H.264) plays on every phone. The video starts as a draft unless you publish it.")

    if _came_from_another_page():
        _reset_form()          # coming back to this page always starts from an empty form
    _last_result()

    client = session.get_client()
    with api_errors("load categories"):
        categories = videos_api.categories(client)

    title = st.text_input("Title *", key=_k("title"), max_chars=120)
    description = st.text_area("Description", key=_k("description"), max_chars=5000, height=100)
    col_cat, col_order = st.columns([2, 1])
    with col_cat:
        category = category_picker(categories, key=_k("category"))
    order = col_order.number_input("Display order", key=_k("order"), min_value=0, max_value=100_000, value=0,
                                   help="Lower numbers appear first in the app.")

    st.divider()
    source, source_error = source_picker(key=_k("source"))
    if source_error:
        st.error(source_error)
    st.divider()
    attach = st.checkbox("Attach a document (PDF, Word or PowerPoint)", key=_k("attach_doc"),
                         help="One document per video. It is view-only in the app — nobody can download it.")
    doc_source, doc_error = (None, None)
    if attach:
        doc_source, doc_error = source_picker(key=_k("doc"), kind="document")
        if doc_error:
            st.error(doc_error)

    st.divider()
    thumbnail = st.file_uploader("Thumbnail (optional)", type=["jpg", "jpeg", "png", "webp"], key=_k("thumb"),
                                 help="Cover image shown in the app. JPEG, PNG or WebP, up to 5 MB.")
    publish = st.checkbox("Publish right after upload", key=_k("publish"),
                          help="Otherwise it stays a draft until you publish it from the video's page.")

    if not st.button("Upload", type="primary", icon=":material/upload:", key=_k("go")):
        return
    if not title.strip():
        st.error("Enter a title.")
        return
    if source is None:
        st.error(source_error or "Choose a video file.")
        return
    if attach and doc_source is None:
        st.error(doc_error or "Choose the document, or untick “Attach a document”.")
        return

    video = run_with_progress(
        lambda on_progress: uploader.start_and_upload(
            client, source,
            title=title.strip(), description=description.strip() or None,
            category=category, sort_order=int(order), on_progress=on_progress,
        )
    )
    if video is None:
        return

    notes = [f"“{video.title}” is saved as a draft."]
    try:
        if doc_source is not None:
            with st.spinner(f"Uploading document {doc_source.name}…"):
                updated = videos_api.upload_document(client, video.id, doc_source.name, doc_source.fh, doc_source.size)
            doc = updated.admin_document
            if doc and doc.status == "ready":
                notes.append(f"Document “{doc.name}” attached ({doc.page_count} pages, view-only).")
            elif doc and doc.status == "failed":
                notes.append(f"Document “{doc.name}” attached, but it can't be shown yet: {doc.error}")
            else:
                notes.append(f"Document “{doc_source.name}” attached — it's being prepared for viewing.")
        if thumbnail is not None:
            videos_api.set_thumbnail(client, video.id, thumbnail.name, thumbnail.getvalue(), thumbnail.type or "image/jpeg")
            notes.append("Thumbnail saved.")
        if publish:
            videos_api.set_status(client, video.id, "published")
            notes[0] = f"“{video.title}” is published and visible in the app."
    except ApiError as exc:
        notes.append(f"Uploaded, but a follow-up step failed: {exc.message} — finish it on the video's page.")
    st.session_state[_RESULT] = (video.id, video.title, notes)
    _reset_form(keep_result=True)   # clear every field for the next upload
    st.rerun()
