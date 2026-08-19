"""
ui/auth_page.py — Login gate for the dashboard. A single screen: a Demo/
Production mode toggle up top, with the matching form directly beneath it -
switching the toggle swaps the form instantly (no separate "choose your
path" screen/step, no Back button - replaced 2026-08 after user feedback
that the old two-screen click-through read as clunky).

  Demo Mode:       pre-filled shared demo credentials, sign in immediately.
  Production Mode: first run prompts to create an account (bootstrap), then
                    sign in with it; returning users just sign in normally.

Deliberately minimal placeholder ahead of real Entra ID / OIDC login via
Streamlit's native st.login() (see PROJECT_CONTEXT.md) - exists so the app
isn't wide open, not as a long-term identity system. One shared account list
for the whole app (db/users.py), no roles, no per-user tenant scoping.
"""

import streamlit as st

from db.users import (
    user_count, production_user_count, create_user, verify_login,
    ensure_demo_user, DEMO_USERNAME, DEMO_PASSWORD,
)
from db.sessions import create_session, get_session, delete_session
from ui.styling import inject_global_css

_ENV_MODE_BY_MODE = {"demo": "Demo / Benchmark Mode", "live": "Live Cloud API"}


def _start_session(user: dict, mode: str) -> None:
    """Signs `user` in for this browser tab AND makes it survive a refresh -
    stores auth_user in session_state (fast path for every rerun within this
    same connection) plus a token in the URL query string mapped to a DB row
    (db/sessions.py) that a fresh page load restores from, since
    session_state alone doesn't survive one. Call this instead of setting
    st.session_state["auth_user"] directly."""
    st.session_state["auth_user"] = user
    st.session_state["env_mode_widget"] = _ENV_MODE_BY_MODE[mode]
    token = create_session(user, mode)
    st.session_state["_session_token"] = token
    st.query_params["s"] = token


def require_login() -> dict:
    """Renders the login screen (a Demo/Production mode toggle with the
    matching form directly beneath it) and halts the script with st.stop()
    until someone is authenticated. Returns the session's user dict once
    authenticated. Call once, right after st.set_page_config() and database
    init, before anything else renders."""
    if st.session_state.get("auth_user"):
        return st.session_state["auth_user"]

    # Not in session_state - either genuinely never logged in, or this is a
    # fresh page load (refresh/reopened tab) that lost it, as every one does
    # in Streamlit. Check for a session token in the URL before falling back
    # to the login screen - restores the exact same session transparently.
    token = st.query_params.get("s")
    if token:
        restored = get_session(token)
        if restored:
            user = {"id": restored["id"], "username": restored["username"], "display_name": restored["display_name"]}
            st.session_state["auth_user"] = user
            st.session_state["env_mode_widget"] = _ENV_MODE_BY_MODE[restored["mode"]]
            st.session_state["_session_token"] = token
            return user
        # Stale/expired/unknown token - drop it from the URL so it doesn't
        # keep getting checked (and failing) on every subsequent load.
        del st.query_params["s"]

    inject_global_css()

    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.markdown(
            '<div style="text-align:center">'
            '<div class="finops-hero-badge">⚡</div>'
            "<h2 style=\"margin-bottom:0\">Multi-Cloud FinOps Optimization System</h2>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            '<div style="text-align:center">Track spend, savings plans, and reservations across Azure and AWS</div>',
            unsafe_allow_html=True,
        )
        st.write("")

        st.caption("Mode")
        mode = st.segmented_control(
            "Mode", options=["Demo", "Production"], default="Demo",
            key="_login_mode_widget", label_visibility="collapsed",
        )
        if mode is None:
            # segmented_control allows deselecting the active pill (clicking
            # it again) - fall back to Demo rather than showing no form at all.
            mode = "Demo"
        st.write("")

        if mode == "Production":
            _render_production_login()
        else:
            _render_demo_login()
    st.stop()


def _render_demo_login():
    ensure_demo_user()

    st.subheader("📊 Demo Mode")
    st.info(f"Pre-filled below - **username `{DEMO_USERNAME}`** / **password `{DEMO_PASSWORD}`**. Just click Sign In.")
    with st.form("demo_login_form"):
        username = st.text_input("Username", value=DEMO_USERNAME)
        password = st.text_input("Password", value=DEMO_PASSWORD, type="password")
        submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)

    if submitted:
        user = verify_login(username, password, mode="demo")
        if user:
            _start_session(user, "demo")
            st.rerun()
        else:
            st.error("Invalid username or password.")


