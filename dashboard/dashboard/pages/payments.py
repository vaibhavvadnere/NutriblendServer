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
from dashboard.auth import session
from dashboard.components.layout import api_errors, page_header
from dashboard.utils.formatting import fmt_datetime, fmt_rupees, mask_mobile

_STATUS_OPTIONS = {"All": None, "Paid": "paid", "Started (not paid)": "created", "Failed": "failed", "Refunded": "refunded"}
_STATUS_LABEL = {"paid": "Paid", "created": "Started", "failed": "Failed", "refunded": "Refunded"}
_PAGE = "payments.page"
_FILTER_SIG = "payments.filters"
_PAGE_SIZE = 20


def render() -> None:
    page_header("Payments", "Razorpay orders. Refunds are done in the Razorpay dashboard and appear here on their own.")

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
            "Confirmed by": {"verify": "App", "webhook": "Razorpay"}.get(o.paid_via or "", "—"),
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
