"""A tiny per-session cache for small lookups the pages repeat on every rerun.

Streamlit reruns the whole page on every click and keystroke. With the server (and its database) far away,
three small lookups per rerun made the page feel frozen — and a slow rerun is exactly when a click or a
browser message can get lost. Values live in the admin's own session only and expire quickly.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import streamlit as st

_STORE = "_memo"


def remember(key: Any, ttl: float, fetch: Callable[[], Any]) -> Any:
    """`fetch()` at most once per `ttl` seconds for this key in this session. Errors are not cached."""
    store = st.session_state.setdefault(_STORE, {})
    hit = store.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fetch()
    store[key] = (now, value)
    if len(store) > 64:                       # never grow without bound
        for old in sorted(store, key=lambda k: store[k][0])[:32]:
            del store[old]
    return value


def forget_all() -> None:
    """Drop everything (after something changed on the server that these lookups describe)."""
    st.session_state.pop(_STORE, None)
