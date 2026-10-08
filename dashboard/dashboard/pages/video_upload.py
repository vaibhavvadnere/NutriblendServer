"""
pages/video_upload.py — Upload a new video.

Files are chosen in browser upload boxes (components/browser_upload) and
fingerprinted there. On Upload the server creates the video (or answers "this
exact file already exists" -> Open / Resume / Upload anyway), the boxes hand
their files to the Uploads panel in the sidebar, and the form is cleared. The
upload then runs in the sidebar — straight from the admin's browser to
storage — while the admin uploads another video or uses other pages. When it
finishes, services/uploads.py saves the thumbnail, publishes if asked, and the
result shows here (and as a notice on any page).
"""

from __future__ import annotations

import streamlit as st

from dashboard import navigation
from dashboard.api import videos as videos_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components import browser_upload as upload_box
from dashboard.components.layout import api_errors, page_header
from dashboard.components.video_ui import (broken_file_warning, category_picker, details_hints, document_job, frame_thumbnail,
                                           probe_fields, upload_job)
from dashboard.config import settings
from dashboard.services import uploads
from dashboard.services.memo import forget_all, remember

_VERSION = "video_upload.version"   # bumping it gives every field a fresh, empty widget
_RESULTS = "video_upload.results"   # finished uploads to show (newest first)
_STARTED = "video_upload.started"   # title of the upload just handed to the sidebar


def _k(name: str) -> str:
    """Widget key for this form version (upload.<version>.<name>)."""
    return f"upload.{st.session_state.get(_VERSION, 0)}.{name}"


def _reset_form() -> None:
    """Empty every field. Old widget keys are dropped; new ones start blank."""
    st.session_state[_VERSION] = st.session_state.get(_VERSION, 0) + 1
    for key in [k for k in st.session_state if str(k).startswith("upload.")]:
        del st.session_state[key]


def _came_from_another_page() -> bool:
    return st.session_state.get(navigation.PREVIOUS_PAGE) != navigation.UPLOAD_PATH


def _collect_results() -> None:
    """Move finished uploads started on this page into this page's banners."""
    shown = st.session_state.setdefault(_RESULTS, [])
    for result in uploads.take_results(uploads.ORIGIN_UPLOAD):
        shown.insert(0, result)


def _banners() -> None:
    started = st.session_state.pop(_STARTED, None)
    if started:
        st.info(f"“{started}” is uploading — follow it in **Uploads** in the sidebar. You can upload another "
                "video or switch pages; keep this browser tab open until it finishes.", icon=":material/upload:")
    running = [t for t in uploads.active(origin=uploads.ORIGIN_UPLOAD) if t["state"] == "running"]
    if running and not started:
        names = ", ".join(f"“{t['title']}”" for t in running)
        st.caption(f":material/upload: Uploading now: {names} — see Uploads in the sidebar.")
    for i, r in enumerate(st.session_state.get(_RESULTS, [])):
        with st.container(border=True):
            (st.success if r["ok"] else st.warning)(r["notes"][0], icon=":material/check_circle:" if r["ok"] else ":material/warning:")
            for note in r["notes"][1:]:
                st.caption(note)
            left, right, _ = st.columns([1, 1, 3])
            if left.button("Open video", type="primary", icon=":material/movie:", width="stretch",
                           key=f"video_upload.open.{r['task']}"):
                st.session_state[_RESULTS].pop(i)
                st.switch_page(navigation.pages().video_detail, query_params={"id": r["video_id"]})
            if right.button("Dismiss", width="stretch", key=f"video_upload.dismiss.{r['task']}"):
                st.session_state[_RESULTS].pop(i)
                st.rerun()


