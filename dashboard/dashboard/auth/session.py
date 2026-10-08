"""
auth/session.py — Per-browser-session state for the signed-in admin.

Everything lives in st.session_state: memory only, never written to disk, and
gone when the browser tab is closed or refreshed. Streamlit keeps one
session_state per browser tab, so two tabs are two independent sessions.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx
import streamlit as st

from dashboard.api.client import ApiClient
from dashboard.config import settings
from dashboard.models import User

_ACCESS = "auth.access_token"
_REFRESH = "auth.refresh_token"
_EXPIRES_AT = "auth.expires_at"
_USER = "auth.user"


class SessionTokenStore:
    """TokenStore (see api/client.py) backed by st.session_state."""

    def get_access(self) -> Optional[str]:
        return st.session_state.get(_ACCESS)

    def get_refresh(self) -> Optional[str]:
        return st.session_state.get(_REFRESH)

    def get_expires_at(self) -> Optional[float]:
        return st.session_state.get(_EXPIRES_AT)

    def set_tokens(self, access: str, refresh: str, expires_in: int) -> None:
        st.session_state[_ACCESS] = access
        st.session_state[_REFRESH] = refresh
        st.session_state[_EXPIRES_AT] = time.time() + expires_in

    def clear(self) -> None:
        from dashboard.auth import remember

        remember.forget()          # a signed-out session must not be restorable after a refresh
        for key in (_ACCESS, _REFRESH, _EXPIRES_AT, _USER):
            st.session_state.pop(key, None)


@st.cache_resource(show_spinner=False)
def _shared_http() -> httpx.Client:
    """One connection pool for the whole process. It holds no credentials —
    tokens are attached per request — so sharing it across sessions is safe."""
    return httpx.Client(
        base_url=settings.server_url,
        timeout=settings.REQUEST_TIMEOUT_SECONDS,
        headers={"Accept": "application/json"},
    )


def get_client() -> ApiClient:
    """An ApiClient bound to *this* browser session's tokens."""
    return ApiClient(tokens=SessionTokenStore(), http=_shared_http())


def is_logged_in() -> bool:
    return bool(st.session_state.get(_REFRESH)) and st.session_state.get(_USER) is not None


def current_user() -> Optional[User]:
    return st.session_state.get(_USER)


def set_current_user(user: User) -> None:
    st.session_state[_USER] = user


def clear() -> None:
    """Forget everything about the signed-in user (local only)."""
    SessionTokenStore().clear()
