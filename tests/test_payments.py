"""
Payments: plans, Razorpay orders, verification, webhooks, subscriptions,
admin tools, account deletion and the video lock.

Razorpay is replaced by the mock provider (same HMAC signatures, fixed test
secrets) and MongoDB by mongomock, so nothing here touches the network.
Includes the security cases from the go-live checklist: tampered signature,
someone else's order, wrong amount, forged / replayed webhooks, price from the
client ignored, concurrent confirmations never granting twice.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

USER_MOBILE = "9876543210"
ADMIN_MOBILE = "8484844053"


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def pay(monkeypatch):
    """In-memory database for every collection payments touch, mock Razorpay,
    payments on, rate limits off. Returns a small namespace of helpers."""
    from mongomock_motor import AsyncMongoMockClient

    from app.core import deps
    from app.core.config import settings
    from app.providers import payments
    from app.repositories import (
        favourite_repo, otp_repo, payment_event_repo, payment_repo, plan_repo, refresh_token_repo,
        subscription_repo, user_repo, video_repo,
    )

    db = AsyncMongoMockClient()["payments_test"]
    for module, attr, name in [
        (plan_repo, "plans_collection", "plans"),
        (payment_repo, "payment_orders_collection", "payment_orders"),
        (subscription_repo, "subscriptions_collection", "subscriptions"),
        (payment_event_repo, "payment_events_collection", "payment_events"),
        (user_repo, "users_collection", "users"),
        (otp_repo, "otps_collection", "otps"),
        (refresh_token_repo, "refresh_tokens_collection", "refresh_tokens"),
        (video_repo, "videos_collection", "videos"),
        (favourite_repo, "favourites_collection", "favourites"),
    ]:
        monkeypatch.setattr(module, attr, db[name])
    monkeypatch.setattr(settings, "PAYMENTS_ENABLED", True)
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "mock")
    monkeypatch.setattr(settings, "PAYMENTS_LOCK_VIDEOS", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(settings, "EMAIL_ENABLED", False)
    monkeypatch.setattr(settings, "ADMIN_MOBILE_NUMBERS", "")
    deps.admin_mobile_numbers.cache_clear()
    payments.reset_payment_provider()

    async def setup():
        # The unique indexes are the real guard against double counting — create them like production does.
        await db["payment_orders"].create_index("razorpay_order_id", unique=True)
        await db["subscriptions"].create_index("user_id", unique=True)
        await db["payment_events"].create_index("event_id", unique=True)
        now = datetime.now(timezone.utc)
        for mobile, role in ((USER_MOBILE, "user"), ("9000000001", "user"), (ADMIN_MOBILE, "admin")):
            await db["users"].insert_one({"_id": ObjectId(), "mobile_number": mobile, "name": mobile, "status": "active",
                                          "is_verified": True, "role": role, "created_at": now})
    asyncio.run(setup())

    from app.core.security import SCOPE_ADMIN, create_access_token
    from app.main import app

    class H:
        provider = payments.get_payment_provider()
        client = TestClient(app)
        user = {"Authorization": "Bearer " + create_access_token(USER_MOBILE)}
        other = {"Authorization": "Bearer " + create_access_token("9000000001")}
        admin = {"Authorization": "Bearer " + create_access_token(ADMIN_MOBILE, scope=SCOPE_ADMIN)}
        settings_ = settings
        database = db

        def plan(self, name="Monthly", amount=19900, days=30, active=True):
            r = self.client.post("/api/v1/admin/plans", headers=self.admin,
                                 json={"name": name, "amount": amount, "duration_days": days, "active": active})
            assert r.status_code == 201, r.text
            return r.json()["data"]

        def order(self, plan_id, headers=None):
            r = self.client.post("/api/v1/payments/orders", json={"plan_id": plan_id}, headers=headers or self.user)
            assert r.status_code == 201, r.text
            return r.json()["data"]

        def verify(self, order_id, payment_id, signature, headers=None):
            return self.client.post("/api/v1/payments/verify", headers=headers or self.user, json={
                "razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": signature})

        def sub(self, headers=None):
            return self.client.get("/api/v1/users/me/subscription", headers=headers or self.user).json()["data"]

        def webhook(self, event, event_id, signature=None):
            raw, good = self.provider.sign_webhook(event)
            return self.client.post("/api/v1/payments/webhook", content=raw, headers={
                "X-Razorpay-Signature": good if signature is None else signature,
                "X-Razorpay-Event-Id": event_id, "Content-Type": "application/json"})

    return H()


def captured(order_id, payment_id, amount, name="payment.captured", status="captured"):
    return {"entity": "event", "event": name, "payload": {"payment": {"entity": {
        "id": payment_id, "order_id": order_id, "amount": amount, "currency": "INR", "status": status}}}}


# ── Signatures (provider) ────────────────────────────────────────────────────

def test_signature_checks():
    import hashlib
    import hmac

    from app.providers.payments.mock import MockPaymentProvider

    m = MockPaymentProvider()
    order = asyncio.run(m.create_order(19900, "INR", "r", {}))
    pid, sig = m.pay(order["id"])
    # Razorpay's documented algorithm, computed independently.
    assert sig == hmac.new(b"mock_key_secret", f"{order['id']}|{pid}".encode(), hashlib.sha256).hexdigest()
    assert m.verify_payment_signature(order["id"], pid, sig)
    assert not m.verify_payment_signature(order["id"], pid, "0" * 64)
    assert not m.verify_payment_signature("order_other", pid, sig)
    assert not m.verify_payment_signature(order["id"], pid, "")
    raw, wsig = m.sign_webhook({"event": "x"})
    assert m.verify_webhook_signature(raw, wsig)
    assert not m.verify_webhook_signature(raw + b" ", wsig)
    assert not m.verify_webhook_signature(raw, None)


def test_razorpay_provider_needs_keys(monkeypatch):
    from app.core.config import settings
    from app.providers.payments import PaymentProviderError
    from app.providers.payments.razorpay import RazorpayProvider

    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", None)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", None)
    with pytest.raises(PaymentProviderError):
        RazorpayProvider()


def test_webhooks_rejected_without_a_webhook_secret(monkeypatch):
    from app.core.config import settings
    from app.providers.payments.razorpay import RazorpayProvider

    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "s")
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", None)
    assert not RazorpayProvider().verify_webhook_signature(b"{}", "anything")


# ── App flow ─────────────────────────────────────────────────────────────────

def test_login_required_and_payments_switch(pay):
    assert pay.client.get("/api/v1/payments/plans").status_code == 401
    assert pay.client.get("/api/v1/users/me/subscription").status_code == 401
    pay.settings_.PAYMENTS_ENABLED = False
    r = pay.client.get("/api/v1/payments/plans", headers=pay.user)
    assert r.status_code == 404 and r.json()["error"]["code"] == "PAYMENTS_DISABLED"


def test_only_active_plans_are_listed_and_sold(pay):
    monthly = pay.plan()
    old = pay.plan(name="Old", active=False)
    assert [p["name"] for p in pay.client.get("/api/v1/payments/plans", headers=pay.user).json()["data"]] == ["Monthly"]
    r = pay.client.post("/api/v1/payments/orders", json={"plan_id": old["id"]}, headers=pay.user)
    assert r.status_code == 404 and r.json()["error"]["code"] == "PLAN_NOT_FOUND"
    r = pay.client.post("/api/v1/payments/orders", json={"plan_id": "nope"}, headers=pay.user)
    assert r.json()["error"]["code"] == "PLAN_NOT_FOUND"
    assert monthly["amount"] == 19900


def test_price_always_comes_from_the_plan(pay):
    plan = pay.plan()
    r = pay.client.post("/api/v1/payments/orders", json={"plan_id": plan["id"], "amount": 100}, headers=pay.user)
    data = r.json()["data"]
    assert data["amount"] == 19900 and pay.provider.orders[data["order_id"]]["amount"] == 19900
    assert data["key_id"] == "rzp_test_mock" and "secret" not in r.text.lower()


def test_successful_payment_activates_the_subscription(pay):
    plan = pay.plan()
    assert pay.sub()["is_active"] is False
    o = pay.order(plan["id"])
    pid, sig = pay.provider.pay(o["order_id"])
    r = pay.verify(o["order_id"], pid, sig)
    assert r.status_code == 200 and r.json()["data"]["is_active"] is True
    s = pay.sub()
    assert s["plan_name"] == "Monthly" and s["days_left"] == 30


def test_tampered_signature_is_rejected(pay):
    o = pay.order(pay.plan()["id"])
    pid, sig = pay.provider.pay(o["order_id"])
    r = pay.verify(o["order_id"], pid, sig[:-1] + ("0" if sig[-1] != "0" else "1"))
    assert r.status_code == 400 and r.json()["error"]["code"] == "PAYMENT_VERIFICATION_FAILED"
    assert pay.sub()["is_active"] is False


def test_someone_elses_order_looks_like_an_unknown_one(pay):
    o = pay.order(pay.plan()["id"])
    pid, sig = pay.provider.pay(o["order_id"])
    r = pay.verify(o["order_id"], pid, sig, headers=pay.other)
    assert r.status_code == 404 and r.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert pay.verify("order_unknown", pid, sig).json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert pay.sub(pay.other)["is_active"] is False


def test_wrong_amount_or_currency_at_razorpay_is_rejected(pay):
    plan = pay.plan()
    for field, value in (("amount", 100), ("currency", "USD"), ("order_id", "order_other")):
        o = pay.order(plan["id"])
        pid, sig = pay.provider.pay(o["order_id"])
        pay.provider.payments[pid][field] = value
        assert pay.verify(o["order_id"], pid, sig).json()["error"]["code"] == "PAYMENT_VERIFICATION_FAILED"
    assert pay.sub()["is_active"] is False


def test_authorized_but_not_captured_is_pending(pay):
    o = pay.order(pay.plan()["id"])
    pid, sig = pay.provider.pay(o["order_id"], status="authorized")
    r = pay.verify(o["order_id"], pid, sig)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PAYMENT_PENDING"


def test_razorpay_unreachable_during_verify_is_pending_not_failed(pay):
    o = pay.order(pay.plan()["id"])
    pid, sig = pay.provider.pay(o["order_id"])
    pay.provider.fail_next = "timeout"
    assert pay.verify(o["order_id"], pid, sig).json()["error"]["code"] == "PAYMENT_PENDING"
    assert pay.verify(o["order_id"], pid, sig).status_code == 200  # retry works


def test_razorpay_unreachable_when_creating_an_order(pay):
    plan = pay.plan()
    pay.provider.fail_next = "down"
    r = pay.client.post("/api/v1/payments/orders", json={"plan_id": plan["id"]}, headers=pay.user)
    assert r.status_code == 502 and r.json()["error"]["code"] == "PAYMENT_PROVIDER_ERROR"


def test_verify_twice_never_doubles_access(pay):
    o = pay.order(pay.plan()["id"])
    pid, sig = pay.provider.pay(o["order_id"])
    first = pay.verify(o["order_id"], pid, sig).json()["data"]
    second = pay.verify(o["order_id"], pid, sig).json()["data"]
    assert first["expires_at"] == second["expires_at"]
    # A different (even validly signed) payment on an already paid order is not accepted.
    other_pid = "pay_other"
    assert pay.verify(o["order_id"], other_pid, pay.provider.sign_payment(o["order_id"], other_pid)).status_code == 400


def test_second_purchase_extends_from_the_current_expiry(pay):
    plan = pay.plan()
    o1 = pay.order(plan["id"])
    first = pay.verify(o1["order_id"], *pay.provider.pay(o1["order_id"])).json()["data"]
    o2 = pay.order(plan["id"])
    second = pay.verify(o2["order_id"], *pay.provider.pay(o2["order_id"])).json()["data"]
    def parse(value):  # "...Z" isn't accepted by fromisoformat before Python 3.11
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    gap = parse(second["expires_at"]) - parse(first["expires_at"])
    assert abs(gap - timedelta(days=30)) < timedelta(seconds=5)
    assert second["days_left"] == 60


def test_simultaneous_confirmations_all_count_once(pay):
    from app.repositories import payment_repo
    from app.services import payment_service, subscription_service

    plan = pay.plan()
    orders = [pay.order(plan["id"]) for _ in range(3)]
    paid = [(o["order_id"], *pay.provider.pay(o["order_id"])) for o in orders]

    async def race():
        docs = [await payment_repo.find_by_razorpay_order_id(oid) for oid, _, _ in paid]
        # Each order confirmed twice at once (app + webhook): exactly one wins per order.
        results = await asyncio.gather(*(
            payment_service.complete_paid_order(doc, pid, via)
            for doc, (_, pid, _) in zip(docs, paid) for via in ("verify", "webhook")
        ))
        return results, await subscription_service.get_for_user(docs[0]["user_id"])

    results, sub = asyncio.run(race())
    assert sum(results) == 3 and sub.days_left == 90


def test_order_creation_is_rate_limited(pay, monkeypatch):
    from app.repositories import rate_limit_repo
    from mongomock_motor import AsyncMongoMockClient

    monkeypatch.setattr(rate_limit_repo, "rate_limits_collection", AsyncMongoMockClient()["rl"]["rate_limits"], raising=False)
    pay.settings_.RATE_LIMIT_ENABLED = True
    plan = pay.plan()
    codes = [pay.client.post("/api/v1/payments/orders", json={"plan_id": plan["id"]}, headers=pay.user).status_code
             for _ in range(21)]
    assert codes[:20] == [201] * 20 and codes[20] == 429


# ── Webhook ──────────────────────────────────────────────────────────────────

def test_webhook_must_be_signed(pay):
    o = pay.order(pay.plan()["id"])
    pid, _ = pay.provider.pay(o["order_id"])
    assert pay.webhook(captured(o["order_id"], pid, 19900), "e1", signature="deadbeef").status_code == 400
    assert pay.client.post("/api/v1/payments/webhook", content=b"{}").status_code == 400
    assert pay.client.post("/api/v1/payments/webhook", content=b"x" * 300_000).status_code == 413
    assert pay.sub()["is_active"] is False


def test_webhook_grants_access_once(pay):
    o = pay.order(pay.plan()["id"])
    pid, sig = pay.provider.pay(o["order_id"])
    pay.settings_.PAYMENTS_ENABLED = False  # still processed: the money has moved
    assert pay.webhook(captured(o["order_id"], pid, 19900), "e1").json()["data"]["result"] == "granted"
    pay.settings_.PAYMENTS_ENABLED = True
    assert pay.sub()["days_left"] == 30
    assert pay.webhook(captured(o["order_id"], pid, 19900), "e1").json()["data"]["result"] == "duplicate"
    assert pay.webhook(captured(o["order_id"], pid, 19900, name="order.paid"), "e2").json()["data"]["result"] == "already_done"
    assert pay.verify(o["order_id"], pid, sig).json()["data"]["days_left"] == 30


def test_webhook_ignores_mismatches_and_unknown_orders(pay):
    o = pay.order(pay.plan()["id"])
    pid, _ = pay.provider.pay(o["order_id"])
    assert pay.webhook(captured(o["order_id"], pid, 100), "e1").json()["data"]["result"] == "ignored_mismatch"
    assert pay.webhook(captured(o["order_id"], pid, 19900, status="authorized"), "e2").json()["data"]["result"] == "ignored_mismatch"
    assert pay.webhook(captured("order_x", "pay_x", 19900), "e3").json()["data"]["result"] == "ignored_unknown_order"
    assert pay.webhook({"event": "something.else", "payload": {}}, "e4").json()["data"]["result"] == "ignored_event"
    assert pay.sub()["is_active"] is False


def test_failed_payment_is_recorded(pay):
    o = pay.order(pay.plan()["id"])
    ev = captured(o["order_id"], "pay_f", 19900, name="payment.failed", status="failed")
    ev["payload"]["payment"]["entity"]["error_description"] = "Card declined"
    assert pay.webhook(ev, "e1").json()["data"]["result"] == "failed_recorded"
    item = pay.client.get("/api/v1/admin/payments?status=failed", headers=pay.admin).json()["data"]["items"][0]
    assert item["failure_reason"] == "Card declined"


def test_refund_takes_back_that_purchase(pay):
    plan = pay.plan()
    o1, o2 = pay.order(plan["id"]), pay.order(plan["id"])
    p1, s1 = pay.provider.pay(o1["order_id"])
    p2, s2 = pay.provider.pay(o2["order_id"])
    pay.verify(o1["order_id"], p1, s1)
    pay.verify(o2["order_id"], p2, s2)
    assert pay.sub()["days_left"] == 60
    refund = {"event": "refund.processed", "payload": {"refund": {"entity": {"id": "rfnd_1", "payment_id": p2, "amount": 19900}}}}
    assert pay.webhook(refund, "r1").json()["data"]["result"] == "refunded"
    assert pay.webhook(refund, "r2").json()["data"]["result"] == "already_done"
    assert pay.sub()["days_left"] == 30
    refund["payload"]["refund"]["entity"].update(id="rfnd_2", payment_id=p1)
    pay.webhook(refund, "r3")
    assert pay.sub()["is_active"] is False


# ── Admin ────────────────────────────────────────────────────────────────────

def test_admin_endpoints_need_an_admin_session(pay):
    assert pay.client.get("/api/v1/admin/plans").status_code == 401
    assert pay.client.get("/api/v1/admin/plans", headers=pay.user).status_code == 403
    assert pay.client.get("/api/v1/admin/payments", headers=pay.user).status_code == 403


def test_admin_plans(pay):
    plan = pay.plan()
    bad = pay.client.post("/api/v1/admin/plans", headers=pay.admin, json={"name": "x", "amount": 50, "duration_days": 1})
    assert bad.status_code == 422  # below Razorpay's ₹1 minimum
    r = pay.client.patch(f"/api/v1/admin/plans/{plan['id']}", headers=pay.admin, json={"amount": 29900, "active": False})
    assert r.json()["data"]["amount"] == 29900 and r.json()["data"]["active"] is False
    assert pay.client.get("/api/v1/payments/plans", headers=pay.user).json()["data"] == []
    assert len(pay.client.get("/api/v1/admin/plans", headers=pay.admin).json()["data"]) == 1


def test_editing_a_plan_does_not_change_old_orders(pay):
    plan = pay.plan()
    o = pay.order(plan["id"])
    pay.client.patch(f"/api/v1/admin/plans/{plan['id']}", headers=pay.admin, json={"amount": 99900, "duration_days": 365})
    pid, sig = pay.provider.pay(o["order_id"])
    assert pay.verify(o["order_id"], pid, sig).json()["data"]["days_left"] == 30


def test_admin_payments_list_and_grant_revoke(pay):
    o = pay.order(pay.plan()["id"])
    pay.verify(o["order_id"], *pay.provider.pay(o["order_id"]))
    page = pay.client.get("/api/v1/admin/payments?status=paid", headers=pay.admin).json()["data"]
    assert page["total"] == 1 and page["items"][0]["paid_via"] == "verify"
    uid = page["items"][0]["user_id"]
    r = pay.client.post(f"/api/v1/admin/users/{uid}/subscription/grant", headers=pay.admin, json={"days": 10, "note": "dealer"})
    data = r.json()["data"]
    assert data["subscription"]["days_left"] == 40
    assert data["history"][0]["source"] == "admin" and data["history"][0]["note"] == "dealer" and data["history"][0]["by"]
    r = pay.client.post(f"/api/v1/admin/users/{uid}/subscription/revoke", headers=pay.admin, json={})
    assert r.json()["data"]["subscription"]["is_active"] is False and r.json()["data"]["ended_reason"] == "admin_revoke"
    assert pay.client.get("/api/v1/admin/users/nope/subscription", headers=pay.admin).status_code == 400
    assert pay.client.get(f"/api/v1/admin/users/{ObjectId()}/subscription", headers=pay.admin).status_code == 404


# ── Account deletion and the video lock ──────────────────────────────────────

def test_deleting_the_account_ends_access_but_keeps_payments(pay):
    from app.repositories import payment_repo, subscription_repo

    o = pay.order(pay.plan()["id"])
    pay.verify(o["order_id"], *pay.provider.pay(o["order_id"]))
    step1 = pay.client.post("/api/v1/auth/delete-account", json={"mobile_number": USER_MOBILE})
    otp = step1.json()["data"].get("dev_otp")
    assert otp, step1.text
    step2 = pay.client.post("/api/v1/auth/delete-account", json={"mobile_number": USER_MOBILE, "otp": otp})
    assert step2.json()["data"]["deleted"] is True

    async def check():
        user = await pay.database["users"].find_one({"status": "deleted"})
        return await subscription_repo.find_by_user(user["_id"]), await payment_repo.find_by_razorpay_order_id(o["order_id"])

    sub, order = asyncio.run(check())
    assert sub["ended_reason"] == "account_deleted" and order["status"] == "paid"


def test_video_lock(pay):
    plan = pay.plan()
    assert pay.client.get("/api/v1/videos", headers=pay.user).status_code == 200  # lock off: open
    pay.settings_.PAYMENTS_LOCK_VIDEOS = True
    r = pay.client.get("/api/v1/videos", headers=pay.user)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SUBSCRIPTION_REQUIRED"
    assert pay.client.get(f"/api/v1/videos/{ObjectId()}", headers=pay.user).json()["error"]["code"] == "SUBSCRIPTION_REQUIRED"
    assert pay.client.get(f"/api/v1/videos/{ObjectId()}/document", headers=pay.user).json()["error"]["code"] == "SUBSCRIPTION_REQUIRED"
    assert pay.client.get("/api/v1/videos/categories", headers=pay.user).status_code == 200
    o = pay.order(plan["id"])
    pay.verify(o["order_id"], *pay.provider.pay(o["order_id"]))
    assert pay.client.get("/api/v1/videos", headers=pay.user).status_code == 200


# ── Reconcile safety net ─────────────────────────────────────────────────────

def _age(pay, order_id, minutes):
    """Pretend an order was created `minutes` ago."""
    asyncio.run(pay.database["payment_orders"].update_one(
        {"razorpay_order_id": order_id},
        {"$set": {"created_at": datetime.now(timezone.utc) - timedelta(minutes=minutes)}},
    ))


def test_reconcile_completes_a_paid_but_unconfirmed_order(pay):
    from app.services import payment_reconcile_service as rec

    plan = pay.plan()
    orphan = pay.order(plan["id"])       # paid, but no verify and no webhook
    pay.provider.pay(orphan["order_id"])
    unpaid = pay.order(plan["id"])        # checkout opened and abandoned
    young = pay.order(plan["id"])         # paid seconds ago: normal flow still in progress
    pay.provider.pay(young["order_id"])
    _age(pay, orphan["order_id"], 30)
    _age(pay, unpaid["order_id"], 30)
    _age(pay, young["order_id"], 1)

    counts = asyncio.run(rec.run_once())
    assert counts == {"granted": 1, "not_paid": 1}
    sub = pay.sub()
    assert sub["is_active"] and sub["days_left"] == 30
    item = next(i for i in pay.client.get("/api/v1/admin/payments?status=paid", headers=pay.admin).json()["data"]["items"])
    assert item["paid_via"] == "reconcile" and item["razorpay_order_id"] == orphan["order_id"]
    # A late webhook / verify for the same payment changes nothing.
    pid = item["razorpay_payment_id"]
    assert pay.webhook(captured(orphan["order_id"], pid, 19900), "late").json()["data"]["result"] == "already_done"
    assert asyncio.run(rec.run_once()).get("granted") is None
    assert pay.sub()["days_left"] == 30


def test_reconcile_skips_old_orders_and_reports_pending_and_mismatch(pay):
    from app.services import payment_reconcile_service as rec

    plan = pay.plan()
    old = pay.order(plan["id"]); pay.provider.pay(old["order_id"]); _age(pay, old["order_id"], 60 * 72)
    pending = pay.order(plan["id"]); pay.provider.pay(pending["order_id"], status="authorized"); _age(pay, pending["order_id"], 30)
    wrong = pay.order(plan["id"]); pid, _ = pay.provider.pay(wrong["order_id"]); _age(pay, wrong["order_id"], 30)
    pay.provider.payments[pid]["amount"] = 100
    assert asyncio.run(rec.run_once()) == {"pending_capture": 1, "mismatch": 1}
    assert pay.sub()["is_active"] is False


def test_reconcile_stops_quietly_when_razorpay_is_down(pay):
    from app.services import payment_reconcile_service as rec

    o = pay.order(pay.plan()["id"]); pay.provider.pay(o["order_id"]); _age(pay, o["order_id"], 30)
    pay.provider.fail_next = "down"
    assert asyncio.run(rec.run_once()) == {"error": 1}
    assert asyncio.run(rec.run_once()) == {"granted": 1}  # next run picks it up


def test_admin_check_with_razorpay(pay):
    o = pay.order(pay.plan()["id"])
    url = f"/api/v1/admin/payments/{o['order_id']}/check"
    assert pay.client.post(url, headers=pay.user).status_code == 403
    assert pay.client.post(url, headers=pay.admin).json()["data"]["result"] == "not_paid"
    pay.provider.pay(o["order_id"])
    r = pay.client.post(url, headers=pay.admin).json()["data"]
    assert r["result"] == "granted" and r["order"]["status"] == "paid" and r["order"]["paid_via"] == "admin_check"
    assert pay.client.post(url, headers=pay.admin).json()["data"]["result"] == "already_done"
    assert pay.client.post("/api/v1/admin/payments/order_nope/check", headers=pay.admin).status_code == 404
    assert pay.sub()["days_left"] == 30


def test_reconcile_loop_only_runs_with_real_razorpay_keys(monkeypatch):
    from app.core.config import settings
    from app.services import payment_reconcile_service as rec

    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "mock")
    assert rec.active() is False
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "razorpay")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "s")
    monkeypatch.setattr(settings, "PAYMENTS_RECONCILE_ENABLED", True)
    assert rec.active() is True
    monkeypatch.setattr(settings, "PAYMENTS_RECONCILE_ENABLED", False)
    assert rec.active() is False
