"""
auth/remember.py — Stay signed in across page refreshes (but not across closing the tab).

Streamlit starts a brand-new session on every browser refresh, which used to sign the admin out. Now:

  * After signing in, this process stores the session's tokens under a random 256-bit key (in memory only).
  * The browser keeps just that key in sessionStorage (components/session_keeper): it survives a refresh and is
    erased when the tab is closed. No token ever reaches the browser.
  * After a refresh the key comes back, the tokens are looked up here and the session continues.

Entries expire after SESSION_REMEMBER_HOURS without use, are removed on log out, and disappear when the dashboard
restarts. Tokens still rotate and expire on the server exactly as before.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Optional

import streamlit as st

from dashboard.config import settings

_KEY = "auth.remember_key"          # this session's key (once signed in)
_CLEAR = "auth.remember_clear"      # ask the browser to erase its key
_TRIED = "auth.remember_tried"      # restoring was attempted in this session
_FIELDS = ("auth.access_token", "auth.refresh_token", "auth.expires_at", "auth.user")


@st.cache_resource(show_spinner=False)
def _store() -> dict:
    return {"lock": threading.Lock(), "entries": {}}


def enabled() -> bool:
    return settings.SESSION_REMEMBER_HOURS > 0


def _drop_expired(entries: dict, now: float) -> None:
    ttl = settings.SESSION_REMEMBER_HOURS * 3600
    for key in [k for k, e in entries.items() if now - e["seen"] > ttl]:
        del entries[key]


def sync() -> None:
    """Call on every run while signed in: keep the stored tokens current (they rotate) and make sure
    this session has a key."""
    if not enabled():
        return
    store = _store()
    now = time.time()
    with store["lock"]:
        _drop_expired(store["entries"], now)
        key = st.session_state.get(_KEY)
        if not key or key not in store["entries"]:
            key = secrets.token_urlsafe(32)
            st.session_state[_KEY] = key
        store["entries"][key] = {
            "seen": now,
            "data": {f: st.session_state.get(f) for f in _FIELDS},
        }


def current_key() -> Optional[str]:
    return st.session_state.get(_KEY) if enabled() else None


def restore(key: Optional[str]) -> bool:
    """Sign this session in again from the key the browser kept. True if it worked."""
    st.session_state[_TRIED] = True
    if not enabled() or not key:
        return False
    store = _store()
    now = time.time()
    with store["lock"]:
        _drop_expired(store["entries"], now)
        entry = store["entries"].get(key)
        if entry is None or not entry["data"].get("auth.refresh_token") or entry["data"].get("auth.user") is None:
            return False
        entry["seen"] = now
        for field, value in entry["data"].items():
            st.session_state[field] = value
        st.session_state[_KEY] = key
    return True


def tried() -> bool:
    return bool(st.session_state.get(_TRIED))


def forget() -> None:
    """Sign-out: remove the stored tokens and tell the browser to erase its key."""
    key = st.session_state.pop(_KEY, None)
    if key:
        store = _store()
        with store["lock"]:
            store["entries"].pop(key, None)
    st.session_state[_CLEAR] = True      # (the entry is gone, so a stale key from the browser cannot restore it)


def take_clear() -> bool:
    """True once after sign-out (or a failed restore): the keeper should erase the browser's key."""
    return bool(st.session_state.pop(_CLEAR, False))


def ask_browser_to_clear() -> None:
    st.session_state[_CLEAR] = True


def reported_key(event: Optional[dict[str, Any]]) -> Optional[str]:
    return (event or {}).get("stored") or None
