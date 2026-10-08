"""
pages/video_detail.py — Preview, edit, publish, thumbnail, resume, delete.

Opened from the Videos list with ?id=<video id>.
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

from dashboard import navigation
from dashboard.api import videos as videos_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components.layout import api_errors
from dashboard.components import browser_upload as upload_box
from dashboard.components.video_ui import (broken_file_warning, category_picker, details_hints, document_job, probe_fields,
                                           optimization_summary, status_badge, upload_job)
from dashboard.config import settings
from dashboard.models import Video
from dashboard.services import uploads
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


def _publish(client, video: Video) -> None:
    """Publish now — or, while the document is still being prepared, when it is ready (the server decides)."""
    try:
        result = videos_api.set_status(client, video.id, "published", when_ready=True)
    except ApiError as exc:
        _flash("error", exc.message)
    if result.publish_when_ready:
        what = " and ".join((["it is optimized"] if result.optimizing else [])
                            + (["the document is ready"] if result.admin_document and result.admin_document.status != "ready" else []))
        _flash("success", f"It will be published automatically as soon as {what or 'it is ready'}.")
    _flash("success", "Video published — it's now visible in the app.")


@st.dialog("Delete this video?")
def _confirm_delete(video: Video) -> None:
    st.write(f"**{video.title}** and its files will be deleted permanently. This can't be undone.")
    left, right = st.columns(2)
    if left.button("Cancel", width="stretch"):
        st.rerun()
    if right.button("Delete", type="primary", icon=":material/delete:", width="stretch"):
        try:
            videos_api.delete_video(session.get_client(), video.id)
        except ApiError as exc:
            st.error(exc.message)
            return
        st.session_state["videos.flash"] = f"Deleted “{video.title}”."
        st.switch_page(navigation.pages().videos)


def _box(video: Video, name: str, origin: str, role: str, **box_args) -> tuple[Optional[dict], Optional[dict]]:
    """A browser upload box for this video. Returns (chosen file, running task).
    The task's file is handed to the Uploads panel in the sidebar, which does
    the uploading (it keeps going if the admin switches pages)."""
    file_key, task_key = f"{name}.{video.id}.file", f"{name}.{video.id}.task"
    task = uploads.get(st.session_state.get(task_key))
    if task is None:
        st.session_state.pop(task_key, None)
    event = upload_box.new_event(
        upload_box.render(f"{name}.{video.id}.box", task={"id": task["id"], "role": role} if task else None,
                          **box_args),
        f"{name}.{video.id}.seen",
    )
    if event:
        if event["event"] == "file":
            st.session_state[file_key] = event
        elif event["event"] == "frame" and event.get("sha256"):
            # The frame event repeats the file record: recovers a "file" event swallowed by a slow rerun.
            known = st.session_state.get(file_key)
            if known is None or known.get("sha256") is None or \
                    (known.get("name"), known.get("size")) != (event.get("name"), event.get("size")):
                st.session_state[file_key] = {k: v for k, v in event.items()
                                              if k not in ("data", "width", "height")} | {"event": "file"}
        elif event["event"] == "cleared":
            st.session_state.pop(file_key, None)
    return st.session_state.get(file_key), task


def _task_note(task: dict) -> None:
    if task["state"] == "running":
        st.caption(":material/upload: Uploading — follow it in **Uploads** in the sidebar. You can switch pages; "
                   "keep this browser tab open.")
    else:
        st.caption(f":material/error: The upload stopped{': ' + task['error'] if task.get('error') else ''}. "
                   "**Retry** or **Dismiss** it in Uploads in the sidebar.")


def _start_task(video: Video, name: str, **task_args) -> None:
    task_args.setdefault("title", video.title)
    task_args.setdefault("video_id", video.id)
    st.session_state[f"{name}.{video.id}.task"] = uploads.start(**task_args)
    st.session_state.pop(f"{name}.{video.id}.file", None)
    st.rerun()


def _resume_section(video: Video, *, origin: str = uploads.ORIGIN_RESUME, target: Optional[Video] = None) -> None:
    """Resume an unfinished upload (choose the same file). For an unfinished *replacement* (`target` = the video
    whose file it will replace) completing it swaps the file in."""
    up = video.upload
    st.warning(
        f"This upload isn't finished: {up.received_chunks} of {up.total_chunks} parts received "
        f"({up.fraction:.0%}). Unfinished uploads are removed after {fmt_datetime(up.expires_at)}.",
        icon=":material/pending:",
    )
    st.progress(up.fraction)
    st.subheader("Resume upload")
    st.caption(f"Choose the same file: {video.original_file_name or 'the original file'} ({fmt_bytes(video.file_size)}). "
               "Only the missing parts are sent.")
    chosen, task = _box(video, "resume", origin, "video",
                        expect={"name": video.original_file_name, "size": video.file_size, "sha256": video.sha256})
    if task:
        _task_note(task)
        return
    ready = bool(chosen and chosen.get("sha256") and not chosen.get("mismatch") and chosen.get("size") == video.file_size)
    if st.button("Resume upload", type="primary", icon=":material/play_arrow:", disabled=not ready):
        with api_errors("resume the upload"):
            job = upload_job(session.get_client(), video)
        if target is not None:
            _start_task(video, "resume", origin=origin, label=f"{target.title} (new file)", video_job=job,
                        title=target.title, video_id=target.id)
        else:
            _start_task(video, "resume", origin=origin, label=f"{video.title} (resume)", video_job=job)


def _replace_section(video: Video) -> None:
    """Replace the video's file with a new one. The old file keeps playing until the new one is uploaded and
    checked; then the server swaps it in and deletes the old one."""
    client = session.get_client()
    st.subheader("Replace video file")
    st.caption("The current file keeps playing until the new one is uploaded and checked; then it is swapped in and "
               "the old file is deleted. Title, thumbnail, document, category, order and publish status stay.")
    task_key = f"replace.{video.id}.task"
    task = uploads.get(st.session_state.get(task_key))
    rep = video.replacement
    if rep and task is None and rep.optimization:
        st.caption("A new file is already uploaded and being optimized — see the progress at the top of the page. "
                   "It replaces the current file automatically.")
        return
    if rep and task is None:
        staging = None
        try:
            staging = videos_api.get_video(client, rep.video_id)
        except ApiError:
            pass
        if staging is not None and staging.status == "uploading" and staging.upload:
            _resume_section(staging, origin=uploads.ORIGIN_REPLACE, target=video)
            if st.button("Discard the unfinished replacement", icon=":material/delete:", key=f"replace.{video.id}.discard"):
                _act(lambda: videos_api.delete_video(client, staging.id), "The unfinished replacement was discarded.")
            return
    chosen, task = _box(video, "replace", uploads.ORIGIN_REPLACE, "video",
                        max_bytes=settings.MAX_VIDEO_SIZE_MB * 1024 * 1024)
    if task:
        _task_note(task)
        return
    dup_key = f"replace.{video.id}.duplicate"
    found = st.session_state.get(dup_key)
    if found and not (chosen and found["file"] == (chosen["name"], chosen["size"])):
        found = None
        st.session_state.pop(dup_key, None)
    broken = chosen is not None and broken_file_warning(chosen, key=f"replace.{video.id}.accept_bad.{chosen['name']}.{chosen['size']}")
    ready = bool(chosen and chosen.get("sha256")) and not broken
    allow_duplicate = False
    if found:
        with st.container(border=True):
            st.warning(f"This exact file already exists as “{found['existing'].get('title')}”.", icon=":material/content_copy:")
            left, right = st.columns(2)
            allow_duplicate = left.button("Replace anyway", icon=":material/swap_horiz:", width="stretch",
                                          key=f"replace.{video.id}.anyway")
            if right.button("Cancel", width="stretch", key=f"replace.{video.id}.dupcancel"):
                st.session_state.pop(dup_key, None)
                st.rerun()
    go = st.button("Replace video file", type="primary", icon=":material/swap_horiz:", disabled=not ready or bool(found),
                   key=f"replace.{video.id}.go")
    if not (go or allow_duplicate):
        return
    try:
        staging = videos_api.replace_file(
            client, video.id, file_name=chosen["name"], file_size=chosen["size"], content_type="video/mp4",
            sha256=chosen["sha256"], allow_duplicate=allow_duplicate, **probe_fields(chosen))
    except ApiError as exc:
        if exc.code == "DUPLICATE_VIDEO":
            st.session_state[dup_key] = {"file": (chosen["name"], chosen["size"]), "existing": exc.details}
            st.rerun()
        st.error(f"Couldn't start the replacement: {exc.message}", icon=":material/error:")
        return
    with api_errors("start the upload"):
        job = upload_job(client, staging)
    _start_task(video, "replace", origin=uploads.ORIGIN_REPLACE, label=f"{video.title} (new file)", video_job=job)


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
    with st.container():           # a fixed slot, so warnings appearing don't move the elements below
        hints = details_hints(session.get_client(), title=title, category=category, key=f"edit.{video.id}.hint",
                              video=video, category_key=f"edit.{video.id}.category")
    if st.button("Save details", icon=":material/save:", key=f"edit.{video.id}.save", disabled=hints.blocked):
        if not title.strip():
            st.error("Title can't be empty.")
            return
        _act(
            lambda: videos_api.update_video(
                session.get_client(), video.id, title=title.strip(),
                description=description.strip() or None, category=category, sort_order=int(order),
                allow_duplicate_title=hints.allow_duplicate_title,
            ),
            "Details saved.",
        )


def _thumbnail_section(video: Video) -> None:
    st.subheader("Thumbnail")
    if video.thumbnail_url:
        st.image(video.thumbnail_url, width="stretch")
    else:
        st.caption("No thumbnail yet.")
    image = st.file_uploader("Replace thumbnail" if video.has_thumbnail else "Add thumbnail",
                             type=["jpg", "jpeg", "png", "webp"], key=f"thumb.{video.id}",
                             help="JPEG, PNG or WebP, up to 5 MB.")
    left, right = st.columns(2)
    if left.button("Save thumbnail", disabled=image is None, width="stretch", key=f"thumb.{video.id}.save"):
        _act(lambda: videos_api.set_thumbnail(session.get_client(), video.id, image.name, image.getvalue(),
                                              image.type or "image/jpeg"), "Thumbnail saved.")
    if video.has_thumbnail and right.button("Remove", width="stretch", key=f"thumb.{video.id}.remove"):
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
                             width="stretch")

    with st.expander("Replace document" if doc else "Attach a document", expanded=doc is None):
        chosen, task = _box(video, "doc", uploads.ORIGIN_DOCUMENT, "document", kind="document")
        left, right = st.columns(2)
        if task:
            with left:
                _task_note(task)
        elif left.button("Upload document", type="primary", icon=":material/upload_file:",
                         disabled=not (chosen and chosen.get("sha256")), width="stretch", key=f"doc.{video.id}.upload"):
            with api_errors("start the document upload"):
                job = document_job(client, video.id)
            _start_task(video, "doc", origin=uploads.ORIGIN_DOCUMENT,
                        label=f"{video.title} · {chosen['name']}", doc_job=job)
        if doc and not task and right.button("Remove document", icon=":material/delete:", width="stretch",
                                            key=f"doc.{video.id}.remove"):
            _act(lambda: videos_api.remove_document(client, video.id), "Document removed.")


def _pending_optimization(video: Video, client) -> tuple[Optional[Video], str]:
    """The video whose optimization is pending (this one, or the unfinished replacement's upload) and a signature
    of the state, so the page can refresh itself when it changes."""
    if video.optimizing or video.optimization_failed:
        return video, f"own:{video.optimization.state}"
    rep = video.replacement
    if rep and rep.optimization:
        try:
            return videos_api.get_video(client, rep.video_id), f"rep:{rep.optimization}"
        except ApiError:
            return None, f"rep:{rep.optimization}"
    return None, "none"


def _optimization_panel(video_id: str, signature: str) -> None:
    """Progress of the optimization (the file is re-encoded smaller on the server before it is stored).
    Refreshes itself every few seconds while something is pending; reloads the page when it finishes."""
    client = session.get_client()
    try:
        video = videos_api.get_video(client, video_id)
    except ApiError:
        return
    pending, now = _pending_optimization(video, client)
    if now != signature:
        st.rerun()                          # started, finished or failed: show the whole page again
    if pending is None or pending.optimization is None:
        return
    o = pending.optimization
    is_replacement = pending.id != video.id
    what = "The new file" if is_replacement else "This video"
    with st.container(border=True):
        if o.state == "failed":
            st.error(f"{what} couldn't be optimized: {o.error or 'unknown error'}", icon=":material/error:")
            st.caption("The upload is still on the server (it was not sent to cloud storage). Retry, or delete it "
                       "and upload again.")
            left, right = st.columns(2)
            if left.button("Retry optimization", icon=":material/refresh:", type="primary", width="stretch",
                           key=f"opt.{video_id}.retry"):
                _act(lambda: videos_api.retry_optimization(client, pending.id), "Optimizing again…")
            if is_replacement and right.button("Discard the new file", icon=":material/delete:", width="stretch",
                                               key=f"opt.{video_id}.discard"):
                _act(lambda: videos_api.delete_video(client, pending.id), "The new file was discarded.")
        else:
            if o.state == "queued":
                st.info(f"{what} is waiting in line to be optimized.", icon=":material/hourglass_top:")
            else:
                st.info(f"Optimizing: {what.lower()} is being converted into a smaller file "
                        f"(same resolution) — {o.progress:.0%}.", icon=":material/compress:")
                st.progress(min(max(o.progress, 0.0), 1.0))
            st.caption(
                ("The current file keeps playing until the new one is ready; then it is swapped in automatically. "
                 if is_replacement else
                 "It stays a draft until this is finished" + (" and then publishes by itself. " if video.publish_when_ready else ". ")) +
                "Only the smaller file is stored in the cloud. You can leave this page.")


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

    for result in uploads.results_for_video(video.id):
        # Finished while this page (or another one) was open: show it here once.
        if result["origin"] != uploads.ORIGIN_UPLOAD:
            (st.success if result["ok"] else st.warning)("  \n".join(result["notes"]))
    flash = st.session_state.pop(_FLASH, None)
    if flash:
        (st.success if flash[0] == "success" else st.error)(flash[1])

    st.title(video.title)
    with st.container(horizontal=True):
        status_badge(video)
        if video.category:
            st.badge(video.category, color="blue", icon=":material/label:")
    _, signature = _pending_optimization(video, client)
    if signature != "none":
        run_every = "3s" if signature.endswith(("queued", "running")) else None
        st.fragment(run_every=run_every)(_optimization_panel)(video.id, signature)

    if video.status == "uploading" and video.replaces:
        # the unfinished upload of a new file for another video
        target = None
        try:
            target = videos_api.get_video(client, video.replaces)
        except ApiError:
            pass
        if target is not None:
            st.info(f"This is an unfinished upload of a **new file for “{target.title}”**. Until it is finished that "
                    "video keeps playing its current file.", icon=":material/swap_horiz:")
            if st.button("Open that video", icon=":material/movie:", key="video_detail.open_target"):
                st.switch_page(navigation.pages().video_detail, query_params={"id": target.id})
        _resume_section(video, origin=uploads.ORIGIN_REPLACE, target=target)
        st.divider()
        if st.button("Discard this upload", icon=":material/delete:"):
            _confirm_delete(video)
        return
    if video.status == "uploading":
        _resume_section(video)
        st.divider()
        if st.button("Cancel upload and delete", icon=":material/delete:"):
            _confirm_delete(video)
        return

    if video.missing_files:
        labels = {"video": "the video file", "thumbnail": "the thumbnail", "document": "the document"}
        what = ", ".join(labels.get(m, m) for m in video.missing_files)
        st.error(
            f"Missing from storage: {what}. "
            + ("This video can't be played or published — delete it and upload it again. "
               if "video" in video.missing_files else "Replace or remove it below. ")
            + "(Usually caused by uploading before the storage was switched, e.g. local → R2.)",
            icon=":material/report:",
        )
        if "video" in video.missing_files and st.button("Delete this video", type="primary", icon=":material/delete:",
                                                         key="video_detail.delete_broken"):
            _confirm_delete(video)

    player, side = st.columns([3, 2], gap="large")
    with player:
        if "video" in video.missing_files:
            st.info("No video to preview — the file is missing from storage.", icon=":material/videocam_off:")
        elif not video.playback_url:
            st.info("No preview yet — the video is being optimized on the server." if video.optimizing else
                    "No preview — the video couldn't be optimized." if video.optimization_failed else
                    "No preview available.", icon=":material/videocam_off:")
        else:
            st.video(video.playback_url)
        st.caption(
            f"{video.original_file_name or 'video'} · {fmt_bytes(video.file_size)} · "
            f"duration {fmt_duration(video.duration_seconds)}"
            + (f" · {video.video_codec}" if video.video_codec else "")
        )
        summary = optimization_summary(video)
        if summary:
            st.caption(f":material/compress: {summary}")
        if video.integrity_verified and not (video.optimization and video.optimization.mode in ("compressed", "converted")):
            st.caption(":material/verified: Integrity verified — every byte stored matches the file that was sent"
                       + (f" (SHA-256 {video.sha256[:12]}…)" if video.sha256 else "") + ".",
                       help=f"SHA-256: {video.sha256}" if video.sha256 else None)
        if video.video_codec and video.video_codec != "h264":
            st.warning(f"This video uses {video.video_codec}; some phones only play H.264.", icon=":material/warning:")
    with side:
        st.subheader("Visibility")
        if video.status == "published":
            if video.hidden_in_app:
                failed = video.admin_document is not None and video.admin_document.status == "failed"
                st.warning("Published, but **hidden from the app**: its document "
                           + ("couldn't be prepared — retry or remove it below." if failed else
                              "is still being prepared. It appears in the app by itself once the document is ready."),
                           icon=":material/visibility_off:")
            else:
                st.caption(f"Visible in the app since {fmt_datetime(video.published_at)}.")
            if st.button("Unpublish (make draft)", icon=":material/visibility_off:", width="stretch"):
                _act(lambda: videos_api.set_status(client, video.id, "draft"), "Video is now a draft (hidden in the app).")
        elif video.publish_when_ready:
            reasons = (["it is optimized"] if video.optimizing else []) + (
                ["the document is ready"] if video.admin_document and video.admin_document.status != "ready" else [])
            if reasons == ["the document is ready"]:
                st.info("Waiting for the document to be prepared — then this video is published automatically.",
                        icon=":material/schedule:")
            else:
                st.info(f"Waiting until {' and '.join(reasons) or 'it is ready'} — then this video is published "
                        "automatically.", icon=":material/schedule:")
            if st.button("Cancel (keep as draft)", icon=":material/close:", width="stretch"):
                _act(lambda: videos_api.set_status(client, video.id, "draft"), "Cancelled — the video stays a draft.")
        else:
            doc = video.admin_document
            failed = (doc is not None and doc.status == "failed") or video.optimization_failed
            waiting = (doc is not None and doc.status == "processing") or video.optimizing
            if video.optimization_failed:
                st.warning("The video couldn't be optimized, so it can't be published yet. Retry above.",
                           icon=":material/compress:")
            elif failed:
                st.warning("The document couldn't be prepared, so the video can't be published yet. "
                           "Retry or remove the document below.", icon=":material/description:")
            elif waiting:
                st.caption("Draft — " + ("the video is still being optimized." if video.optimizing else
                                         "the document is still being prepared."))
            else:
                st.caption("Draft — not visible in the app.")
            doc_waiting = doc is not None and doc.status == "processing"
            label = ("Publish when ready" if video.optimizing and doc_waiting else
                     "Publish when optimized" if video.optimizing else
                     "Publish when the document is ready" if waiting else "Publish")
            if st.button(label, type="primary",
                         icon=":material/public:", width="stretch", disabled="video" in video.missing_files or failed):
                _publish(client, video)
        _thumbnail_section(video)

    st.divider()
    _document_section(video)
    st.divider()
    _replace_section(video)
    st.divider()
    _details_form(video, categories)
    st.divider()
    st.caption(
        f"Added {fmt_datetime(video.created_at)} · uploaded {fmt_datetime(video.uploaded_at)} · "
        f"last changed {fmt_datetime(video.updated_at)} · ID `{video.id}`"
    )
    if st.button("Delete video", icon=":material/delete:"):
        _confirm_delete(video)
