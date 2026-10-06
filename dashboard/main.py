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

if session.is_logged_in():
    nav = st.navigation(navigation.pages().menu())
    sidebar_account()
else:
    nav = st.navigation(
        [st.Page(login.render, title="Sign in", icon=":material/login:", url_path="login", default=True)],
        position="hidden",
    )

navigation.track(nav)
nav.run()
