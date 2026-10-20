"""Plans, Payments and the User details subscription section (AppTest + fake server)."""
import json
import httpx
import pytest
from dashboard.auth import session
from tests.test_pages import run_page

PLANS, CALLS, SUB = [], [], {}
USER = {"id": "u7", "mobile_number": "9123400007", "name": "Farmer Ram", "status": "active", "is_verified": True,
        "created_at": "2026-09-25T10:00:00Z", "role": "user", "active_sessions": 1, "is_admin": False}
ORDER = {"id": "o1", "user_id": "u7", "mobile_number": "9123400007", "plan_id": "p1", "plan_name": "Monthly",
         "duration_days": 30, "amount": 19900, "currency": "INR", "razorpay_order_id": "order_1",
         "razorpay_payment_id": "pay_1", "status": "paid", "paid_via": "webhook", "created_at": "2026-10-20T10:00:00Z",
         "paid_at": "2026-10-20T10:01:00Z"}


STARTED = {**ORDER, "id": "o2", "razorpay_order_id": "order_2", "razorpay_payment_id": None, "status": "created",
           "paid_via": None, "paid_at": None}


def ok(data, code=200):
    return httpx.Response(code, json={"success": True, "message": "ok", "data": data})


def server(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    body = json.loads(req.content) if req.content else None
    CALLS.append((req.method, path, body))
    if path == "/api/v1/admin/plans" and req.method == "GET":
        return ok(PLANS)
    if path == "/api/v1/admin/plans" and req.method == "POST":
        PLANS.append({**body, "id": f"p{len(PLANS) + 1}", "currency": "INR", "created_at": "2026-10-20T10:00:00Z"})
        return ok(PLANS[-1], 201)
    if path.startswith("/api/v1/admin/plans/") and req.method == "PATCH":
        plan = next(p for p in PLANS if p["id"] == path.rsplit("/", 1)[1]); plan.update(body)
        return ok(plan)
    if path == "/api/v1/admin/payments":
        return ok({"items": [ORDER, STARTED], "total": 2, "page": 1, "page_size": 20, "pages": 1})
    if path == "/api/v1/admin/payments/order_2/check":
        return ok({"result": "granted", "order": {**STARTED, "status": "paid", "paid_via": "admin_check"}})
    if path == "/api/v1/admin/users/u7/subscription":
        return ok(SUB)
    if path.startswith("/api/v1/admin/users/u7/subscription/"):
        if path.endswith("grant"):
            SUB["subscription"].update(is_active=True, days_left=SUB["subscription"]["days_left"] + body["days"])
        else:
            SUB["subscription"].update(is_active=False, days_left=0); SUB["ended_reason"] = "admin_revoke"
        return ok(SUB)
    if path == "/api/v1/admin/users/u7":
        return ok(USER)
    return httpx.Response(404, json={"success": False, "message": "no", "error": {"code": "NOT_FOUND"}})


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    PLANS.clear(); CALLS.clear()
    SUB.clear(); SUB.update({"subscription": {"is_active": True, "plan_name": "Monthly", "days_left": 30,
                             "started_at": "2026-10-20T10:01:00Z", "expires_at": "2026-11-19T10:01:00Z"},
                             "source": "payment", "history": [{"at": "2026-10-20T10:01:00Z", "action": "extend", "days": 30,
                                                               "source": "payment", "order_id": "order_1"}]})
    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(server))
    monkeypatch.setattr(session, "_shared_http", lambda: client)


def test_add_and_edit_plan():
    at = run_page("plans")
    assert not at.exception, at.exception
    assert any("No plans yet" in i.value for i in at.info)
    at.text_input(key="plans.new.name").input("Monthly")
    at.number_input(key="plans.new.price").set_value(199.0)
    at.number_input(key="plans.new.days").set_value(30)
    next(b for b in at.button if b.label == "Add plan").click().run()
    assert ("POST", "/api/v1/admin/plans", {"name": "Monthly", "amount": 19900, "duration_days": 30, "sort_order": 0,
                                             "description": None, "active": True}) in CALLS
    assert any("added" in s.value for s in at.success)
    assert at.dataframe[0].value.iloc[0]["Price"] == "₹199"
    at.toggle(key="plans.edit.p1.active").set_value(False)
    next(b for b in at.button if b.label == "Save changes").click().run()
    assert PLANS[0]["active"] is False and at.dataframe[0].value.iloc[0]["Status"] == "Off"


def test_plan_needs_a_name():
    at = run_page("plans")
    next(b for b in at.button if b.label == "Add plan").click().run()
    assert any("Enter a name" in e.value for e in at.error) and not any(c[0] == "POST" for c in CALLS)


def test_payments_list():
    at = run_page("payments")
    assert not at.exception, at.exception
    row = at.dataframe[0].value.iloc[0]
    assert row["Amount"] == "₹199" and row["Status"] == "Paid" and row["Confirmed by"] == "Razorpay"
    assert row["Mobile"] == "91XXXXXX07"


def test_user_subscription_grant_and_revoke():
    at = run_page("user_detail", {"id": "u7"})
    assert not at.exception, at.exception
    assert any(s.value == "Subscription" for s in at.subheader)
    assert any("30 days left" in c.value for c in at.caption)
    next(b for b in at.button if b.label == "Give access").click().run()
    assert any(c[0] == "POST" and c[1].endswith("/grant") and c[2]["days"] == 30 for c in CALLS)
    assert any("Added 30 days" in s.value for s in at.success)
    # Ending access asks first (the confirm dialog), and nothing is sent until confirmed.
    next(b for b in at.button if b.label == "End access now").click().run()
    assert any(b.label == "End access" for b in at.button)
    assert not any(c[1].endswith("/revoke") for c in CALLS)


def test_check_an_order_with_razorpay():
    at = run_page("payments")
    assert not at.exception, at.exception
    assert len(at.dataframe[0].value) == 2
    next(b for b in at.button if b.label == "Check with Razorpay").click().run()
    assert ("POST", "/api/v1/admin/payments/order_2/check", None) in CALLS
    assert any("access has been given" in s.value for s in at.success)
