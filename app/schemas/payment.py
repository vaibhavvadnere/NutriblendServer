"""
schemas/payment.py — Request/response models for plans, payments and subscriptions.

Amounts are always integers in paise (₹999 = 99900), exactly as Razorpay uses them.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

OrderStatus = Literal["created", "paid", "failed", "refunded"]


# ── App ──────────────────────────────────────────────────────────────────────

class PlanOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    amount: int = Field(description="Price in paise (₹999 = 99900).")
    currency: str
    duration_days: int


class CreateOrderRequest(BaseModel):
    # Only the plan — the price is always taken from the database.
    plan_id: str = Field(min_length=1, max_length=64)


class OrderOut(BaseModel):
    """Everything the app needs to open Razorpay Checkout."""
    order_id: str
    amount: int
    currency: str
    key_id: str = Field(description="Razorpay Key ID (public).")
    plan_id: str
    plan_name: str


class VerifyPaymentRequest(BaseModel):
    """Exactly what Razorpay Checkout hands the app on success."""
    razorpay_order_id: str = Field(min_length=1, max_length=64)
    razorpay_payment_id: str = Field(min_length=1, max_length=64)
    razorpay_signature: str = Field(min_length=1, max_length=256)


class SubscriptionOut(BaseModel):
    is_active: bool
    plan_id: Optional[str] = None
    plan_name: Optional[str] = None
    started_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    days_left: int = 0


# ── Admin ────────────────────────────────────────────────────────────────────

class PlanAdminOut(PlanOut):
    active: bool
    sort_order: int
    created_at: datetime
    updated_at: Optional[datetime] = None


class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: Optional[str] = Field(default=None, max_length=500)
    # Razorpay's minimum is ₹1 (100 paise). Upper bound guards against typos.
    amount: int = Field(ge=100, le=10_000_000)
    duration_days: int = Field(ge=1, le=3660)
    sort_order: int = 0
    active: bool = True


class PlanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    description: Optional[str] = Field(default=None, max_length=500)
    # Changing the price only affects new orders; existing ones keep theirs.
    amount: Optional[int] = Field(default=None, ge=100, le=10_000_000)
    duration_days: Optional[int] = Field(default=None, ge=1, le=3660)
    sort_order: Optional[int] = None
    active: Optional[bool] = None


class PaymentOrderOut(BaseModel):
    id: str
    user_id: str
    mobile_number: Optional[str] = None
    plan_id: str
    plan_name: str
    duration_days: int
    amount: int
    currency: str
    razorpay_order_id: str
    razorpay_payment_id: Optional[str] = None
    status: OrderStatus
    paid_via: Optional[str] = None
    failure_reason: Optional[str] = None
    created_at: datetime
    paid_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None


class PaymentOrderPage(BaseModel):
    items: list[PaymentOrderOut]
    total: int
    page: int
    page_size: int
    pages: int


class GrantSubscriptionRequest(BaseModel):
    """Admin: give access without a payment (dealer bundles, support goodwill)."""
    days: int = Field(ge=1, le=3660)
    note: Optional[str] = Field(default=None, max_length=200)


class SubscriptionHistoryItem(BaseModel):
    at: datetime
    action: str                      # "extend" | "refund" | "end"
    days: Optional[int] = None
    source: Optional[str] = None     # "payment" | "admin"
    order_id: Optional[str] = None
    reason: Optional[str] = None
    by: Optional[str] = None         # admin's masked mobile for manual changes
    note: Optional[str] = None


class UserSubscriptionAdminOut(BaseModel):
    """Admin view of one user's access: current state + full change history."""
    subscription: SubscriptionOut
    source: Optional[str] = None
    ended_reason: Optional[str] = None
    history: list[SubscriptionHistoryItem] = []


class RevokeSubscriptionRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=200)
