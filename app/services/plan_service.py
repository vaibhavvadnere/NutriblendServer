"""
services/plan_service.py — Plans on sale.

The plan document is the single source of truth for price and duration: the app
only ever sends a plan id, and an order copies the plan's values at the moment
it is created, so editing a plan later never changes what an old order bought.
"""

from datetime import datetime, timezone

from app.core.config import settings
from app.core.exceptions import PlanNotFound
from app.repositories import plan_repo
from app.schemas.payment import PlanAdminOut, PlanCreate, PlanOut, PlanUpdate


def to_plan_out(doc: dict) -> PlanOut:
    return PlanOut(
        id=str(doc["_id"]),
        name=doc["name"],
        description=doc.get("description"),
        amount=doc["amount"],
        currency=doc.get("currency") or settings.PAYMENTS_CURRENCY,
        duration_days=doc["duration_days"],
    )


def to_plan_admin_out(doc: dict) -> PlanAdminOut:
    return PlanAdminOut(
        **to_plan_out(doc).model_dump(),
        active=bool(doc.get("active")),
        sort_order=doc.get("sort_order", 0),
        created_at=doc["created_at"],
        updated_at=doc.get("updated_at"),
    )


async def list_active() -> list[PlanOut]:
    return [to_plan_out(d) for d in await plan_repo.list_plans(active_only=True)]


async def list_all() -> list[PlanAdminOut]:
    return [to_plan_admin_out(d) for d in await plan_repo.list_plans(active_only=False)]


async def get_active(plan_id: str) -> dict:
    """A plan that can be bought right now. PlanNotFound for unknown or inactive."""
    doc = await plan_repo.find_by_id(plan_id)
    if not doc or not doc.get("active"):
        raise PlanNotFound()
    return doc


async def create(payload: PlanCreate) -> PlanAdminOut:
    now = datetime.now(timezone.utc)
    doc = await plan_repo.insert({
        "name": payload.name.strip(),
        "description": (payload.description or "").strip() or None,
        "amount": payload.amount,
        "currency": settings.PAYMENTS_CURRENCY,
        "duration_days": payload.duration_days,
        "sort_order": payload.sort_order,
        "active": payload.active,
        "created_at": now,
        "updated_at": now,
    })
    return to_plan_admin_out(doc)


async def update(plan_id: str, payload: PlanUpdate) -> PlanAdminOut:
    doc = await plan_repo.find_by_id(plan_id)
    if not doc:
        raise PlanNotFound()
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        fields["name"] = fields["name"].strip()
    if "description" in fields:
        fields["description"] = (fields["description"] or "").strip() or None
    fields = {k: v for k, v in fields.items() if v is not None or k == "description"}
    fields["updated_at"] = datetime.now(timezone.utc)
    return to_plan_admin_out(await plan_repo.update(doc["_id"], fields))
