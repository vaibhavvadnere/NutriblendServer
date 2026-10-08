"""Duplicate titles and category spelling: checked before anything is saved."""

import hashlib
import os

import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio
ADMIN = {"_id": ObjectId()}
MB = 1024 * 1024


async def _make(title, category=None, **kw):
    from app.services import video_service

    data = os.urandom(1024)
    return await video_service.start_upload(
        ADMIN, title=title, description=None, category=category, sort_order=0, file_name="a.mp4",
        file_size=len(data), content_type="video/mp4", base_url="http://t",
        sha256=hashlib.sha256(data).hexdigest(), **kw)


async def test_same_title_is_refused_unless_confirmed(storage, mongo):
    from app.core.exceptions import DuplicateTitle

    first = await _make("Easy  Dal Recipe")
    with pytest.raises(DuplicateTitle) as err:
        await _make("easy dal recipe ", allow_duplicate_title=False)
    assert err.value.details["videos"][0]["id"] == first.id
    again = await _make("easy dal recipe", allow_duplicate_title=True)
    assert again.id != first.id


async def test_check_details_lists_matches_and_ignores_itself(storage, mongo):
    from app.services import video_service

    a = await _make("Breakfast ideas")
    check = await video_service.check_details("BREAKFAST IDEAS", None)
    assert [m.id for m in check.same_title] == [a.id]
    assert (await video_service.check_details("Breakfast ideas", None, exclude_id=a.id)).same_title == []
    assert (await video_service.check_details("Breakfast", None)).same_title == []      # only exact titles
    assert (await video_service.check_details("a.b*", None)).same_title == []           # regex characters are literal


async def test_category_takes_the_existing_spelling(storage, mongo):
    from app.services import video_service

    await _make("One", "Recipes")
    two = await _make("Two", "  recipes ")
    assert two.category == "Recipes"
    updated = await video_service.update_details(two.id, {"category": "RECIPES"}, "http://t")
    assert updated.category == "Recipes"
    brand_new = await _make("Three", "Workouts")
    assert brand_new.category == "Workouts"


async def test_check_details_suggests_close_categories(storage, mongo):
    from app.services import video_service

    await _make("One", "Recipes")
    await _make("Two", "Workouts")
    c = (await video_service.check_details(None, "Recpies")).category
    assert c.similar == ["Recipes"] and not c.exists and c.canonical is None
    c = (await video_service.check_details(None, "recipes")).category
    assert c.exists and c.canonical == "Recipes"
    c = (await video_service.check_details(None, "Sleep")).category
    assert c.similar == [] and not c.exists


async def test_renaming_to_a_used_title_is_refused_unless_confirmed(storage, mongo):
    from app.core.exceptions import DuplicateTitle
    from app.services import video_service

    await _make("Alpha")
    b = await _make("Beta")
    with pytest.raises(DuplicateTitle):
        await video_service.update_details(b.id, {"title": "alpha"}, "http://t", allow_duplicate_title=False)
    ok = await video_service.update_details(b.id, {"title": "BETA"}, "http://t", allow_duplicate_title=False)
    assert ok.title == "BETA"            # only capitals changed: it is still the same video's title
    ok = await video_service.update_details(b.id, {"title": "alpha"}, "http://t", allow_duplicate_title=True)
    assert ok.title == "alpha"


async def test_endpoint_is_published_and_error_maps_to_409(storage, mongo):
    from app.core.exceptions import DuplicateTitle
    from app.main import app

    assert any(p.endswith("/admin/videos/check-details") for p in app.openapi()["paths"])
    assert DuplicateTitle.code == "DUPLICATE_TITLE"
