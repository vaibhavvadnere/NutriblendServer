"""
api/payments.py — Plans, payments and user subscriptions (/api/v1/admin/...).
Amounts are in paise on the wire; pages convert to and from rupees.
"""

from __future__ import annotations

from typing import Any, Optional

from dashboard.api.client import ApiClient
from dashboard.config import settings
from dashboard.models import PaymentOrderPage, Plan, UserSubscription


def list_plans(client: ApiClient) -> list[Plan]:
    return [Plan.model_validate(p) for p in client.get(settings.api_path("/admin/plans"))]


def create_plan(client: ApiClient, **fields: Any) -> Plan:
    return Plan.model_validate(client.post(settings.api_path("/admin/plans"), json=fields))


def update_plan(client: ApiClient, plan_id: str, **fields: Any) -> Plan:
    return Plan.model_validate(client.patch(settings.api_path(f"/admin/plans/{plan_id}"), json=fields))


def list_payments(
    client: ApiClient, status: Optional[str] = None, user_id: Optional[str] = None, page: int = 1, page_size: int = 20
) -> PaymentOrderPage:
    params: dict[str, Any] = {"page": page, "page_size": page_size}
    if status:
        params["status"] = status
    if user_id:
        params["user_id"] = user_id
    return PaymentOrderPage.model_validate(client.get(settings.api_path("/admin/payments"), params=params))


def get_user_subscription(client: ApiClient, user_id: str) -> UserSubscription:
    return UserSubscription.model_validate(client.get(settings.api_path(f"/admin/users/{user_id}/subscription")))


def grant(client: ApiClient, user_id: str, days: int, note: Optional[str]) -> UserSubscription:
    data = client.post(settings.api_path(f"/admin/users/{user_id}/subscription/grant"), json={"days": days, "note": note or None})
    return UserSubscription.model_validate(data)


def revoke(client: ApiClient, user_id: str, note: Optional[str]) -> UserSubscription:
    data = client.post(settings.api_path(f"/admin/users/{user_id}/subscription/revoke"), json={"note": note or None})
    return UserSubscription.model_validate(data)


def check_with_razorpay(client: ApiClient, razorpay_order_id: str) -> dict:
    """POST /admin/payments/{order}/check → {"result": ..., "order": {...}}."""
    return client.post(settings.api_path(f"/admin/payments/{razorpay_order_id}/check"))