def _optimization_note(client) -> None:
    """Tell the admin what happens to the video after the upload."""
    try:
        status = remember("optimization", 60, lambda: videos_api.optimization_status(client))
    except ApiError:
        return                       # an older server: nothing to say
    if status.get("active"):
        st.caption(":material/compress: **Automatic optimization is on.** After the upload the server converts the video "
                   "into a much smaller file (same resolution, same look) and stores only that file — the original is "
                   "not kept. This takes a few minutes; the video stays a draft until it is done.")
    elif status.get("enabled"):
        st.warning("Automatic optimization is turned on, but ffmpeg is not installed on the server — videos are stored "
                   "exactly as uploaded.", icon=":material/warning:")


def render() -> None:
    page_header("Upload video", "MP4 (H.264) plays on every phone. The video starts as a draft unless you publish it.")

    if _came_from_another_page():
        _reset_form()          # coming back to this page always starts from an empty form
        st.session_state.pop(_RESULTS, None)   # …without old banners (uploads finished meanwhile still show)
    _collect_results()
    _banners()

    client = session.get_client()
    with api_errors("load categories"):
        categories = remember("categories", 20, lambda: videos_api.categories(client))
    _optimization_note(client)

    title = st.text_input("Title *", key=_k("title"), max_chars=120)
    description = st.text_area("Description", key=_k("description"), max_chars=5000, height=100)
    col_cat, col_order = st.columns([2, 1])
    with col_cat:
        category = category_picker(categories, key=_k("category"))
    order = col_order.number_input("Display order", key=_k("order"), min_value=0, max_value=100_000, value=0,
                                   help="Lower numbers appear first in the app.")

    task_id = st.session_state.get(_k("task"))
    task = uploads.get(task_id)
    # A fixed slot: warnings come and go inside it without moving anything below. (If the elements below shifted,
    # Streamlit would re-create the file boxes and they would lose the chosen file.) While an upload is being
    # handed over the title belongs to that video itself, so nothing is checked.
    with st.container():
        hints = details_hints(client, title="" if task else title, category=None if task else category,
                              key=_k("hint"), category_key=_k("category"))

    st.divider()
    event = upload_box.new_event(
        upload_box.render(_k("video"), max_bytes=settings.MAX_VIDEO_SIZE_MB * 1024 * 1024,
                          task={"id": task_id, "role": "video"} if task and task["video_job"] else None),
        _k("seen"),
    )
    _box_event(event, "file", "duplicate")
    chosen = st.session_state.get(_k("file"))

    st.divider()
    attach = st.checkbox("Attach a document (PDF, Word or PowerPoint)", key=_k("attach_doc"),
                         help="One document per video. It is view-only in the app — nobody can download it.")
    doc_chosen = None
    if attach:
        doc_event = upload_box.new_event(
            upload_box.render(_k("doc"), kind="document",
                              task={"id": task_id, "role": "document"} if task and task["doc_job"] else None),
            _k("doc_seen"),
        )
        _box_event(doc_event, "doc_file")
        doc_chosen = st.session_state.get(_k("doc_file"))

    st.divider()
    thumbnail = st.file_uploader("Thumbnail (optional)", type=["jpg", "jpeg", "png", "webp"], key=_k("thumb"),
                                 help="Cover image shown in the app. JPEG, PNG or WebP, up to 5 MB.")
    if not thumbnail and frame_thumbnail(st.session_state.get(_k("frame")), chosen):
        st.caption(":material/image: No thumbnail chosen — the picture taken from the video (shown in the file box) "
                   "will be the cover.")
    publish = st.checkbox("Publish right after upload", key=_k("publish"),
                          help="Otherwise it stays a draft until you publish it from the video's page.")

    if task:
        if _handed_over(task):
            st.session_state[_STARTED] = task["title"]
            _reset_form()      # the sidebar has the files: free the form for the next upload
            st.rerun()
        if st.session_state.get(_k("handover_failed")):
            st.error("The upload couldn't start — the Uploads panel in the sidebar didn't respond. Reload the page "
                     "and try again; the video can also be resumed from its page.", icon=":material/error:")
            return
        st.info("Starting the upload… stay on this page until it appears in **Uploads** on the left (a few seconds).",
                icon=":material/hourglass_top:")
        return

    duplicate = _duplicate_for(chosen)
    broken = chosen is not None and broken_file_warning(
        chosen, key=_k(f"accept_bad.{chosen['name']}.{chosen['size']}"))
    blocked = duplicate is not None or broken or hints.blocked
    # The click is remembered in the session (callback), not only in this run: if the admin edits another field
    # right after clicking, Streamlit restarts the page and a plain button click would be lost without a word.
    clicked = st.button("Upload", type="primary", icon=":material/upload:", key=_k("go"), disabled=blocked,
                        on_click=_remember_click)
    if blocked:
        st.session_state.pop(_k("go_pending"), None)
    go = clicked or bool(st.session_state.get(_k("go_pending")))
    action = _duplicate_panel(duplicate) if duplicate else None
    if not go and action is None:
        return
    if not title.strip():
        st.session_state.pop(_k("go_pending"), None)
        st.error("Enter a title.")
        return
    if not chosen:
        st.session_state.pop(_k("go_pending"), None)
        st.error("Choose a video file.")
        return
    if chosen.get("sha256") is None:       # the click stays remembered: the upload starts as soon as the check ends
        st.info("Still checking the file (a few seconds per GB) — the upload starts by itself when it is done.",
                icon=":material/hourglass_top:")
        return
    if attach and not doc_chosen:
        st.session_state.pop(_k("go_pending"), None)
        st.error("Choose the document, or untick “Attach a document”.")
        return
    if attach and doc_chosen.get("sha256") is None:
        st.info("Still checking the document — the upload starts by itself when it is done.",
                icon=":material/hourglass_top:")
        return

    # From here on the upload really starts (a rerun can only interrupt the page at a Streamlit call, and
    # none comes before the task exists), so the remembered click is spent — an error must not re-trigger it.
    with st.spinner("Preparing the upload — please stay on this page for a moment…"):
        st.session_state.pop(_k("go_pending"), None)
        if action == "resume":
            with api_errors("load the existing upload"):
                video = videos_api.get_video(client, duplicate["existing"]["video_id"])
        else:
            try:
                video = videos_api.start_upload(
                    client,
                    title=title.strip(), description=description.strip() or None,
                    category=category, sort_order=int(order),
                    file_name=chosen["name"], file_size=chosen["size"], content_type="video/mp4",
                    sha256=chosen["sha256"], allow_duplicate=action == "anyway",
                    allow_duplicate_title=hints.allow_duplicate_title,
                    **probe_fields(chosen),
                )
            except ApiError as exc:
                st.session_state.pop(_k("go_pending"), None)
                if exc.code == "DUPLICATE_VIDEO":
                    st.session_state[_k("duplicate")] = {"file": (chosen["name"], chosen["size"]), "existing": exc.details}
                    st.rerun()
                if exc.code == "DUPLICATE_TITLE":
                    st.error("Another video already has this title — change it, or tick “Use the same title anyway”.",
                             icon=":material/content_copy:")
                    return
                st.error(f"Upload could not start: {exc.message}", icon=":material/error:")
                return
        with api_errors("start the upload"):
            video_job = upload_job(client, video)
            doc_job = document_job(client, video.id) if attach else None
    st.session_state[_k("task")] = uploads.start(
        origin=uploads.ORIGIN_UPLOAD,
        label=video.title + (" + document" if doc_job else ""),
        title=video.title, video_id=video.id, video_job=video_job, doc_job=doc_job,
        thumbnail=((thumbnail.name, thumbnail.getvalue(), thumbnail.type or "image/jpeg") if thumbnail
                   else frame_thumbnail(st.session_state.get(_k("frame")), chosen)),
        publish=publish,
    )
    st.session_state.pop(_k("duplicate"), None)
    st.session_state.pop(_k("go_pending"), None)
    forget_all()           # categories / title checks describe the server as it was before this video existed
    st.rerun()


