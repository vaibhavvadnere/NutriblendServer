"""
pages/privacy.py — The app's privacy policy, as published by the server.

The text lives on the server (app/legal/privacy_policy.md + LEGAL_* in .env) and
is public at /privacy-policy — that address goes into Play Console and the app.
This page shows the same text plus a Google Play readiness checklist.
"""

from __future__ import annotations

import streamlit as st

from dashboard.api import legal as legal_api
from dashboard.auth import session
from dashboard.components.layout import api_errors, page_header


def render() -> None:
    page_header("Privacy policy", "Published by the server for the app and Google Play. Edit the details in the server's .env.")

    with api_errors("load the privacy policy"):
        policy = legal_api.privacy_policy(session.get_client())

    # ── Public links ──────────────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown("**Public links** — no login needed")
        left, right = st.columns(2)
        with left:
            st.caption("Privacy policy (Play Console → App content → Privacy policy, and in the app)")
            st.code(policy.policy_url, language=None)
            st.link_button("Open privacy policy", policy.policy_url, icon=":material/open_in_new:")
        with right:
            st.caption("Account deletion (Play Console → Data safety → Data deletion)")
            st.code(policy.deletion_url, language=None)
            st.link_button("Open deletion page", policy.deletion_url, icon=":material/open_in_new:")
        meta = [f"Version {policy.version}"]
        meta.append(f"Effective {policy.effective_date}" if policy.effective_date else "Effective date not set")
        st.caption(" · ".join(meta))

    # ── Google Play checklist ─────────────────────────────────────────────────
    done = sum(i.ok for i in policy.play_store_checklist)
    total = len(policy.play_store_checklist)
    st.subheader(f"Google Play readiness — {done} of {total}")
    if done < total:
        st.caption("Items marked ✗ must be fixed before submitting the app.")
    for item in policy.play_store_checklist:
        icon = ":material/check_circle:" if item.ok else ":material/cancel:"
        color = "green" if item.ok else "red"
        with st.container(border=True):
            st.markdown(f":{color}[{icon}] **{item.item}**  \n{item.hint}")

    if policy.missing_fields:
        with st.expander(f"Details still to fill in ({len(policy.missing_fields)})", expanded=True):
            st.caption("Add these to the server's .env, then restart the server. They appear as “to be filled” in the policy until then.")
            st.code(
                "\n".join(
                    [
                        "LEGAL_COMPANY_NAME=          # must match the developer name on Play",
                        "LEGAL_COMPANY_ADDRESS=",
                        "LEGAL_CONTACT_EMAIL=",
                        "LEGAL_GRIEVANCE_OFFICER=",
                        "LEGAL_GRIEVANCE_EMAIL=       # optional; defaults to LEGAL_CONTACT_EMAIL",
                        "LEGAL_HOSTING_PROVIDER=      # e.g. Render, AWS, DigitalOcean",
                        "PRIVACY_POLICY_EFFECTIVE_DATE=   # e.g. 1 October 2026",
                    ]
                ),
                language="bash",
            )
            st.markdown("Missing now: " + ", ".join(f"**{m}**" for m in policy.missing_fields))

    # ── The policy itself ─────────────────────────────────────────────────────
    st.divider()
    with st.container(border=True):
        st.markdown(policy.markdown)
