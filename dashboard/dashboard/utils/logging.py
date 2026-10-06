"""
utils/logging.py — Configure logging once per process.

Streamlit re-runs main.py on every interaction, so setup is guarded.
"""

from __future__ import annotations

import logging

from dashboard.config import settings

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root = logging.getLogger("dashboard")
    root.setLevel(settings.LOG_LEVEL.upper())
    root.addHandler(handler)
    root.propagate = False
    # httpx logs full URLs at INFO; keep it quiet unless debugging.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured = True
