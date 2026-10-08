"""Staying signed in across a page refresh (dashboard/auth/remember.py).

A refresh is a new Streamlit session in the same process, so these tests use two AppTest sessions:
the first signs in, the second is "the same tab after a refresh" and only knows the key from sessionStorage.
"""

import time

import pytest
from streamlit.testing.v1 import AppTest

from dashboard.auth import remember
from dashboard.config import settings


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    remember._store.clear()
    monkeypatch.setattr(settings, "SESSION_REMEMBER_HOURS", 12)
    yield
    remember._store.clear()


def _signed_in():
    def app():
        import streamlit as st
        from dashboard.auth import remember, session
        st.session_state["auth.access_token"] = "A1"
        st.session_state["auth.refresh_token"] = "R1"
        st.session_state["auth.expires_at"] = 123.0
        st.session_state["auth.user"] = "the-user"
        remember.sync()
        st.session_state["key"] = remember.current_key()
    at = AppTest.from_function(app)
    at.run()
    assert not at.exception, at.exception
    return at, at.session_state["key"]


def _after_refresh(key):
    def app():
        import streamlit as st
        from dashboard.auth import remember, session
        st.session_state["restored"] = remember.restore(st.query_params.get("k"))
        st.session_state["logged_in"] = session.is_logged_in()
        st.session_state["refresh"] = st.session_state.get("auth.refresh_token")
        st.session_state["tried"] = remember.tried()
    at = AppTest.from_function(app)
    at.query_params["k"] = key or ""
    at.run()
    assert not at.exception, at.exception
    return at


def test_a_refreshed_tab_is_signed_in_again():
    _, key = _signed_in()
    assert key and len(key) >= 40
    at = _after_refresh(key)
    assert at.session_state["restored"] is True
    assert at.session_state["logged_in"] is True
    assert at.session_state["refresh"] == "R1"


def test_rotated_tokens_are_what_gets_restored():
    at, key = _signed_in()
    at.session_state["auth.refresh_token"] = "R2"          # the server rotated the token meanwhile
    at.run()                                                # (a normal run re-syncs; here the app re-runs sync)
    # the fresh session below must see the latest pair, not the first one
    def app():
        import streamlit as st
        from dashboard.auth import remember
        st.session_state["auth.access_token"] = "A2"
        st.session_state["auth.refresh_token"] = "R2"
        st.session_state["auth.expires_at"] = 456.0
        st.session_state["auth.user"] = "the-user"
        st.session_state["auth.remember_key"] = st.query_params["k"]
        remember.sync()
    t = AppTest.from_function(app); t.query_params["k"] = key; t.run()
    assert _after_refresh(key).session_state["refresh"] == "R2"


def test_signing_out_makes_the_key_useless():
    _signed_in_at, key = _signed_in()

    def out():
        import streamlit as st
        from dashboard.auth import remember
        st.session_state["auth.remember_key"] = st.query_params["k"]
        remember.forget()
        st.session_state["clear"] = remember.take_clear()
    t = AppTest.from_function(out); t.query_params["k"] = key; t.run()
    assert t.session_state["clear"] is True                # the browser is told to erase its key
    at = _after_refresh(key)
    assert at.session_state["restored"] is False and at.session_state["logged_in"] is False


def test_an_unknown_key_does_not_sign_anyone_in():
    at = _after_refresh("not-a-real-key")
    assert at.session_state["restored"] is False and at.session_state["logged_in"] is False
    assert at.session_state["tried"] is True                # and it isn't tried again in this session


def test_keys_expire_after_the_idle_time(monkeypatch):
    _, key = _signed_in()
    monkeypatch.setattr(settings, "SESSION_REMEMBER_HOURS", 1 / 3600 / 10)     # 0.1 second
    time.sleep(0.3)
    assert _after_refresh(key).session_state["restored"] is False


def test_switched_off_means_every_refresh_signs_out(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_REMEMBER_HOURS", 0)
    at, key = _signed_in()
    assert key is None
    assert _after_refresh("anything").session_state["restored"] is False


def test_two_sessions_get_different_keys():
    _, a = _signed_in()
    _, b = _signed_in()
    assert a != b


def test_waiting_for_the_browser_draws_no_sign_in_form():
    """While the browser has not answered yet (a refresh), only a spinner is drawn, never the form."""
    from pathlib import Path
    main = str(Path(__file__).resolve().parents[1] / "main.py")
    at = AppTest.from_file(main, default_timeout=15).run()
    assert not at.exception, at.exception
    assert not at.text_input and not any(b.label == "Send OTP" for b in at.button)
    assert any("nb-wait" in m.value for m in at.markdown)
