"""
ui/auth_page.py — Simple shared login gate for the dashboard.

Deliberately minimal placeholder ahead of real Entra ID / OIDC login via
Streamlit's native st.login() (see PROJECT_CONTEXT.md) - exists so the app
isn't wide open, not as a long-term identity system. One shared account list
for the whole app (db/users.py), no roles, no per-user tenant scoping.
"""

import streamlit as st

from db.users import user_count, create_user, verify_login


def require_login() -> dict:
    """Renders a login (or first-run bootstrap) screen and halts the script
    with st.stop() if nobody is authenticated yet. Returns the session's user
    dict once authenticated. Call once, right after st.set_page_config() and
    database init, before anything else renders."""
    if st.session_state.get("auth_user"):
        return st.session_state["auth_user"]

    st.markdown("## ⚡ FinOps Engine")
    st.caption("Cloud Cost & Commitment Optimizer")

    try:
        existing_users = user_count()
    except Exception as e:
        st.error(f"Could not reach the user database: {e}")
        st.stop()

    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        if existing_users == 0:
            _render_bootstrap_form()
        else:
            _render_login_form()
    st.stop()


def _render_bootstrap_form():
    st.subheader("👋 Welcome — set up the first account")
    st.caption(
        "No users exist yet. Create the first account to access the dashboard. "
        "More people can be added later from Settings & Connections."
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


def _render_login_form():
    st.subheader("🔒 Sign in")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)

    if submitted:
        user = verify_login(username, password)
        if user:
            st.session_state["auth_user"] = user
            st.rerun()
        else:
            st.error("Invalid username or password.")


def render_logout_control():
    """Small sidebar widget showing who's signed in with a logout button."""
    user = st.session_state.get("auth_user")
    if not user:
        return
    st.caption(f"Signed in as **{user['display_name'] or user['username']}**")
    if st.button("Log out", use_container_width=True):
        del st.session_state["auth_user"]
        st.rerun()
