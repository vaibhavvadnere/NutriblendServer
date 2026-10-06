"""api/legal.py — Privacy policy text and Google Play checklist (public endpoint)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from dashboard.api.client import ApiClient
from dashboard.config import settings


class ChecklistItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    item: str
    ok: bool
    hint: str = ""


class PrivacyPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")
    markdown: str
    version: str
    effective_date: Optional[str] = None
    policy_url: str
    deletion_url: str
    missing_fields: list[str] = []
    play_store_checklist: list[ChecklistItem] = []


def privacy_policy(client: ApiClient) -> PrivacyPolicy:
    return PrivacyPolicy.model_validate(client.get(settings.api_path("/legal/privacy-policy"), auth=False))
