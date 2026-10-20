"""
pages/payments.py — Every checkout attempt (Razorpay order), newest first.

Data: GET /admin/payments. "Started" = checkout opened but not paid (the user
may have cancelled). Refunds are made in the Razorpay dashboard; they show up
here automatically through the webhook.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import navigation
from dashboard.api import payments as payments_api
from dashboard.api.errors import ApiError
from dashboard.auth import session
from dashboard.components.layout import api_errors, page_header
from dashboard.utils.formatting import fmt_datetime, fmt_rupees, mask_mobile

_STATUS_OPTIONS = {"All": None, "Paid": "paid", "Started (not paid)": "created", "Failed": "failed", "Refunded": "refunded"}
_STATUS_LABEL = {"paid": "Paid", "created": "Started", "failed": "Failed", "refunded": "Refunded"}
_PAGE = "payments.page"
_FILTER_SIG = "payments.filters"
_PAGE_SIZE = 20
_CONFIRMED_BY = {"verify": "App", "webhook": "Razorpay", "reconcile": "Auto-check", "admin_check": "Admin check"}
_CHECK_RESULT = {
    "granted": ("success", "It was paid — access has been given now."),
    "already_done": ("info", "Already confirmed earlier. Nothing to do."),
    "not_paid": ("info", "Razorpay has no successful payment for this order (cancelled or not paid)."),
    "pending_capture": ("warning", "The bank approved it but Razorpay hasn't captured it yet. Check again later."),
    "mismatch": ("error", "A payment exists but the amount doesn't match this order. Look at it in the Razorpay dashboard."),
}
_FLASH = "payments.flash"


def _check_box(orders) -> None:
    """For "I paid but got nothing": ask Razorpay about one unconfirmed order now."""
    unconfirmed = [o for o in orders if o.status in ("created", "failed")]
    with st.expander("Paid but no access? Check an order with Razorpay", icon=":material/sync:"):
        st.caption("The server also does this automatically every few minutes for recent orders.")
        if not unconfirmed:
            st.caption("No unconfirmed orders on this page.")
            return
        order = st.selectbox(
            "Order", unconfirmed, key="payments.check.pick",
            format_func=lambda o: f"{fmt_datetime(o.created_at)} · {mask_mobile(o.mobile_number or '')} · {o.plan_name} · {fmt_rupees(o.amount)}",
        )
        if st.button("Check with Razorpay", icon=":material/sync:"):
            try:
                data = payments_api.check_with_razorpay(session.get_client(), order.razorpay_order_id)
            except ApiError as exc:
                st.session_state[_FLASH] = ("error", exc.message)
            else:
                st.session_state[_FLASH] = _CHECK_RESULT.get(data.get("result"), ("info", str(data.get("result"))))
            st.rerun()


def render() -> None:
    page_header("Payments", "Razorpay orders. Refunds are done in the Razorpay dashboard and appear here on their own.")
    flash = st.session_state.pop(_FLASH, None)
    if flash:
        kind, text = flash
        {"success": st.success, "warning": st.warning, "error": st.error}.get(kind, st.info)(text)

    label = st.selectbox("Status", list(_STATUS_OPTIONS), key="payments.status")
    status = _STATUS_OPTIONS[label]
    if st.session_state.get(_FILTER_SIG) != status:
        st.session_state[_FILTER_SIG] = status
        st.session_state[_PAGE] = 1
    page = st.session_state.get(_PAGE, 1)

    with api_errors("load payments"):
        result = payments_api.list_payments(session.get_client(), status, None, page, _PAGE_SIZE)

    if result.total == 0:
        st.info("No payments match this filter." if status else "No payments yet.", icon=":material/payments:")
        return

    df = pd.DataFrame([
        {
            "user_id": o.user_id,
            "Date": fmt_datetime(o.paid_at or o.created_at),
            "Mobile": mask_mobile(o.mobile_number or ""),
            "Plan": o.plan_name,
            "Amount": fmt_rupees(o.amount),
            "Status": _STATUS_LABEL.get(o.status, o.status),
            "Confirmed by": _CONFIRMED_BY.get(o.paid_via or "", "—"),
            "Razorpay payment": o.razorpay_payment_id or "—",
            "Note": o.failure_reason or "",
        }
        for o in result.items
    ])
    event = st.dataframe(
        df, hide_index=True, width="stretch",
        column_order=["Date", "Mobile", "Plan", "Amount", "Status", "Confirmed by", "Razorpay payment", "Note"],
        on_select="rerun", selection_mode="single-row", key=f"payments.table.{page}.{status}",
    )
    st.caption("Select a row to open the buyer's details.")
    rows = event.selection.rows if event else []
    if rows:
        st.switch_page(navigation.pages().user_detail, query_params={"id": df.iloc[rows[0]]["user_id"]})

    _check_box(result.items)

    first = (result.page - 1) * result.page_size + 1
    last = first + len(result.items) - 1
    info, prev_col, next_col = st.columns([4, 1, 1], vertical_alignment="center")
    info.caption(f"Showing {first:,}–{last:,} of {result.total:,} · page {result.page} of {result.pages}")
    if prev_col.button("Previous", icon=":material/chevron_left:", disabled=result.page <= 1, width="stretch"):
        st.session_state[_PAGE] = result.page - 1
        st.rerun()
    if next_col.button("Next", icon=":material/chevron_right:", disabled=result.page >= result.pages, width="stretch"):
        st.session_state[_PAGE] = result.page + 1
        st.rerun()
