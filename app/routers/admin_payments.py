"""
routers/admin_payments.py — Dashboard endpoints for plans, payments and user
subscriptions (admin session required; rules in services/plan_service.py and
services/admin_payment_service.py).

These work even while PAYMENTS_ENABLED is off.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, status

from app.core.config import settings
from app.core.deps import require_admin
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.payment import (
    GrantSubscriptionRequest,
    PaymentOrderPage,
    PlanAdminOut,
    PlanCreate,
    PlanUpdate,
    RevokeSubscriptionRequest,
    UserSubscriptionAdminOut,
)
from app.services import admin_payment_service, plan_service

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/admin",
    tags=["admin-payments"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_admin)],
)


# ── Plans ────────────────────────────────────────────────────────────────────

@router.get("/plans", response_model=ApiResponse[list[PlanAdminOut]])
async def list_plans():
    """Every plan, including inactive ones, in display order."""
    return ok(await plan_service.list_all(), "Plans fetched")


@router.post("/plans", response_model=ApiResponse[PlanAdminOut], status_code=status.HTTP_201_CREATED)
async def create_plan(payload: PlanCreate):
    """New plan. `amount` in paise (₹199 = 19900), `duration_days` 1–3660."""
    return ok(await plan_service.create(payload), "Plan created")


@router.patch("/plans/{plan_id}", response_model=ApiResponse[PlanAdminOut])
async def update_plan(plan_id: str, payload: PlanUpdate):
    """Edit a plan. Price/duration changes apply to NEW orders only. Plans are
    never deleted (old orders refer to them) — set `active: false` to stop selling."""
    return ok(await plan_service.update(plan_id, payload), "Plan updated")


# ── Payments ─────────────────────────────────────────────────────────────────

@router.get("/payments", response_model=ApiResponse[PaymentOrderPage])
async def list_payments(
    status: Optional[Literal["created", "paid", "failed", "refunded"]] = Query(default=None),
    user_id: Optional[str] = Query(default=None, max_length=24),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=admin_payment_service.MAX_PAGE_SIZE),
):
    """Orders newest first. `created` = checkout opened but not paid (yet)."""
    return ok(await admin_payment_service.list_orders(status, user_id, page, page_size), "Payments fetched")


# ── One user's access ────────────────────────────────────────────────────────

@router.get("/users/{user_id}/subscription", response_model=ApiResponse[UserSubscriptionAdminOut])
async def user_subscription(user_id: str):
    """Current access and the full history of changes (purchases, refunds, grants)."""
    return ok(await admin_payment_service.user_subscription(user_id), "Subscription fetched")


@router.post("/users/{user_id}/subscription/grant", response_model=ApiResponse[UserSubscriptionAdminOut])
async def grant_subscription(user_id: str, payload: GrantSubscriptionRequest, admin: dict = Depends(require_admin)):
    """Give `days` of access without a payment (dealer bundles, support). Adds to any current access."""
    return ok(await admin_payment_service.grant(user_id, payload.days, payload.note, admin), "Access granted")


@router.post("/users/{user_id}/subscription/revoke", response_model=ApiResponse[UserSubscriptionAdminOut])
async def revoke_subscription(user_id: str, payload: RevokeSubscriptionRequest, admin: dict = Depends(require_admin)):
    """End access now. Does not refund — refunds are made in the Razorpay dashboard."""
    return ok(await admin_payment_service.revoke(user_id, payload.note, admin), "Access revoked")
