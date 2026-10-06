"""
pages/video_detail.py — Preview, edit, publish, thumbnail, resume, delete.

Opened from the Videos list with ?id=<video id>.
"""

from __future__ import annotations

import streamlit as st

from dashboard import navigation
from dashboard.api import videos as videos_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components.layout import api_errors
from dashboard.components.video_ui import category_picker, run_with_progress, source_picker, status_badge
from dashboard.models import Video
from dashboard.services import uploader
from dashboard.utils.formatting import fmt_bytes, fmt_datetime, fmt_duration

_FLASH = "video_detail.flash"


def _flash(kind: str, text: str) -> None:
    st.session_state[_FLASH] = (kind, text)
    st.rerun()


def _act(fn, success: str) -> None:
    try:
        fn()
    except ApiError as exc:
        _flash("error", exc.message)
    _flash("success", success)


@st.dialog("Delete this video?")
def _confirm_delete(video: Video) -> None:
    st.write(f"**{video.title}** and its files will be deleted permanently. This can't be undone.")
    left, right = st.columns(2)
    if left.button("Cancel", use_container_width=True):
        st.rerun()
    if right.button("Delete", type="primary", icon=":material/delete:", use_container_width=True):
        try:
            videos_api.delete_video(session.get_client(), video.id)
        except ApiError as exc:
            st.error(exc.message)
            return
        st.session_state["videos.flash"] = f"Deleted “{video.title}”."
        st.switch_page(navigation.pages().videos)


def _resume_section(video: Video) -> None:
    up = video.upload
    st.warning(
        f"This upload isn't finished: {up.received_chunks} of {up.total_chunks} parts received "
        f"({up.fraction:.0%}). Unfinished uploads are removed after {fmt_datetime(up.expires_at)}.",
        icon=":material/pending:",
    )
    st.progress(up.fraction)
    st.subheader("Resume upload")
    st.caption(f"Choose the same file: {video.original_file_name or 'the original file'} ({fmt_bytes(video.file_size)}).")
    source, error = source_picker(key=f"resume.{video.id}")
    if error:
        st.error(error)
    if st.button("Resume upload", type="primary", icon=":material/play_arrow:", disabled=source is None):
        done = run_with_progress(lambda cb: uploader.resume(session.get_client(), video, source, on_progress=cb))
        if done:
            _flash("success", "Upload finished. The video is now a draft.")


def _details_form(video: Video, categories: list[str]) -> None:
    st.subheader("Details")
    title = st.text_input("Title", value=video.title, max_chars=120, key=f"edit.{video.id}.title")
    description = st.text_area("Description", value=video.description or "", max_chars=5000, height=100,
                               key=f"edit.{video.id}.description")
    col_cat, col_order = st.columns([2, 1])
    with col_cat:
        category = category_picker(categories, key=f"edit.{video.id}.category", current=video.category)
    order = col_order.number_input("Display order", min_value=0, max_value=100_000, value=video.sort_order,
                                   key=f"edit.{video.id}.order", help="Lower numbers appear first in the app.")
    if st.button("Save details", icon=":material/save:", key=f"edit.{video.id}.save"):
        if not title.strip():
            st.error("Title can't be empty.")
            return
        _act(
            lambda: videos_api.update_video(
                session.get_client(), video.id, title=title.strip(),
                description=description.strip() or None, category=category, sort_order=int(order),
            ),
            "Details saved.",
        )


def _thumbnail_section(video: Video) -> None:
    st.subheader("Thumbnail")
    if video.thumbnail_url:
        st.image(video.thumbnail_url, use_container_width=True)
    else:
        st.caption("No thumbnail yet.")
    image = st.file_uploader("Replace thumbnail" if video.has_thumbnail else "Add thumbnail",
                             type=["jpg", "jpeg", "png", "webp"], key=f"thumb.{video.id}",
                             help="JPEG, PNG or WebP, up to 5 MB.")
    left, right = st.columns(2)
    if left.button("Save thumbnail", disabled=image is None, use_container_width=True, key=f"thumb.{video.id}.save"):
        _act(lambda: videos_api.set_thumbnail(session.get_client(), video.id, image.name, image.getvalue(),
                                              image.type or "image/jpeg"), "Thumbnail saved.")
    if video.has_thumbnail and right.button("Remove", use_container_width=True, key=f"thumb.{video.id}.remove"):
        _act(lambda: videos_api.remove_thumbnail(session.get_client(), video.id), "Thumbnail removed.")


_PAGE_KEY = "video_detail.doc_page"