def _render_production_login():
    try:
        existing_prod_users = production_user_count()
    except Exception as e:
        st.error(f"Could not reach the user database: {e}")
        st.stop()

    if existing_prod_users == 0:
        _render_bootstrap_form()
    else:
        _render_production_login_form()


def _render_bootstrap_form():
    st.subheader("👋 Welcome — set up the first account")
    st.caption(
        "No production accounts exist yet. Create the first one to access the dashboard. "
        "More people can be added later from the User Management page."
    )
    with st.form("bootstrap_form"):
        username = st.text_input("Username")
        display_name = st.text_input("Display name (optional)")
        pw1 = st.text_input("Password", type="password")
        pw2 = st.text_input("Confirm password", type="password")
        submitted = st.form_submit_button("Create Account", type="primary", use_container_width=True)

    if submitted:
        if not username or not pw1:
            st.error("Username and password are both required.")
        elif pw1 != pw2:
            st.error("Passwords don't match.")
        elif len(pw1) < 8:
            st.error("Use at least 8 characters for the password.")
        else:
            try:
                create_user(username, pw1, display_name)
                st.success("Account created — log in below.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))


def _render_production_login_form():
    st.subheader("🏭 Sign in")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)

    if submitted:
        user = verify_login(username, password, mode="live")
        if user:
            # Lands directly on Home (tenant connection happens there now,
            # via its own "Add a new tenant" - no separate gate screen).
            _start_session(user, "live")
            st.rerun()
        else:
            st.error("Invalid username or password.")


def _clear_session():
    """Drops every piece of session state tied to being signed in, sending
    the next rerun back to the login screen (Mode toggle reset to its Demo
    default). Shared by both the explicit Log out button and the Switch Mode
    button below - switching between Demo and Live is a full sign-out, not
    an in-session toggle (see render_switch_mode_control for why).

    Also deletes the server-side session row and drops the token from the
    URL - without this, the (now stale) URL would still restore the old
    session on the very next page load, undoing the sign-out."""
    delete_session(st.session_state.get("_session_token"))
    if "s" in st.query_params:
        del st.query_params["s"]
    st.session_state.pop("auth_user", None)
    st.session_state.pop("_login_mode_widget", None)
    st.session_state.pop("env_mode_widget", None)
    st.session_state.pop("_session_token", None)


def render_logout_control():
    """Small sidebar widget showing who's signed in with a logout button."""
    user = st.session_state.get("auth_user")
    if not user:
        return
    st.caption(f"Signed in as **{user['display_name'] or user['username']}**")
    if st.button("Log out", use_container_width=True):
        _clear_session()
        st.rerun()


def render_switch_mode_control():
    """Read-only display of the current Demo/Live mode, plus a button that
    signs out and returns to the login screen (its Mode toggle) to switch.

    Deliberately NOT a free in-session toggle (that's how this worked before
    2026-08, and how the sidebar "Data Source Environment" radio used to
    behave) - now that demo and live have genuinely separate data AND
    genuinely separate login accounts (db/schema.py's demo/live split), a
    silent in-session switch would let someone authenticated as the shared
    Demo user reach the Live scope's real User Management page (add/view
    real accounts) without ever signing in as a real user. Same reasoning
    Salesforce uses for sandbox vs production: separate logins, no toggle,
    because mixing them up silently is a real, recurring incident pattern
    elsewhere - not a hypothetical."""
    env_mode = st.session_state.get("env_mode_widget", "Demo / Benchmark Mode")
    other_mode = "Live Cloud API" if env_mode == "Demo / Benchmark Mode" else "Demo / Benchmark Mode"
    st.caption(f"**Data Source Environment:** {env_mode}")
    if st.button(f"🔁 Switch to {other_mode}", use_container_width=True,
                 help="Switching signs you out - sign back in for the other mode. Demo and Live are fully separate accounts and data now, not just a view toggle."):
        _clear_session()
        st.rerun()
    return env_mode
