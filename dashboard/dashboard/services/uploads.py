"""
services/uploads.py — Uploads that keep going while the admin moves between pages.

The sidebar's upload manager (components/browser_upload, mode "manager") does
the actual uploading in the browser. Python keeps the list of tasks in
st.session_state and finishes them when the manager reports back — whichever
page is open at that moment:

    task_id = start(origin="upload", label=..., video_job=..., doc_job=..., thumbnail=..., publish=...)
      -> the page's upload boxes hand their files to the manager
      -> manager: "done" / "error" / "stopped" / "dismissed"
      -> handle(event): thumbnail + publish (on success), a note in the sidebar, and a result
         the pages can show (result_for / results_for_video).

A task: {id, origin, label, title, video_id, video_job?, doc_job?, thumbnail?,
publish, state: running|failed|stopped, error?}.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

import streamlit as st

logger = logging.getLogger("dashboard.uploads")

_TASKS = "uploads.tasks"
_RESULTS = "uploads.results"
_NOTICES = "uploads.notices"
SEEN = "uploads.seen"

ORIGIN_UPLOAD = "upload"        # Upload video page (video, maybe + document)
ORIGIN_RESUME = "resume"        # a video's page: Resume upload
ORIGIN_DOCUMENT = "document"    # a video's page: Attach / Replace document
ORIGIN_REPLACE = "replace"      # a video's page: Replace video file (task.video_id = the video; the job's = the new upload)


def _tasks() -> dict[str, dict]:
    return st.session_state.setdefault(_TASKS, {})


def _results() -> dict[str, dict]:
    return st.session_state.setdefault(_RESULTS, {})


def start(
    *,
    origin: str,
    label: str,
    title: str,
    video_id: str,
    video_job: Optional[dict] = None,
    doc_job: Optional[dict] = None,
    thumbnail: Optional[tuple[str, bytes, str]] = None,
    publish: bool = False,
) -> str:
    """Register an upload; the page's boxes then hand their files to the manager."""
    task_id = uuid.uuid4().hex
    _tasks()[task_id] = {
        "id": task_id, "origin": origin, "label": label, "title": title, "video_id": video_id,
        "video_job": video_job, "doc_job": doc_job, "thumbnail": thumbnail, "publish": publish,
        "state": "running", "error": None,
    }
    return task_id


def get(task_id: Optional[str]) -> Optional[dict]:
    return _tasks().get(task_id) if task_id else None


def active(*, video_id: Optional[str] = None, origin: Optional[str] = None) -> list[dict]:
    return [t for t in _tasks().values()
            if (video_id is None or t["video_id"] == video_id) and (origin is None or t["origin"] == origin)]


def manager_defs() -> list[dict[str, Any]]:
    """What the sidebar manager needs (no thumbnails — they stay in Python)."""
    return [{"id": t["id"], "label": t["label"], "video_job": t["video_job"], "doc_job": t["doc_job"]}
            for t in _tasks().values()]


def result_for(task_id: Optional[str], pop: bool = True) -> Optional[dict]:
    if not task_id:
        return None
    return _results().pop(task_id, None) if pop else _results().get(task_id)


def take_results(origin: str) -> list[dict]:
    """Finished results of uploads started from `origin` (removed once returned), oldest first."""
    out = []
    for task_id, r in list(_results().items()):
        if r["origin"] == origin:
            out.append(_results().pop(task_id))
    return out


def results_for_video(video_id: str, origin: Optional[str] = None) -> list[dict]:
    """Finished results for this video (removed once returned)."""
    out = []
    for task_id, r in list(_results().items()):
        if r["video_id"] == video_id and (origin is None or r["origin"] == origin):
            out.append(_results().pop(task_id))
    return out


def _notice(task: dict, text: str, ok: bool) -> None:
    """A short note under Uploads in the sidebar (every page) until the admin clears it.
    (Not st.toast: a toast shown in a run started by a component event doesn't reach the browser.)"""
    notices = st.session_state.setdefault(_NOTICES, [])
    notices[:] = [n for n in notices if n["task"] != task["id"]][-4:]
    notices.append({"task": task["id"], "video_id": task["video_id"], "text": text, "ok": ok})


def sidebar_notices() -> None:
    """Render the notes (call inside `with st.sidebar:` on every page)."""
    from dashboard import navigation

    notices = st.session_state.get(_NOTICES, [])
    for n in list(notices):
        (st.success if n["ok"] else st.warning)(n["text"], icon=":material/check_circle:" if n["ok"] else ":material/warning:")
        left, right = st.columns(2)
        if left.button("Open video", key=f"uploads.open.{n['task']}", width="stretch"):
            notices.remove(n)
            st.switch_page(navigation.pages().video_detail, query_params={"id": n["video_id"]})
        if right.button("OK", key=f"uploads.ok.{n['task']}", width="stretch"):
            notices.remove(n)
            st.rerun()