def _remember_click() -> None:
    st.session_state[_k("go_pending")] = True


def _box_event(event, file_key: str, also_clear: str | None = None) -> None:
    """File chosen / cleared / handed to the sidebar, from one of the page's boxes."""
    if not event:
        return
    kind = event["event"]
    if kind == "file":
        st.session_state[_k(file_key)] = event
        frame = st.session_state.get(_k("frame"))
        if frame and (frame.get("name"), frame.get("size")) != (event.get("name"), event.get("size")):
            st.session_state.pop(_k("frame"), None)       # a different file: its cover is not ours
        cover = event.get("cover")                         # the picture taken from the video, sent with the fingerprint
        if cover is not None:
            st.session_state[_k("frame")] = {"event": "frame", "name": event.get("name"), "size": event.get("size"),
                                             **cover}
    elif kind == "frame":      # the cover picture the browser took from the video
        st.session_state[_k("frame")] = event
        # It repeats the file record with its fingerprint. If the "file" event carrying the fingerprint was
        # missed (a slow rerun can swallow an event), take the fingerprint from here.
        known = st.session_state.get(_k(file_key))
        if event.get("sha256") and (known is None or (
                known.get("sha256") is None
                and (known.get("name"), known.get("size")) == (event.get("name"), event.get("size")))
                or (known.get("name"), known.get("size")) != (event.get("name"), event.get("size"))):
            record = {k: v for k, v in event.items() if k not in ("data", "width", "height", "event")}
            record["event"] = "file"
            st.session_state[_k(file_key)] = record
    elif kind == "cleared":
        st.session_state.pop(_k(file_key), None)
        st.session_state.pop(_k("frame"), None)
    elif kind == "handed":
        st.session_state.setdefault(_k("handed"), set()).add(event["role"])
    elif kind == "error" and event.get("message") == "handover failed":
        st.session_state[_k("handover_failed")] = True
    if also_clear and kind in ("file", "cleared"):
        st.session_state.pop(_k(also_clear), None)


