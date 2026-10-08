"""
video_table — the Videos list as a table where the WHOLE row is the click target.

st.dataframe can only highlight one cell (or show a checkbox column), so the list is drawn by this small
component: rows light up on hover, a click (or Enter on a focused row) opens the video, column headers sort the
rows on the page. Nothing leaves the browser except the click: the component answers {"seq", "id"}.

    event = render(key=..., rows=[{"id": "...", "Title": ..., "Category": ..., "Status": ..., "tone": "draft",
                                    "Document": ..., "Duration": ..., "Size": ..., "Order": 0, "Added": ...,
                                    "sort": {"Duration": 12.0, "Size": 1234, "Added": 1700000000}}])
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import streamlit.components.v1 as components

_component = components.declare_component("video_table", path=str(Path(__file__).parent))

COLUMNS = ["Title", "Category", "Status", "Document", "Duration", "Size", "Order", "Added"]
RIGHT_ALIGNED = ["Order"]
TONES = ("published", "draft", "uploading", "optimizing", "problem")


def render(*, key: str, rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    return _component(key=key, default=None, rows=rows, columns=COLUMNS, right=RIGHT_ALIGNED)
