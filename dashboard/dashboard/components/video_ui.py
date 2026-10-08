"""
components/video_ui.py — Pieces shared by the video screens: status badge,
category picker, and the jobs handed to the browser upload box
(components/browser_upload) for a video file or a document.

Files are always chosen in the admin's browser — never read from the disk of
the machine the dashboard runs on (on AWS that would be the server's disk).
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

from dashboard.models import Video

STATUS_BADGE = {
    "published": ("Published", "green", ":material/public:"),
    "draft": ("Draft", "gray", ":material/edit_note:"),
    "uploading": ("Uploading", "orange", ":material/upload:"),
}

_NONE = "No category"
_NEW = "New category…"


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


class Hints:
    """Result of `details_hints`: Save/Upload must stay disabled while `blocked`."""

    def __init__(self, blocked: bool = False, allow_duplicate_title: bool = False):
        self.blocked = blocked
        self.allow_duplicate_title = allow_duplicate_title


def details_hints(client, *, title: str, category: Optional[str], key: str, video: Optional[Video] = None,
                  category_key: Optional[str] = None) -> "Hints":
    """Warnings while the admin types: another video with this title, and a category that looks like a
    typo of an existing one. A same title or a new-looking category must be confirmed with a tick. The
    server checks again, so a failed lookup here only means fewer hints."""
    from dashboard.api import videos as videos_api
    from dashboard.api.errors import ApiError

    title_changed = bool(title.strip()) and (video is None or _norm(title) != _norm(video.title))
    cat_changed = bool(category) and (video is None or _norm(category) != _norm(video.category))
    if not (title_changed or cat_changed):
        return Hints()
    try:
        from dashboard.services.memo import remember
        args = (title.strip() if title_changed else None, category if cat_changed else None,
                video.id if video else None)
        found = remember(("check-details", *args), 20,
                         lambda: videos_api.check_details(client, args[0], args[1], exclude_id=args[2]))
    except ApiError:
        return Hints()
    hints = Hints()

    same = found.get("same_title") or []
    if same:
        first = same[0]
        more = f" (and {len(same) - 1} more)" if len(same) > 1 else ""
        st.warning(f"A video called “{first['title']}” already exists ({first['status']}){more}.",
                   icon=":material/content_copy:")
        hints.allow_duplicate_title = st.checkbox("Use the same title anyway", key=f"{key}.same_title.{_norm(title)}")
        hints.blocked = not hints.allow_duplicate_title

    cat = found.get("category") or {}
    if cat_changed and cat.get("canonical") and cat["canonical"] != category:
        st.info(f"Will be saved as “{cat['canonical']}” — the spelling already in use.", icon=":material/spellcheck:")
    elif cat_changed and cat.get("similar") and not cat.get("exists"):
        names = cat["similar"]
        st.warning(f"“{category}” is a new category. Did you mean {' or '.join(f'“{n}”' for n in names)}?",
                   icon=":material/spellcheck:")
        for n in names:
            if category_key:
                st.button(f"Use “{n}”", key=f"{key}.use.{n}", on_click=_use_category, args=(category_key, n))
        ok = st.checkbox(f"“{category}” is correct — create it as a new category", key=f"{key}.new_cat.{_norm(category)}")
        hints.blocked = hints.blocked or not ok
    return hints


def _use_category(category_key: str, name: str) -> None:
    st.session_state[f"{category_key}.select"] = name
    st.session_state.pop(f"{category_key}.new", None)


def optimization_summary(video: Video) -> Optional[str]:
    """One line about what optimization did to a finished video (None when it didn't run)."""
    from dashboard.utils.formatting import fmt_bytes

    o = video.optimization
    if not o or o.state != "done" or not o.source_size or not o.output_size:
        return None
    if o.mode == "kept":
        return f"Stored as uploaded ({fmt_bytes(o.output_size)}) — it was already efficient."
    what = "Converted to H.264" if o.mode == "converted" else "Optimized"
    return (f"{what}: {fmt_bytes(o.source_size)} uploaded → {fmt_bytes(o.output_size)} stored"
            + (f" ({o.saved_percent}% smaller)" if o.saved_percent else "") + ". The original was not stored.")


def status_badge(video: Video) -> None:
    label, color, icon = STATUS_BADGE.get(video.status, (video.status.title(), "gray", None))
    if video.hidden_in_app:
        label, color, icon = "Published · hidden (document)", "orange", ":material/visibility_off:"
    elif video.optimizing:
        pct = f" {video.optimization.progress:.0%}" if video.optimization.state == "running" else ""
        label, color, icon = f"Optimizing{pct}", "blue", ":material/compress:"
    elif video.optimization_failed:
        label, color, icon = "Optimization failed", "red", ":material/error:"
    elif video.publish_when_ready and video.status == "draft":
        label, color, icon = "Draft · publishes when ready", "blue", ":material/schedule:"
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


def upload_job(client, video: Video) -> dict:
    """Everything the browser upload manager needs to send this video's file:
    a ticket (outlives the 15-minute dashboard session) and the chunk plan."""
    from dashboard.api import videos as videos_api
    from dashboard.components.browser_upload import PARALLEL_PIECES
    from dashboard.config import settings

    ticket = videos_api.upload_ticket(client, video.id)
    plan = video.upload or videos_api.upload_progress(client, video.id)
    return {
        "video_id": video.id,
        "ticket": ticket["ticket"],
        "api": settings.browser_api_base,
        "chunk_size": plan.chunk_size,
        "total_chunks": plan.total_chunks,
        "sha256": video.sha256,
        "parallel": PARALLEL_PIECES,
    }


def document_job(client, video_id: str) -> dict:
    """What the browser upload manager needs to send this video's document."""
    from dashboard.api import videos as videos_api
    from dashboard.config import settings

    ticket = videos_api.document_ticket(client, video_id)
    return {"video_id": video_id, "ticket": ticket["ticket"], "api": settings.browser_api_base}


# ffprobe's spelling of the codecs the browser reader can name.
_CODEC_NAMES = {"H.264": "h264", "HEVC (H.265)": "hevc", "AV1": "av1", "VP9": "vp9", "VP8": "vp8",
                "MPEG-4 Visual": "mpeg4", "H.263": "h263", "Motion JPEG": "mjpeg", "ProRes": "prores"}


def probe_fields(file_event: dict | None) -> dict:
    """Length and codec the browser read from the chosen video, as start-upload fields."""
    probe = (file_event or {}).get("probe") or {}
    fields: dict = {}
    duration = probe.get("duration")
    if isinstance(duration, (int, float)) and 0 < duration <= 86_400:
        fields["duration_seconds"] = round(float(duration), 2)
    codec = _CODEC_NAMES.get(probe.get("video_codec") or "")
    if codec:
        fields["video_codec"] = codec
    return fields


def broken_file_warning(file_event: dict | None, *, key: str) -> bool:
    """True while the chosen video looks broken (not an MP4, cut short, no index, no picture)
    and the admin has not ticked the box to upload it anyway. The reasons show in the file box."""
    probe = (file_event or {}).get("probe") or {}
    if not any(w.get("level") == "error" for w in probe.get("warnings", [])):
        return False
    return not st.checkbox("Upload it anyway — I understand it may not play", key=key)


_MAX_COVER_BYTES = 5 * 1024 * 1024


def frame_thumbnail(frame_event: dict | None, chosen: dict | None) -> tuple[str, bytes, str] | None:
    """The cover picture the browser took from the chosen video, as (name, bytes, content type) —
    only if it belongs to the file that is chosen now."""
    import base64

    if not frame_event or not chosen or not frame_event.get("data"):
        return None
    if (frame_event.get("name"), frame_event.get("size")) != (chosen.get("name"), chosen.get("size")):
        return None
    prefix = "data:image/jpeg;base64,"
    data = frame_event["data"]
    if not isinstance(data, str) or not data.startswith(prefix):
        return None
    try:
        raw = base64.b64decode(data[len(prefix):], validate=True)
    except ValueError:
        return None
    if not raw.startswith(b"\xff\xd8") or len(raw) > _MAX_COVER_BYTES:
        return None
    return "cover.jpg", raw, "image/jpeg"
