"""
pages/plans.py — Subscription plans: list, add, edit, switch on/off.

Data: GET /admin/plans; actions: POST /admin/plans, PATCH /admin/plans/{id}.
Prices are typed in rupees here and sent in paise. Plans are never deleted
(old orders refer to them) — switch one off to stop selling it.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard.api import payments as payments_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components.layout import api_errors, page_header
from dashboard.models import Plan
from dashboard.utils.formatting import fmt_datetime, fmt_rupees

_FLASH = "plans.flash"


def _flash() -> None:
    flash = st.session_state.pop(_FLASH, None)
    if flash:
        kind, text = flash
        (st.success if kind == "success" else st.error)(text)


def _save(action, success: str, **fields) -> None:
    try:
        action(session.get_client(), **fields)
    except ApiError as exc:
        st.session_state[_FLASH] = ("error", exc.message)
    else:
        st.session_state[_FLASH] = ("success", success)
    st.rerun()


def _plan_fields(prefix: str, plan: Plan | None = None) -> dict | None:
    """The shared form body. Returns the fields to send, or None if invalid."""
    name = st.text_input("Name", value=plan.name if plan else "", max_chars=60, key=f"{prefix}.name",
                         placeholder="e.g. Yearly")
    c1, c2, c3 = st.columns(3)
    price = c1.number_input("Price (₹)", min_value=1.0, max_value=100000.0, step=1.0, format="%.2f",
                            value=(plan.amount / 100) if plan else 199.0, key=f"{prefix}.price")
    days = c2.number_input("Duration (days)", min_value=1, max_value=3660, step=1,
                           value=plan.duration_days if plan else 30, key=f"{prefix}.days")
    order = c3.number_input("Display order", min_value=0, max_value=1000, step=1,
                            value=plan.sort_order if plan else 0, key=f"{prefix}.order",
                            help="Lower numbers show first in the app.")
    description = st.text_area("Description (shown in the app)", value=(plan.description or "") if plan else "",
                               max_chars=500, key=f"{prefix}.desc", height=80)
    active = st.toggle("On sale", value=plan.active if plan else True, key=f"{prefix}.active",
                       help="Off hides the plan in the app. Existing subscriptions are not affected.")
    return {
        "name": name.strip(),
        "amount": int(round(price * 100)),
        "duration_days": int(days),
        "sort_order": int(order),
        "description": description.strip() or None,
        "active": active,
    }


def render() -> None:
    page_header("Plans", "What users can buy in the app. Prices in rupees; changes apply to new purchases only.")
    _flash()

    with api_errors("load plans"):
        plans = payments_api.list_plans(session.get_client())

    if plans:
        st.dataframe(
            pd.DataFrame([
                {
                    "Name": p.name,
                    "Price": fmt_rupees(p.amount),
                    "Duration": f"{p.duration_days} days",
                    "Status": "On sale" if p.active else "Off",
                    "Order": p.sort_order,
                    "Updated": fmt_datetime(p.updated_at or p.created_at),
                }
                for p in plans
            ]),
            hide_index=True, width="stretch",
        )
    else:
        st.info("No plans yet. Add the first one below.", icon=":material/sell:")

    st.subheader("Add a plan")
    with st.form("plans.new", clear_on_submit=True, border=True):
        fields = _plan_fields("plans.new")
        if st.form_submit_button("Add plan", type="primary", icon=":material/add:"):
            if not fields["name"]:
                st.error("Enter a name.")
            else:
                _save(payments_api.create_plan, f"Plan “{fields['name']}” added.", **fields)

    if plans:
        st.subheader("Edit a plan")
        chosen = st.selectbox("Plan", plans, format_func=lambda p: f"{p.name} · {fmt_rupees(p.amount)} · {p.duration_days} days",
                              key="plans.edit.pick")
        with st.form(f"plans.edit.{chosen.id}", border=True):
            fields = _plan_fields(f"plans.edit.{chosen.id}", chosen)
            st.caption("Changing the price or duration only affects purchases made after saving.")
            if st.form_submit_button("Save changes", type="primary", icon=":material/save:"):
                if not fields["name"]:
                    st.error("Enter a name.")
                else:
                    _save(lambda c, **f: payments_api.update_plan(c, chosen.id, **f), f"Plan “{fields['name']}” saved.", **fields)