def handle(event: Optional[dict]) -> bool:
    """Process one event from the manager. Returns True if something changed."""
    if not event or event.get("event") not in ("done", "error", "stopped", "dismissed"):
        return False
    task = _tasks().get(event.get("task"))
    if task is None:
        return False
    kind = event["event"]
    if kind == "dismissed":
        _tasks().pop(task["id"], None)
        st.session_state[_NOTICES] = [n for n in st.session_state.get(_NOTICES, []) if n["task"] != task["id"]]
        return True
    if kind in ("error", "stopped") and task["origin"] == ORIGIN_REPLACE and _replacement_swapped(task):
        kind, event = "done", {**event, "event": "done"}       # the swap had already happened (e.g. the reply was lost)
    if kind in ("error", "stopped"):
        task["state"] = "failed" if kind == "error" else "stopped"
        task["error"] = event.get("message")
        what = "document" if event.get("stage") == "document" else "upload"
        message = (f"Upload of “{task['title']}” stopped." if kind == "stopped"
                   else f"The {what} of “{task['title']}” failed: {task['error']}")
        _notice(task, f"{message} Retry it in Uploads above.", ok=False)
        _results()[task["id"]] = {"task": task["id"], "origin": task["origin"], "video_id": task["video_id"],
                                  "title": task["title"], "ok": False, "notes": [message]}
        return True

    _tasks().pop(task["id"], None)
    _results()[task["id"]] = _finish(task, event)
    r = _results()[task["id"]]
    _notice(task, r["notes"][0] if r["ok"] else " ".join(r["notes"][:2]), ok=r["ok"])
    return True


def _replacement_swapped(task: dict) -> bool:
    """For a "replace video file" task: did the new file already become the video's file?"""
    from dashboard.api import videos as videos_api
    from dashboard.api.errors import ApiError
    from dashboard.auth import session

    try:
        rep = videos_api.get_video(session.get_client(), task["video_id"]).replacement
    except ApiError:
        return False
    # swapped in already, or fully received and now being optimized on the server (the swap follows by itself)
    return rep is None or rep.optimization is not None


def _finish(task: dict, event: dict) -> dict:
    """Upload done: thumbnail and publish (unless the document failed)."""
    from dashboard.api import videos as videos_api
    from dashboard.api.errors import ApiError
    from dashboard.auth import session
    from dashboard.models import Video

    client = session.get_client()
    video = Video.model_validate(event["video"]) if event.get("video") else None
    doc_error = event.get("doc_error")
    title = task["title"]
    notes: list[str] = []
    ok = True
    if video is None:
        # The browser found nothing left to upload: check what the server says.
        try:
            video = videos_api.get_video(client, task["video_id"])
        except ApiError as exc:
            return {"task": task["id"], "origin": task["origin"], "video_id": task["video_id"], "title": title,
                    "ok": False, "notes": [f"Couldn't check “{title}” after uploading: {exc.message}"]}
        pending_replace = (task["origin"] == ORIGIN_REPLACE and video.replacement is not None
                           and video.replacement.optimization is None)
        if video.status == "uploading" or pending_replace:
            return {"task": task["id"], "origin": task["origin"], "video_id": task["video_id"], "title": title,
                    "ok": False, "notes": [f"The upload of “{title}” could not be finished (it may have expired). "
                                           "Open the video to resume it or delete it."]}
    optimizing = bool(video and video.optimizing)
    if task["origin"] == ORIGIN_REPLACE:
        target = video
        if video is not None and video.replaces:        # the reply is the received upload: look at the video itself
            try:
                target = videos_api.get_video(client, task["video_id"])
            except ApiError:
                target = None
        if target is not None and target.replacement is not None:
            notes.append(f"The new file for “{title}” was received and is being optimized — it replaces the "
                         "current file automatically when ready (the current one keeps playing until then).")
        else:
            notes.append(f"The video file of “{title}” was replaced — the new file is live and the old one is deleted.")
    elif task["origin"] == ORIGIN_DOCUMENT:
        notes.append(f"Document uploaded for “{title}”.")
    elif task["origin"] == ORIGIN_RESUME:
        notes.append(f"“{title}” finished uploading — it is now a draft.")
    elif optimizing:
        notes.append(f"“{title}” uploaded — now being optimized into a smaller file. It stays a draft until that is done.")
    else:
        notes.append(f"“{title}” uploaded — saved as a draft.")
    doc = video.admin_document if video else None
    if task["doc_job"] and doc_error:
        ok = False
        notes.append(f"The video is uploaded, but the document wasn't ({doc_error}). "
                     "Attach it on the video's page — the video was left as a draft.")
    elif task["doc_job"] and doc:
        notes.append(f"Document “{doc.name}” attached — it's being prepared for viewing (view-only)."
                     if doc.status == "processing" else
                     f"Document “{doc.name}” attached ({doc.page_count} pages, view-only).")
    try:
        if task.get("thumbnail"):
            name, data, content_type = task["thumbnail"]
            videos_api.set_thumbnail(client, task["video_id"], name, data, content_type)
            notes.append("Thumbnail saved.")
        if task.get("publish") and not doc_error:
            # With a document still being prepared the video waits (server side) and goes live by itself.
            # While the file is being optimized or the document prepared the video waits (server side)
            # and goes live by itself.
            done = videos_api.set_status(client, task["video_id"], "published", when_ready=True)
            if done.publish_when_ready:
                what = " and ".join(([ "it is optimized"] if done.optimizing else []) + (["its document is ready"] if task["doc_job"] else []))
                notes[0] = f"“{title}” uploaded — it will be published automatically as soon as {what or 'it is ready'}."
            else:
                notes[0] = f"“{title}” is published and visible in the app."
    except ApiError as exc:
        ok = False
        notes.append(f"Uploaded, but a follow-up step failed: {exc.message} — finish it on the video's page.")
    logger.info("Upload task %s finished for video %s (ok=%s)", task["id"], task["video_id"], ok)
    return {"task": task["id"], "origin": task["origin"], "video_id": task["video_id"], "title": title,
            "ok": ok, "notes": notes}
