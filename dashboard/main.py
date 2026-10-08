"""
main.py — Nutriblend admin dashboard entry point.

    streamlit run main.py

Decides which pages exist for this browser session:
  * signed out -> only the login page (every other URL falls back to it)
  * signed in  -> the admin pages, with the sidebar navigation
"""

import streamlit as st

from dashboard.auth import session
from dashboard.components.layout import sidebar_account
from dashboard.config import settings
from dashboard import navigation
from dashboard.pages import login
from dashboard.utils.logging import setup_logging

setup_logging()
st.set_page_config(page_title=settings.APP_TITLE, page_icon=":material/eco:", layout="wide")

# Stay signed in across a page refresh: a hidden component keeps a random key in sessionStorage (erased when the
# tab is closed); the tokens stay in this process (dashboard/auth/remember.py). It is always the first element,
# signed in or not, so nothing above it ever shifts.
from dashboard.auth import remember
from dashboard.components.session_keeper import keeper

if session.is_logged_in():
    remember.sync()
_reported = keeper(store_key=remember.current_key(), clear=remember.take_clear()) if remember.enabled() else None
st.markdown(
    "<style>[data-testid='stElementContainer']:has(iframe[title*='session_keeper']){position:absolute;"
    "width:0;height:0;overflow:hidden;margin:0;padding:0}</style>",
    unsafe_allow_html=True,
)
_pending = False
if remember.enabled() and not session.is_logged_in() and not remember.tried() and not st.session_state.get("_pend_gave_up"):
    if _reported is None:
        # The browser has not answered yet (milliseconds). Register every page (so the address in the browser
        # is not rewritten to the sign-in page) and show only a small spinner: the sign-in form is not drawn at
        # all, so a refresh never flashes it before the session is restored.
        _pending = True
    else:
        if remember.restore(remember.reported_key(_reported)):
            st.rerun()
        if remember.reported_key(_reported):
            remember.ask_browser_to_clear()   # a key that no longer works: erase it

if _pending:
    st.navigation(navigation.pages().menu(), position="hidden")
    st.markdown(
        "<style>.nb-wait{position:fixed;top:50%;left:50%;width:28px;height:28px;margin:-14px 0 0 -14px;"
        "border:3px solid rgba(128,128,128,.25);border-top-color:#2e7d32;border-radius:50%;"
        "animation:nbspin .8s linear infinite;z-index:5}"
        "@keyframes nbspin{to{transform:rotate(360deg)}}</style><div class='nb-wait'></div>",
        unsafe_allow_html=True,
    )

    @st.fragment(run_every="6s")
    def _give_up() -> None:
        # The browser never answered (blocked script?): after a few seconds show the sign-in form.
        if st.session_state.get("_pend_seen"):
            st.session_state["_pend_gave_up"] = True
            st.rerun()
        st.session_state["_pend_seen"] = True

    _give_up()
    st.stop()

if session.is_logged_in():
    nav = st.navigation(navigation.pages().menu())
    # Uploads run in the sidebar so they keep going on every page.
    from dashboard.components import browser_upload
    from dashboard.services import uploads

    with st.sidebar:
        event = browser_upload.new_event(browser_upload.manager(uploads.manager_defs()), uploads.SEEN)
        uploads.handle(event)      # finish an upload (thumbnail, publish) whatever page is open
        uploads.sidebar_notices()
    sidebar_account()
else:
    nav = st.navigation(
        [st.Page(login.render, title="Sign in", icon=":material/login:", url_path="login", default=True)],
        position="hidden",
    )

navigation.track(nav)
nav.run()