def _handed_over(task: dict) -> bool:
    handed = st.session_state.get(_k("handed"), set())
    return (not task["video_job"] or "video" in handed) and (not task["doc_job"] or "document" in handed)


def _duplicate_for(chosen) -> dict | None:
    """The duplicate warning, if it is about the file currently chosen."""
    found = st.session_state.get(_k("duplicate"))
    if found and chosen and found["file"] == (chosen["name"], chosen["size"]):
        return found
    st.session_state.pop(_k("duplicate"), None)
    return None


def _duplicate_panel(found: dict) -> str | None:
    """Warn that this exact file exists. Returns "anyway", "resume" or None."""
    existing = found["existing"]
    status = existing.get("status")
    with st.container(border=True):
        if status == "uploading":
            st.warning(
                f"This exact file is already being uploaded as “{existing.get('title')}” "
                f"({existing.get('uploaded_fraction', 0):.0%} done). Resume that upload instead of starting again?",
                icon=":material/content_copy:",
            )
        else:
            st.warning(
                f"This exact file is already uploaded as “{existing.get('title')}” ({status}).",
                icon=":material/content_copy:",
            )
        cols = iter(st.columns(4 if status == "uploading" else 3))
        if status == "uploading" and next(cols).button("Resume that upload", type="primary", icon=":material/play_arrow:",
                                                       width="stretch", key=_k("dup.resume")):
            return "resume"
        if next(cols).button("Open existing", icon=":material/movie:", width="stretch", key=_k("dup.open")):
            st.switch_page(navigation.pages().video_detail, query_params={"id": existing["video_id"]})
        if next(cols).button("Upload anyway", icon=":material/upload:", width="stretch", key=_k("dup.anyway"),
                             help="Creates a second video from the same file."):
            return "anyway"
        if next(cols).button("Cancel", width="stretch", key=_k("dup.cancel")):
            st.session_state.pop(_k("duplicate"), None)
            st.rerun()
    return None
