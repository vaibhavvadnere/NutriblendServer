"""
navigation.py — The signed-in pages, created once per script run.

Kept in one place so a page can link to another with
`st.switch_page(navigation.pages().user_detail, query_params={...})`.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from dashboard.pages import overview, privacy, user_detail, users, video_detail, video_upload, videos


@dataclass(frozen=True)
class Pages:
    overview: st.Page
    users: st.Page
    user_detail: st.Page
    videos: st.Page
    video_upload: st.Page
    video_detail: st.Page
    privacy: st.Page

    def menu(self) -> list:
        return [self.overview, self.users, self.videos, self.video_upload, self.privacy,
                self.user_detail, self.video_detail]


_KEY = "_nav_pages"

#: url_path of the page shown in the previous run / in this run (see main.py).
PREVIOUS_PAGE = "_nav_previous"
CURRENT_PAGE = "_nav_current"
UPLOAD_PATH = "upload"


def track(page) -> None:
    """Call once per run with the page about to run, so pages can tell whether
    the user just arrived from somewhere else."""
    st.session_state[PREVIOUS_PAGE] = st.session_state.get(CURRENT_PAGE)
    st.session_state[CURRENT_PAGE] = getattr(page, "url_path", None)


def pages() -> Pages:
    """The Page objects registered with st.navigation in this run."""
    cached = st.session_state.get(_KEY)
    if not isinstance(cached, Pages) or not hasattr(cached, "privacy"):  # rebuilt after an app update
        st.session_state[_KEY] = Pages(
            overview=st.Page(overview.render, title="Dashboard", icon=":material/dashboard:", url_path="dashboard", default=True),
            users=st.Page(users.render, title="Users", icon=":material/group:", url_path="users"),
            # Reached from the Users table; not shown in the sidebar.
            user_detail=st.Page(user_detail.render, title="User details", icon=":material/person:", url_path="user", visibility="hidden"),
            videos=st.Page(videos.render, title="Videos", icon=":material/video_library:", url_path="videos"),
            video_upload=st.Page(video_upload.render, title="Upload video", icon=":material/upload:", url_path="upload"),
            video_detail=st.Page(video_detail.render, title="Video", icon=":material/movie:", url_path="video", visibility="hidden"),
            privacy=st.Page(privacy.render, title="Privacy policy", icon=":material/policy:", url_path="privacy"),
        )
    return st.session_state[_KEY]
