"""
session_keeper — a hidden component that keeps ONE random key in the browser's sessionStorage.

sessionStorage survives a page refresh but is erased when the tab is closed, which is exactly the behaviour
wanted: refresh = still signed in, close the tab and come back = sign in again. The tokens themselves are never
sent to the browser (see dashboard/auth/remember.py); only the key that points at them.

    reported = keeper(store_key="abc", clear=False)
    reported -> None until the browser has answered, then {"seq": ..., "stored": <key or None>}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import streamlit.components.v1 as components

_component = components.declare_component("session_keeper", path=str(Path(__file__).parent))


def keeper(*, store_key: Optional[str], clear: bool) -> Optional[dict[str, Any]]:
    return _component(key="session_keeper", default=None, store_key=store_key, clear=clear)
