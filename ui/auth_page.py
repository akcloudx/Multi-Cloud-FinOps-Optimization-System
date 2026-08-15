"""
ui/auth_page.py — Login gate for the dashboard, with the Demo/Production
mode choice made *before* login (not after, like the old post-login setup
gate) - so the login screen itself branches into two flows:

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
from ui.styling import inject_global_css


def require_login() -> dict:
    """Renders the login screen (mode choice -> demo login or production
    bootstrap/login) and halts the script with st.stop() until someone is
    authenticated. Returns the session's user dict once authenticated. Call
    once, right after st.set_page_config() and database init, before
    anything else renders."""
    if st.session_state.get("auth_user"):
        return st.session_state["auth_user"]

    inject_global_css()

    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.markdown(
            '<div style="text-align:center">'
            '<div class="finops-hero-badge">⚡</div>'
            "<h2 style=\"margin-bottom:0\">FinOps Engine</h2>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            '<div style="text-align:center">Cloud Cost &amp; Commitment Optimizer</div>',
            unsafe_allow_html=True,
        )
        st.write("")

        mode_choice = st.session_state.get("_login_mode_choice")
        if mode_choice is None:
            _render_mode_choice()
        elif mode_choice == "demo":
            _render_demo_login()
        else:
            _render_production_login()
    st.stop()


def _render_mode_choice():
    st.caption("Choose how you'd like to start.")
    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            st.markdown("#### 📊 Demo Mode")
            st.caption("Explore instantly with a shared demo account and pre-loaded sample data - no setup needed.")
            if st.button("Continue with Demo", key="login_pick_demo", use_container_width=True, type="primary"):
                st.session_state["_login_mode_choice"] = "demo"
                st.rerun()
    with c2:
        with st.container(border=True):
            st.markdown("#### 🏭 Production Mode")
            st.caption("Create your own account (or sign in) and connect a real cloud tenant.")
            if st.button("Continue with Production", key="login_pick_prod", use_container_width=True, type="primary"):
                st.session_state["_login_mode_choice"] = "production"
                st.rerun()


def _render_demo_login():
    ensure_demo_user()

    if st.button("← Back", key="login_back_demo"):
        del st.session_state["_login_mode_choice"]
        st.rerun()

    st.subheader("📊 Demo Mode")
    st.info(f"Pre-filled below - **username `{DEMO_USERNAME}`** / **password `{DEMO_PASSWORD}`**. Just click Sign In.")
    with st.form("demo_login_form"):
        username = st.text_input("Username", value=DEMO_USERNAME)
        password = st.text_input("Password", value=DEMO_PASSWORD, type="password")
        submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)

    if submitted:
        user = verify_login(username, password, mode="demo")
        if user:
            st.session_state["auth_user"] = user
            # Demo Mode skips the tenant-connection gate entirely - straight
            # to the dashboard on Demo / Benchmark data.
            st.session_state["env_mode_widget"] = "Demo / Benchmark Mode"
            st.session_state["setup_complete"] = True
            st.rerun()
        else:
            st.error("Invalid username or password.")


def _render_production_login():
    if st.button("← Back", key="login_back_prod"):
        del st.session_state["_login_mode_choice"]
        st.rerun()

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
            st.session_state["auth_user"] = user
            # Production Mode still needs a cloud tenant - the (now
            # tenant-only) setup gate in app.py handles that next.
            st.session_state["env_mode_widget"] = "Live Cloud API"
            st.session_state["setup_complete"] = False
            st.rerun()
        else:
            st.error("Invalid username or password.")


def _clear_session():
    """Drops every piece of session state tied to being signed in, sending
    the next rerun back to the mode-choice screen. Shared by both the
    explicit Log out button and the Switch Mode button below - switching
    between Demo and Live is a full sign-out, not an in-session toggle (see
    render_switch_mode_control for why)."""
    st.session_state.pop("auth_user", None)
    st.session_state.pop("_login_mode_choice", None)
    st.session_state.pop("setup_complete", None)
    st.session_state.pop("env_mode_widget", None)


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
    signs out and returns to the mode-choice screen to switch.

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
