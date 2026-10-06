"""
api/system.py — Server health (GET /api/health, unversioned).
"""

from __future__ import annotations

from dashboard.api.client import ApiClient
from dashboard.models import Health


def health(client: ApiClient) -> Health:
    return Health.model_validate(client.get("/api/health", auth=False))
