"""
components/video_ui.py — Pieces shared by the video screens: status badge,
category picker, file-source picker and the upload progress runner.
"""

from __future__ import annotations

from typing import Callable, Optional

import streamlit as st

from dashboard.models import Video
from dashboard.services.uploader import (
    DOCUMENT_EXTENSIONS,
    VIDEO_EXTENSIONS,
    Source,
    UploadError,
    source_from_path,
    videos_in_folder,
)
from dashboard.utils.formatting import fmt_bytes

STATUS_BADGE = {
    "published": ("Published", "green", ":material/public:"),
    "draft": ("Draft", "gray", ":material/edit_note:"),
    "uploading": ("Uploading", "orange", ":material/upload:"),
}
BROWSER_LIMIT_MB = 500

_NONE = "No category"
_NEW = "New category…"


def status_badge(video: Video) -> None:
    label, color, icon = STATUS_BADGE.get(video.status, (video.status.title(), "gray", None))
    st.badge(label, color=color, icon=icon)


def category_picker(existing: list[str], key: str, current: Optional[str] = None) -> Optional[str]:
    """Pick an existing category, none, or type a new one. Returns the value (or None)."""
    options = [_NONE, *existing, _NEW]
    index = options.index(current) if current in existing else 0
    choice = st.selectbox("Category", options, index=index, key=f"{key}.select")
    if choice == _NEW:
        typed = st.text_input("New category name", key=f"{key}.new", max_chars=40, placeholder="e.g. Recipes")
        return typed.strip() or None
    return None if choice == _NONE else choice


_FOLDERS = ["~/Downloads", "~/Movies", "~/Desktop", "~/Documents"]
_OTHER = "Other folder…"
_PASTE = "Paste a full path…"


def _mac_picker(key: str, extensions: tuple[str, ...], noun: str) -> tuple[Optional[Source], Optional[str]]:
    col_folder, col_file = st.columns([1, 2])
    folder_choice = col_folder.selectbox(
        "Folder", [*_FOLDERS, _OTHER, _PASTE], key=f"{key}.folder",
        index=None, placeholder="Choose a folder",   # nothing pre-selected
    )
    if folder_choice is None:
        col_file.selectbox(noun.capitalize(), [], key=f"{key}.waiting", disabled=True,
                           placeholder="Choose a folder first")
        return None, None

    if folder_choice == _PASTE:
        path = st.text_input(
            f"Full path to the {noun} file",
            key=f"{key}.path",
            placeholder="/Users/vaibhav/Downloads/lesson-1.mp4",
            help="In Finder, select the file and press Option+Command+C to copy its full path, then paste here. "
                 "(Dragging a file onto this box doesn't work — browsers don't pass file paths to web pages.)",
        )
        if not path.strip():
            return None, None
        try:
            source = source_from_path(path, extensions)
        except UploadError as exc:
            return None, exc.message
        st.caption(f"Selected: {source.name} · {fmt_bytes(source.size)}")
        return source, None

    folder = folder_choice
    if folder_choice == _OTHER:
        folder = col_folder.text_input("Folder path", key=f"{key}.other", placeholder="/Users/vaibhav/Videos")
        if not folder.strip():
            return None, None
    try:
        videos = videos_in_folder(folder, extensions)
    except UploadError as exc:
        return None, exc.message
    if not videos:
        col_file.selectbox(noun.capitalize(), [f"No {' / '.join(extensions)} files in this folder"], disabled=True, key=f"{key}.none")
        return None, None

    picked = col_file.selectbox(
        noun.capitalize(),
        videos,
        key=f"{key}.video",
        index=None,                                  # you pick the file; nothing is pre-selected
        placeholder=f"Choose a {noun} ({len(videos)} found)",
        format_func=lambda v: f"{v.name}  ·  {fmt_bytes(v.size)}",
        help=f"Newest first. Only {' / '.join(extensions)} files are listed.",
    )
    if picked is None:
        return None, None
    try:
        return source_from_path(picked.path, extensions), None
    except UploadError as exc:
        return None, exc.message


def source_picker(key: str, kind: str = "video") -> tuple[Optional[Source], Optional[str]]:
    """Where the file comes from (kind: "video" or "document"). Returns (source, error message)."""
    is_video = kind == "video"
    extensions = VIDEO_EXTENSIONS if is_video else DOCUMENT_EXTENSIONS
    noun = "video" if is_video else "document"
    mode = st.radio(
        "Video file" if is_video else "Document file",
        ["File on this Mac", "Browser upload"],
        key=f"{key}.mode",
        horizontal=True,
        captions=["Any size — pick it from a folder (best for large files)",
                  f"Drag & drop or browse — up to {BROWSER_LIMIT_MB} MB"],
    )
    if mode == "File on this Mac":
        return _mac_picker(key, extensions, noun)

    uploaded = st.file_uploader(
        "Drop an MP4 here or browse" if is_video else "Drop a PDF, Word or PowerPoint file here or browse",
        type=[e.lstrip(".") for e in extensions], key=f"{key}.file",
    )
    if uploaded is None:
        return None, None
    return Source(name=uploaded.name, size=uploaded.size, fh=uploaded), None


def run_with_progress(action: Callable[[Callable[[int, int, int, int], None]], Video]) -> Optional[Video]:
    """Run an upload, showing a progress bar. Returns the video, or None after
    showing the error (the upload can then be resumed from the Videos page)."""
    bar = st.progress(0.0, text="Starting upload…")

    def on_progress(sent: int, total: int, done: int, chunks: int) -> None:
        fraction = done / chunks if chunks else 1.0
        bar.progress(min(fraction, 1.0), text=f"Uploading… {fmt_bytes(sent)} of {fmt_bytes(total)} ({fraction:.0%})")

    try:
        video = action(on_progress)
    except UploadError as exc:
        bar.empty()
        hint = " You can resume it from the Videos page." if exc.video_id else ""
        st.error(f"Upload stopped: {exc.message}{hint}", icon=":material/error:")
        return None
    bar.progress(1.0, text="Upload complete")
    return video
