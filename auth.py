"""A password gate in front of everything.

WHY THIS EXISTS, given Streamlit Cloud already has a viewer allowlist.

This repository is public, and on Streamlit Community Cloud an app *inherits its
default visibility from the repository* — a private repo deploys private, a
public repo deploys **public**. The allowlist is still available, but it is now
something you have to remember to switch on rather than something you get for
free.

That asymmetry is the whole argument. What this page shows is every customer's
repository names, package versions and migration history, and the cost of
forgetting one toggle is that all of it is on the open internet and indexable.
A password does not depend on remembering a setting in someone else's dashboard.

It is a second lock, not a replacement: keep the viewer allowlist on as well.

TWO DELIBERATE CHOICES

**It fails closed.** No password configured means the app refuses to run, rather
than running open. A public repo makes the unconfigured case genuinely likely —
anyone can fork this and deploy it in a minute — and "someone deployed it
without reading the README" must not be the same thing as "the store is
readable by the internet".

**The comparison is constant-time.** `==` on a secret leaks its length and
prefix through timing. `hmac.compare_digest` is the standard fix and costs
nothing here.
"""

from __future__ import annotations

import hmac
import os

import streamlit as st

_SESSION_KEY = "_authenticated"


def _configured_password() -> str | None:
    """`st.secrets` first, then the environment, so local runs work unchanged."""
    try:
        value = st.secrets.get("DASHBOARD_PASSWORD")  # type: ignore[union-attr]
        if value:
            return str(value)
    except Exception:
        # No secrets.toml at all is the normal local case, not an error.
        pass
    return os.environ.get("DASHBOARD_PASSWORD")


def require_password() -> None:
    """Halt the script unless the visitor has entered the password.

    Call this first, before anything touches the database. `st.stop()` ends the
    run, so nothing below it renders and no query is issued.
    """
    expected = _configured_password()

    if not expected:
        st.title("🔒 gurt admin")
        st.error(
            "**DASHBOARD_PASSWORD is not set, so this app will not start.**\n\n"
            "This is deliberate. The dashboard exposes customer repository "
            "names and migration history, and refusing to run is the safe "
            "failure — an unconfigured deployment must not be an open one."
        )
        st.caption(
            "Set it in the app's Secrets (Streamlit Cloud) or export it locally, "
            "then reload."
        )
        st.stop()

    if st.session_state.get(_SESSION_KEY):
        return

    st.title("🔒 gurt admin")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        # Constant-time: `==` would leak the secret's length and prefix.
        if hmac.compare_digest(entered, expected):
            st.session_state[_SESSION_KEY] = True
            # Only the boolean is kept — never the password itself, which would
            # otherwise sit in session state for the life of the session.
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.stop()