def _document_section(video: Video) -> None:
    st.subheader("Document")
    st.caption("One PDF, Word or PowerPoint file per video. View-only: shown as pages, never downloadable.")
    client = session.get_client()
    doc = video.admin_document

    if doc is None:
        st.caption("No document attached.")
    else:
        kind = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint"}.get(doc.file_type, doc.file_type)
        pages = f" · {doc.page_count} pages" if doc.page_count else ""
        st.markdown(f"**{doc.name}**  \n{kind} · {fmt_bytes(doc.size)}{pages} · added {fmt_datetime(doc.uploaded_at)}")
        if doc.status == "processing":
            st.info("Preparing the document for viewing…", icon=":material/hourglass_top:")
            if st.button("Refresh", icon=":material/refresh:", key=f"doc.{video.id}.refresh"):
                st.rerun()
        elif doc.status == "failed":
            st.error(doc.error or "The document couldn't be prepared.", icon=":material/error:")
            if st.button("Retry", icon=":material/refresh:", key=f"doc.{video.id}.retry"):
                _act(lambda: videos_api.retry_document(client, video.id), "Preparing the document again…")
        else:
            with st.expander("Preview pages", expanded=False):
                try:
                    viewer = videos_api.document_pages(client, video.id)
                except ApiError as exc:
                    st.error(exc.message)
                else:
                    page = st.number_input("Page", min_value=1, max_value=viewer.page_count, value=1, step=1,
                                           key=f"{_PAGE_KEY}.{video.id}")
                    st.image(viewer.pages[int(page) - 1], caption=f"Page {int(page)} of {viewer.page_count}",
                             use_container_width=True)

    with st.expander("Replace document" if doc else "Attach a document", expanded=doc is None):
        source, error = source_picker(key=f"doc.{video.id}.src", kind="document")
        if error:
            st.error(error)
        left, right = st.columns(2)
        if left.button("Upload document", type="primary", icon=":material/upload_file:", disabled=source is None,
                       use_container_width=True, key=f"doc.{video.id}.upload"):
            with st.spinner(f"Uploading {source.name}…"):
                _act(lambda: videos_api.upload_document(client, video.id, source.name, source.fh, source.size),
                     f"Document “{source.name}” uploaded.")
        if doc and right.button("Remove document", icon=":material/delete:", use_container_width=True,
                                key=f"doc.{video.id}.remove"):
            _act(lambda: videos_api.remove_document(client, video.id), "Document removed.")


def render() -> None:
    if st.button("Back to videos", icon=":material/arrow_back:"):
        st.switch_page(navigation.pages().videos)

    video_id = st.query_params.get("id")
    if not video_id:
        st.info("Pick a video from the Videos page.", icon=":material/video_library:")
        return

    client = session.get_client()
    with api_errors("load this video"):
        video = videos_api.get_video(client, video_id)
        categories = videos_api.categories(client)

    flash = st.session_state.pop(_FLASH, None)
    if flash:
        (st.success if flash[0] == "success" else st.error)(flash[1])

    st.title(video.title)
    with st.container(horizontal=True):
        status_badge(video)
        if video.category:
            st.badge(video.category, color="blue", icon=":material/label:")

    if video.status == "uploading":
        _resume_section(video)
        st.divider()
        if st.button("Cancel upload and delete", icon=":material/delete:"):
            _confirm_delete(video)
        return

    player, side = st.columns([3, 2], gap="large")
    with player:
        st.video(video.playback_url)
        st.caption(
            f"{video.original_file_name or 'video'} · {fmt_bytes(video.file_size)} · "
            f"duration {fmt_duration(video.duration_seconds)}"
            + (f" · {video.video_codec}" if video.video_codec else "")
        )
        if video.video_codec and video.video_codec != "h264":
            st.warning(f"This video uses {video.video_codec}; some phones only play H.264.", icon=":material/warning:")
    with side:
        st.subheader("Visibility")
        if video.status == "published":
            st.caption(f"Visible in the app since {fmt_datetime(video.published_at)}.")
            if st.button("Unpublish (make draft)", icon=":material/visibility_off:", use_container_width=True):
                _act(lambda: videos_api.set_status(client, video.id, "draft"), "Video is now a draft (hidden in the app).")
        else:
            st.caption("Draft — not visible in the app.")
            if st.button("Publish", type="primary", icon=":material/public:", use_container_width=True):
                _act(lambda: videos_api.set_status(client, video.id, "published"), "Video published — it's now visible in the app.")
        _thumbnail_section(video)

    st.divider()
    _document_section(video)
    st.divider()
    _details_form(video, categories)
    st.divider()
    st.caption(
        f"Added {fmt_datetime(video.created_at)} · uploaded {fmt_datetime(video.uploaded_at)} · "
        f"last changed {fmt_datetime(video.updated_at)} · ID `{video.id}`"
    )
    if st.button("Delete video", icon=":material/delete:"):
        _confirm_delete(video)
