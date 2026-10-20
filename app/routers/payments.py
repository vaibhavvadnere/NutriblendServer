"""
routers/payments.py — Plans, Razorpay orders and payment verification for the
mobile app (HTTP only; rules live in services/payment_service.py).

All endpoints need an app session and answer 404 PAYMENTS_DISABLED while
PAYMENTS_ENABLED is off. The Razorpay webhook (no session, signature-checked)
is added separately.
"""

import logging

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.deps import client_ip, get_current_user
from app.schemas.common import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.payment import CreateOrderRequest, OrderOut, PlanOut, SubscriptionOut, VerifyPaymentRequest
from app.services import payment_service, payment_webhook_service

logger = logging.getLogger("nutriblend.payments")

# Razorpay's events are a few KB; anything far bigger isn't one.
_MAX_WEBHOOK_BYTES = 256 * 1024

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/payments",
    tags=["payments"],
    responses=ERROR_RESPONSES,
)

# GET /users/me/subscription lives with the other "me" data but is served from here
# so everything payment-related stays in one module.
subscription_router = APIRouter(
    prefix=f"{settings.API_PREFIX}/users",
    tags=["payments"],
    responses=ERROR_RESPONSES,
)


@router.get("/plans", response_model=ApiResponse[list[PlanOut]])
async def list_plans(_: dict = Depends(get_current_user)):
    """Plans on sale, in display order. `amount` is in paise (₹199 = 19900)."""
    return ok(await payment_service.list_plans(), "Plans fetched successfully")


@router.post("/orders", response_model=ApiResponse[OrderOut], status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: CreateOrderRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    Start a purchase: creates a Razorpay order for the plan. Pass the response
    to Razorpay Checkout (`key_id`, `order_id`, `amount`, `currency`).

    The price always comes from the plan on the server — the app never sends one.

    - `404 PLAN_NOT_FOUND` — unknown or no longer on sale.
    - `429 RATE_LIMITED` — too many attempts.
    - `502 PAYMENT_PROVIDER_ERROR` — Razorpay unreachable; try again.
    """
    order = await payment_service.create_order(current_user, payload.plan_id, client_ip(request))
    return ok(order, "Order created")


@router.post("/verify", response_model=ApiResponse[SubscriptionOut])
async def verify_payment(payload: VerifyPaymentRequest, current_user: dict = Depends(get_current_user)):
    """
    Confirm a payment with what Razorpay Checkout returned on success. On
    success the subscription is active (or extended) and returned.

    Safe to call more than once for the same payment.

    - `404 ORDER_NOT_FOUND` — not an order of this user.
    - `400 PAYMENT_VERIFICATION_FAILED` — bad signature, amount mismatch, or the
      payment didn't succeed. Any money taken is refunded by Razorpay.
    - `409 PAYMENT_PENDING` — paid but not confirmed yet; check
      `GET /users/me/subscription` again in a minute.
    """
    subscription = await payment_service.verify(
        current_user, payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
    )
    return ok(subscription, "Payment verified")


@router.post("/webhook", include_in_schema=False)
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
):
    """
    Razorpay → us. No session: trusted only via X-Razorpay-Signature over the raw
    body. 200 = processed (or deliberately ignored), 400 = not a genuine Razorpay
    event, 500 = our failure (Razorpay retries).
    """
    raw = await request.body()
    if len(raw) > _MAX_WEBHOOK_BYTES:
        return JSONResponse({"success": False, "message": "Payload too large"}, status_code=413)
    try:
        result = await payment_webhook_service.handle(raw, x_razorpay_signature, x_razorpay_event_id)
    except payment_webhook_service.InvalidWebhook:
        return JSONResponse({"success": False, "message": "Invalid webhook"}, status_code=400)
    return {"success": True, "message": "ok", "data": {"result": result}}


@subscription_router.get("/me/subscription", response_model=ApiResponse[SubscriptionOut])
async def my_subscription(current_user: dict = Depends(get_current_user)):
    """The signed-in user's plan: `is_active`, `plan_name`, `expires_at`, `days_left`."""
    return ok(await payment_service.my_subscription(current_user), "Subscription fetched")
