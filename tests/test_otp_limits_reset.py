"""Development restarts clear the OTP request limits; production keeps them."""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


async def _seed(mongo, monkeypatch):
    from app.repositories import rate_limit_repo
    monkeypatch.setattr(rate_limit_repo, "rate_limits_collection", mongo["rate_limits"])
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    await mongo["rate_limits"].insert_many([
        {"_id": "a", "scope": "otp_cooldown", "identifier": "+911", "count": 1, "expires_at": exp},
        {"_id": "b", "scope": "otp_ip_hour", "identifier": "1.2.3.4", "count": 5, "expires_at": exp},
        {"_id": "c", "scope": "something_else", "identifier": "x", "count": 1, "expires_at": exp},
    ])


async def test_development_start_clears_otp_limits(mongo, monkeypatch):
    from app.core.config import settings
    from app.services import rate_limiter

    monkeypatch.setattr(settings, "ENV", "development")
    await _seed(mongo, monkeypatch)
    await rate_limiter.reset_otp_limits_on_startup()
    left = [d["_id"] async for d in mongo["rate_limits"].find()]
    assert left == ["c"]


async def test_production_start_keeps_limits(mongo, monkeypatch):
    from app.core.config import settings
    from app.services import rate_limiter

    monkeypatch.setattr(settings, "ENV", "production")
    await _seed(mongo, monkeypatch)
    await rate_limiter.reset_otp_limits_on_startup()
    assert await mongo["rate_limits"].count_documents({}) == 3
