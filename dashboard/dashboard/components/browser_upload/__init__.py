"""
components/browser_upload — Uploads that run in the admin's browser
(index.html + upload.js + hash_worker.js + hashes.js; no build step).

Files never pass through the dashboard: the browser fingerprints them and
sends them straight to storage (R2) with a ticket and signed links from the
API server. Works from any computer, for any file size.

Two modes of the same component:

    event = render(key, kind="video"|"document", task=None)   # a file box on a page
    event = manager(tasks)                                     # the sidebar, on every page

Box events (dicts; `seq` is unique — handle each once):
    {"event": "file", name, size, last_modified, sha256 (None while checking), mismatch,
                probe (videos): duration, width, height, video_codec, audio_codec, fast_start, warnings[{code, level, text}]}
    {"event": "cleared"}
    {"event": "handed", task, role}     the sidebar manager has the file
Manager events:
    {"event": "done", task, video, doc_error}     video = the server's video JSON
    {"event": "error" | "stopped", task, video_id, stage, message, code}
    {"event": "dismissed", task}

tasks: [{id, label, video_job?, doc_job?}] — see services/uploads.py
    video_job: {video_id, ticket, api, chunk_size, total_chunks, sha256, parallel}
    doc_job:   {video_id, ticket, api}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import streamlit.components.v1 as components

_component = components.declare_component("browser_upload", path=str(Path(__file__).parent))

VIDEO_EXTENSIONS = [".mp4", ".m4v"]
DOCUMENT_EXTENSIONS = [".pdf", ".docx", ".pptx"]
_ACCEPT = {
    "video": VIDEO_EXTENSIONS + ["video/mp4"],
    "document": DOCUMENT_EXTENSIONS + [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ],
}
PARALLEL_PIECES = 4


def render(
    key: str,
    *,
    task: Optional[dict] = None,
    expect: Optional[dict] = None,
    max_bytes: Optional[int] = None,
    label: Optional[str] = None,
    disabled: bool = False,
    kind: str = "video",
) -> Optional[dict[str, Any]]:
    """A file box on a page. kind "video" or "document". `expect` = {name, size,
    sha256} of the file an unfinished upload needs (resume). `task` = {id, role}
    hands the chosen file to the sidebar manager (services/uploads.py)."""
    return _component(
        key=key,
        default=None,
        mode="box",
        task=task,
        expect=expect,
        max_bytes=max_bytes,
        label=label or ("Video file" if kind == "video" else "Document file (PDF, Word or PowerPoint)"),
        disabled=disabled,
        extensions=VIDEO_EXTENSIONS if kind == "video" else DOCUMENT_EXTENSIONS,
        accept=",".join(_ACCEPT[kind]),
        probe=kind == "video",
    )


def manager(tasks: list[dict]) -> Optional[dict[str, Any]]:
    """The sidebar upload manager (render it on every page, same key)."""
    return _component(key="upload_manager", default=None, mode="manager", tasks=tasks)


def new_event(event: Optional[dict], seen_key: str) -> Optional[dict]:
    """The event if it hasn't been handled yet (component values repeat on every rerun)."""
    import streamlit as st

    if not event or not event.get("seq") or st.session_state.get(seen_key) == event["seq"]:
        return None
    st.session_state[seen_key] = event["seq"]
    return event
