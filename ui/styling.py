"""
ui/styling.py — Shared CSS polish injected once per script run.

Targets Streamlit's stable data-testid hooks rather than generated class
names (those change between versions). Kept deliberately light-touch: the
base theme (.streamlit/config.toml) already does the heavy lifting for
colors/fonts - this just adds card depth, hover feedback, and a bit more
visual weight to primary actions, on top of both the light and dark themes.
"""

import streamlit as st

_CSS = """
<style>
/* Card depth for forms (login, bootstrap, tenant connect) */
div[data-testid="stForm"] {
    border-radius: 16px;
    padding: 1.75rem 1.75rem 1rem 1.75rem;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.10), 0 10px 30px rgba(0, 0, 0, 0.06);
}

/* Note: st.container(border=True) renders its border via a Streamlit-
   generated emotion-cache class name that isn't stable across versions/
   builds, so it's deliberately not targeted here - the theme's own
   showWidgetBorder styling (.streamlit/config.toml) already covers it,
   and hardcoding a hashed class name would silently stop working on the
   next Streamlit upgrade. */

/* Primary buttons carry a bit more visual weight than default Streamlit */
button[kind="primary"], button[kind="primaryFormSubmit"] {
    font-weight: 600 !important;
    box-shadow: 0 2px 10px rgba(37, 99, 235, 0.22);
    transition: box-shadow 0.18s ease, transform 0.18s ease;
}
button[kind="primary"]:hover, button[kind="primaryFormSubmit"]:hover {
    box-shadow: 0 4px 16px rgba(37, 99, 235, 0.32);
    transform: translateY(-1px);
}

/* st.metric cards get a touch more breathing room */
div[data-testid="stMetric"] {
    padding: 0.5rem 0.25rem;
}

/* Sidebar nav links (st.navigation) render noticeably smaller/lighter than
   the rest of the sidebar's custom headers by default - match them up.
   Several selectors targeted since the exact testid isn't guaranteed stable
   across Streamlit versions; harmless if some don't match anything. */
[data-testid="stSidebarNav"] a,
[data-testid="stSidebarNav"] span,
[data-testid="stSidebarNavLink"],
section[data-testid="stSidebar"] nav a,
section[data-testid="stSidebar"] nav span {
    font-size: 1rem !important;
    font-weight: 500 !important;
}
[data-testid="stSidebarNav"] [data-testid="stSidebarNavSectionHeader"] {
    font-size: 0.85rem !important;
    font-weight: 700 !important;
    text-transform: none !important;
}

/* Login / setup gate hero heading - a bit more presence than a plain ## */
.finops-hero-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 56px;
    height: 56px;
    border-radius: 14px;
    background: linear-gradient(135deg, #2563EB, #60A5FA);
    font-size: 28px;
    margin-bottom: 0.5rem;
    box-shadow: 0 6px 20px rgba(37, 99, 235, 0.35);
}
</style>
"""


def inject_global_css():
    """Idempotent - safe to call multiple times per run (login screen +
    main app both call it); Streamlit just renders the same <style> tag
    again, which browsers dedupe with no visible effect."""
    st.markdown(_CSS, unsafe_allow_html=True)
