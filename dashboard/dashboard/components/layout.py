"""
components/layout.py — Page chrome and the standard way to show API errors.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

import streamlit as st

from dashboard.api import auth as auth_api
from dashboard.api.errors import (
    ApiError,
    Forbidden,
    NetworkError,
    RateLimited,
    SessionExpired,
    ValidationFailed,
)
from dashboard.auth import session
from dashboard.utils.formatting import mask_mobile

logger = logging.getLogger("dashboard.ui")


def page_header(title: str, subtitle: Optional[str] = None) -> None:
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def sidebar_account() -> None:
    """Signed-in user + logout button, at the bottom of the sidebar."""
    user = session.current_user()
    if user is None:
        return
    with st.sidebar:
        st.markdown(f"**{user.name}**  \n{mask_mobile(user.mobile_number)}")
        if st.button("Log out", icon=":material/logout:", width="stretch"):
            auth_api.logout(session.get_client())
            session.clear()
            st.rerun()


@contextmanager
def api_errors(action: str = "load this page") -> Iterator[None]:
    """
    Wrap API calls in a page:

        with api_errors("load users"):
            users = admin_api.list_users(client)

    Shows a friendly message for each error type. An expired session sends
    the user back to sign in.
    """
    try:
        yield
    except SessionExpired:
        session.clear()
        st.session_state["flash"] = "Your session has expired. Please sign in again."
        st.rerun()
    except Forbidden as exc:
        st.error(f"Not authorised: {exc.message}", icon=":material/block:")
        st.stop()
    except NetworkError as exc:
        st.error(exc.message, icon=":material/cloud_off:")
        st.stop()
    except RateLimited as exc:
        wait = f" Try again in {exc.retry_after}s." if exc.retry_after else ""
        st.warning(f"{exc.message}{wait}", icon=":material/hourglass_top:")
        st.stop()
    except ValidationFailed as exc:
        st.error(exc.message, icon=":material/error:")
        st.stop()
    except ApiError as exc:
        logger.warning("Failed to %s: %r", action, exc)
        st.error(f"Could not {action}: {exc.message}", icon=":material/error:")
        st.stop()
