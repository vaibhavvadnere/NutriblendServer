"""
api/admin.py — Admin endpoints (/api/v1/admin/...). All require an admin account;
a non-admin gets Forbidden (403).
"""

from __future__ import annotations

from typing import Literal, Optional

from dashboard.api.client import ApiClient
from dashboard.config import settings
from dashboard.models import AdminStats, User, UserDetail, UserPage


def confirm_admin(client: ApiClient) -> User:
    """Raises Forbidden if the signed-in number is not in ADMIN_MOBILE_NUMBERS."""
    return User.model_validate(client.get(settings.api_path("/admin/me"))["user"])


def get_stats(client: ApiClient) -> AdminStats:
    return AdminStats.model_validate(client.get(settings.api_path("/admin/stats")))


def list_users(
    client: ApiClient,
    query: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> UserPage:
    params = {"page": page, "page_size": page_size}
    if query:
        params["q"] = query
    if status:
        params["status"] = status
    return UserPage.model_validate(client.get(settings.api_path("/users"), params=params))


def get_user(client: ApiClient, user_id: str) -> UserDetail:
    return UserDetail.model_validate(client.get(settings.api_path(f"/admin/users/{user_id}")))


def set_user_status(client: ApiClient, user_id: str, status: Literal["active", "blocked"]) -> UserDetail:
    data = client.patch(settings.api_path(f"/admin/users/{user_id}/status"), json={"status": status})
    return UserDetail.model_validate(data)
