"""
ui/auth_page.py — Login gate for the dashboard. A landing page (hero, real
feature list, connect/sync/recommend process, platform badges - added
2026-08-27, the "Instrument Panel" direction from the design canvas, after
feedback that a bare login card didn't read as a mature product) with the
actual functional sign-in at the bottom: a Demo/Production mode toggle with
the matching form directly beneath it - switching the toggle swaps the form
instantly (no separate "choose your path" screen/step, no Back button -
replaced 2026-08 after user feedback that the old two-screen click-through
read as clunky).

  Demo Mode:       pre-filled shared demo credentials, sign in immediately.
  Production Mode: first run prompts to create an account (bootstrap), then
                    sign in with it; returning users just sign in normally.

The sign-in mechanism itself is a deliberately minimal placeholder ahead of
real Entra ID / OIDC login via Streamlit's native st.login() (see
PROJECT_CONTEXT.md) - exists so the app isn't wide open, not as a long-term
identity system. One shared account list for the whole app (db/users.py),
no roles, no per-user tenant scoping. The landing content above it is real
product marketing, not a placeholder - see _render_landing_* below.
"""

import streamlit as st

from db.users import (
    user_count, production_user_count, create_user, verify_login,
    ensure_demo_user, DEMO_USERNAME, DEMO_PASSWORD,
)
from db.sessions import create_session, get_session, delete_session
from ui.styling import inject_global_css, sidebar_icon

_ENV_MODE_BY_MODE = {"demo": "Demo Mode", "live": "Production"}

# Inline SVG sprite, ported verbatim from the approved design-canvas source
# (Landing.dc.html) - one <symbol> per icon, referenced via <use href="#i-x">
# wherever needed below, same technique the canvas itself used.
_ICON_SPRITE = """
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
<defs>
<symbol id="i-bolt" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round" d="M13 3 5 13.5h5.6L11 21l8-11h-5.6L13 3Z"/></symbol>
<symbol id="i-arrow" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m13 6 6 6-6 6"/></g></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.3-4.3"/></g></symbol>
<symbol id="i-coins" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="6.2" rx="7" ry="2.8"/><path d="M5 6.2v5c0 1.55 3.13 2.8 7 2.8s7-1.25 7-2.8v-5"/><path d="M5 11.2v5c0 1.55 3.13 2.8 7 2.8s7-1.25 7-2.8v-5"/></g></symbol>
<symbol id="i-tag" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"><path d="M11.6 4H6.2A2.2 2.2 0 0 0 4 6.2v5.4c0 .58.23 1.14.64 1.55l8.4 8.4a2.2 2.2 0 0 0 3.1 0l5.4-5.4a2.2 2.2 0 0 0 0-3.1l-8.4-8.4A2.2 2.2 0 0 0 11.6 4Z"/><circle cx="8.3" cy="9" r="1.3" fill="currentColor" stroke="none"/></g></symbol>
<symbol id="i-target" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".8" fill="currentColor" stroke="none"/></g></symbol>
<symbol id="i-bars" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"><path d="M4.5 20V10.5"/><path d="M12 20V4"/><path d="M19.5 20v-7.5"/></g></symbol>
<symbol id="i-boltf" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round" d="M13 3 5 13.5h5.6L11 21l8-11h-5.6L13 3Z"/></symbol>
<symbol id="i-compass" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m15.2 8.8-2 6.4-6.4 2 2-6.4 6.4-2Z"/></g></symbol>
<symbol id="i-cloud" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round" d="M7.2 18a4.5 4.5 0 0 1-.5-8.97A6 6 0 0 1 18 8.5a4 4 0 0 1-1 7.5H7.2Z"/></symbol>
<symbol id="i-hex" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round" d="M12 3 20 8v8l-8 5-8-5V8Z"/></symbol>
<symbol id="i-grid" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"><rect x="3.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.2"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.2"/></g></symbol>
<symbol id="i-globe" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></g></symbol>
<symbol id="i-coin" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75"><circle cx="12" cy="12" r="9"/><path d="M12 7v10M9.5 9.3c0-1.3 1.1-2 2.5-2s2.5.7 2.5 1.8c0 2.6-5 1.4-5 4 0 1.1 1.1 1.9 2.5 1.9s2.5-.7 2.5-1.9"/></g></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m7.8 12.4 2.6 2.6 5.8-5.8"/></g></symbol>
<symbol id="i-key" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="15" r="4.5"/><path d="m11.2 11.8 8.3-8.3M16.5 6.5l2.6 2.6M19.3 3.7l2 2"/></g></symbol>
</defs>
</svg>
"""


def _icon(name: str, size: int = 15, color: str = "currentColor") -> str:
    return f'<svg style="width:{size}px;height:{size}px;flex-shrink:0;color:{color};"><use href="#i-{name}"/></svg>'


def _start_session(user: dict, mode: str) -> None:
    """Signs `user` in for this browser tab AND makes it survive a refresh -
    stores auth_user in session_state (fast path for every rerun within this
    same connection) plus a token in the URL query string mapped to a DB row
    (db/sessions.py) that a fresh page load restores from, since
    session_state alone doesn't survive one. Call this instead of setting
    st.session_state["auth_user"] directly.

    create_session() can return None (a transient DB hiccup, e.g. right as
    Azure SQL Serverless wakes from auto-pause) - sign-in still succeeds for
    this connection either way; only refresh-persistence is degraded, and
    only until the next successful login."""
    st.session_state["auth_user"] = user
    st.session_state["env_mode_widget"] = _ENV_MODE_BY_MODE[mode]
    token = create_session(user, mode)
    if token:
        st.session_state["_session_token"] = token
        st.query_params["s"] = token


def require_login() -> dict:
    """Renders the login screen (a Demo/Production mode toggle with the
    matching form directly beneath it) and halts the script with st.stop()
    until someone is authenticated. Returns the session's user dict once
    authenticated. Call once, right after st.set_page_config() and database
    init, before anything else renders."""
    # Real bug reported 2026-08-30: after logout, the main content
    # correctly falls through to the login screen, but the SIDEBAR still
    # shows the previous run's logged-in nav menu (Home/Tenant Management/
    # User Management) until a manual browser refresh - that menu is
    # rendered by st.navigation() itself (called much later in app.py,
    # only when logged in), not by anything in this function, so it never
    # gets a chance to be told "gone" on a run that returns before ever
    # reaching st.navigation() again. Three attempts to force a reload
    # from a normal st.button's click handler all failed, each verified
    # live in the browser: st.sidebar.empty() (that stale menu isn't a
    # normal element app code can clear via the usual delta mechanism), a
    # st.components.v1.html <script>window.parent.location.reload()
    # (didn't execute - iframe/CSP sandboxing blocks reaching
    # window.parent here), and a st.markdown(..., unsafe_allow_html=True)
    # <meta http-equiv="refresh"> tag (browsers only reliably honor that
    # when it's genuinely in the document's initial <head>, not when
    # React-rendered into the body later). A real st.link_button (an
    # ACTUAL <a href> the browser navigates on its own) is the one thing
    # confirmed to actually trigger a real navigation and clear the stale
    # menu - a first version of this fix used one for a SEPARATE
    # "Finish logging out" confirmation click, since a link_button can't
    # run Python on click and _clear_session() needs to happen somewhere -
    # real follow-up feedback: that extra click/screen wasn't wanted, "Log
    # out" should be one click, full stop. Fixed by making "Log out"
    # ITSELF the real link (see render_logout_control() below - a
    # st.link_button straight to "/?logout=1"), and doing the actual
    # session teardown HERE, at the top of the very next (genuinely
    # navigated-to, sidebar-resetting) page load, keyed off that query
    # param - the user still only clicks once, the real navigation just
    # happens to be a fraction of a second before the teardown instead of
    # a fraction after it.
    if st.query_params.get("logout") == "1":
        _clear_session()
        del st.query_params["logout"]

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
    st.markdown(_ICON_SPRITE + '<div class="fl-page">', unsafe_allow_html=True)
    # Mode lives in a plain session_state key, not a widget's own key - see
    # _render_landing_closing_cta's docstring for why (fourth pass,
    # 2026-08-27: the previous top-bar segmented_control version crashed
    # with a real StreamlitAPIException the moment the in-card buttons
    # tried to override it, and the top bar was removed anyway per direct
    # user request, so this is both a bug fix and a simplification).
    mode = st.session_state.get("login_mode", "Demo")
    _render_landing_hero()
    _render_landing_features()
    _render_landing_process()
    # No platform-badges section here anymore (removed 2026-08-27, real
    # user feedback: "Azure/AWS/Demo Mode/USD" duplicated the hero's own
    # preview-card badges, and the mode itself is already a real choice in
    # the sign-in card right below - not just decorative repetition).
    _render_landing_closing_cta(mode)
    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()


def _render_landing_hero():
    """Top-of-page hero: what this is, and the real problem it solves - not
    just a bare login card. Added 2026-08-27 (the "Instrument Panel"
    direction picked from the design canvas) after direct user feedback that
    a bare login form didn't read as a mature, purposeful product. Content
    and markup ported verbatim from the approved canvas source
    (Landing.dc.html) - a first pass reconstructed this from a text summary
    instead and didn't match what was approved; this is the correction.

    The "Live Portfolio Snapshot" stats are the canvas's own illustrative
    example numbers (42 running VMs, $1,284 est. savings, etc.), not a live
    query against the demo DB - the card's own subtitle ("What you see the
    moment you connect a tenant") already frames it as a preview of the
    post-login experience, not a claim that it's live right now, so this
    doesn't overstate anything."""
    st.markdown(
        '<div class="fl-hero"><div class="fl-glow"></div><div class="fl-wrap fl-herogrid">'
        '<div class="fl-heroleft">'
        f'<span class="fl-kicker">{_icon("bolt", 12)}Enterprise FinOps Platform</span>'
        '<h1>Stop paying for cloud capacity <span class="fl-accent">you aren\'t using.</span></h1>'
        '<p class="fl-lead">Multi-Cloud FinOps Optimization System turns idle resources, uncommitted spend, '
        "and rightsizing opportunities across Azure and AWS into real, actionable numbers — not another "
        "dashboard full of noise.</p>"
        '<div class="fl-ctarow">'
        f'<a href="#fl-signin-anchor" class="fl-btn-primary">Sign in to your dashboard{_icon("arrow", 16, "#0B1120")}</a>'
        '<a href="#fl-features-anchor" class="fl-link-secondary">See how it works ↓</a>'
        "</div>"
        '<p class="fl-trustline">Demo Mode available — explore with realistic sample data before connecting a real tenant.</p>'
        "</div>"
        '<div class="fl-heroright"><div class="fl-previewcard">'
        '<div class="fl-previewhead"><div><div class="fl-previewtitle">Live Portfolio Snapshot</div>'
        '<div class="fl-previewsub">What you see the moment you connect a tenant</div></div>'
        f'<span class="fl-livepill"><span class="fl-pulsedot"></span>Synced</span></div>'
        '<div class="fl-badgerow">'
        '<span class="fl-pbadge azure">☁ Azure</span><span class="fl-pbadge">AWS</span>'
        '<span class="fl-pbadge">Demo Mode</span><span class="fl-pbadge">USD</span></div>'
        '<div class="fl-statgrid">'
        '<div class="fl-pstat"><div class="lbl">Running VMs</div><div class="val fl-mono">42</div></div>'
        '<div class="fl-pstat"><div class="lbl">Running Databases</div><div class="val fl-mono">11</div></div>'
        '<div class="fl-pstat"><div class="lbl">Compute PAYG Rate</div><div class="val fl-mono">$6.18/hr</div></div>'
        '<div class="fl-pstat"><div class="lbl">Total Committed</div><div class="val fl-mono">$4.50/hr</div></div>'
        '<div class="fl-pstat alert"><div class="lbl">Critical Alerts</div><div class="val fl-mono">3 items</div></div>'
        '<div class="fl-pstat"><div class="lbl">Est. Monthly Savings</div>'
        '<div class="val fl-mono" style="color:#34D399;">$1,284</div></div>'
        "</div></div></div>"
        "</div></div>",
        unsafe_allow_html=True,
    )


def _render_landing_features():
    """One card per real Analyze tab (app.py's page_analyze) - kept in sync
    by hand, same as every other place in this app that names its own
    tabs; there's no single shared source of truth for the tab list to pull
    from without a larger refactor than this pass warrants."""
    # Cost Analysis card removed, 2026-08-30 - that tab itself was removed
    # from page_analyze() earlier this session (real user call, not this
    # pass - see git history around 2026-08-30), so this landing page had
    # drifted out of sync with the docstring's own "kept in sync by hand"
    # promise, still advertising a tab that no longer exists. 6 real tabs
    # left (verified directly against page_analyze()'s current st.tabs()
    # list), rebalanced into two even rows of 3 (both using .fl-featuresrow2's
    # centered, narrower grid) instead of the old uneven 4-then-2 that would
    # have resulted from just deleting the card in place.
    row1 = [
        ("search", "#60A5FA", "Asset Inventory", "Real-time compute and database resource registry across both clouds, one unified, filterable table."),
        ("coins", "#34D399", "Savings Plan Analysis", "Coverage vs. commitment gaps for every eligible workload, with a recommended purchase amount."),
        ("tag", "#FBBF24", "RI Coverage", "Reserved Instance gap detection — what's covered, what's leaking to pay-as-you-go."),
    ]
    row2 = [
        ("target", "#A78BFA", "VM / EC2 Rightsizing", "Under- and over-provisioned resource detection with configurable, industry-grounded thresholds."),
        ("boltf", "#F87171", "Recommendations", "Prioritized, actionable savings opportunities — one clear next step per issue."),
        ("compass", "#38BDF8", "FinOps Maturity Assessment", "Scored against the real FinOps Foundation framework, not an in-house rubric."),
    ]

    def _card(icon, color, title, desc):
        return (
            f'<div class="fl-fcard"><div class="fl-ficon" style="background:{color}20;">'
            f'{_icon(icon, 19, color)}</div><h3>{title}</h3><p>{desc}</p></div>'
        )

    st.markdown(
        '<section id="fl-features-anchor" class="fl-section" style="padding-top:20px;"><div class="fl-wrap">'
        '<div class="fl-sectionhead fl-center"><span class="fl-kicker">Platform Features</span>'
        "<h2>Everything a FinOps practice needs, in one place</h2>"
        "<p>Six views into the same portfolio — from raw inventory to a scored maturity assessment — "
        "covering both clouds the same way.</p></div>"
        f'<div class="fl-featuresrow2">{"".join(_card(*c) for c in row1)}</div>'
        f'<div class="fl-featuresrow2">{"".join(_card(*c) for c in row2)}</div>'
        "</div></section>",
        unsafe_allow_html=True,
    )


def _render_landing_process():
    steps = [
        ("01", "Connect a cloud tenant", "Azure Service Principal or AWS IAM keys — read-only access, verified live before you save it."),
        ("02", "Sync inventory", "Pulls compute, database, and commitment data into one registry, refreshed on demand."),
        ("03", "Get recommendations", "Prioritized dashboards surface exactly where spend is leaking and what to do about it."),
    ]
    steps_html = "".join(
        f'<div class="fl-step"><div class="fl-num">{n}</div><h3>{t}</h3><p>{d}</p></div>'
        for n, t, d in steps
    )
    st.markdown(
        '<section class="fl-section" style="padding-top:20px;"><div class="fl-wrap">'
        '<div class="fl-sectionhead fl-center"><span class="fl-kicker">Simple Process</span>'
        "<h2>From connection to recommendation in three steps</h2></div>"
        f'<div class="fl-steps">{steps_html}</div>'
        "</div></section>",
        unsafe_allow_html=True,
    )



def _render_landing_closing_cta(mode: str):
    """Closing section - fourth pass, 2026-08-27, after several rounds of
    real user feedback (full history kept here since this function has been
    the most iterated-on part of the redesign):
    (a) the two-st.columns()-halves "single card" version never looked
        seamless (Streamlit's column gap can't be fully suppressed) -
        replaced with the same stacked-section rhythm Features/Process
        already use;
    (b) the checklist row and the "Multi-Cloud FinOps Optimization System"
        subtitle under "Sign in" were flagged as redundant - the hero
        already covers Azure+AWS multi-cloud, so both are dropped here;
    (c) a top-bar mode toggle was tried and then explicitly removed again -
        it also crashed with a real StreamlitAPIException the moment the
        in-card buttons below tried to override its session_state value:
        Streamlit forbids writing to a key that belongs to an
        already-instantiated widget in the same run. `mode` now comes from
        a PLAIN session_state key ("login_mode", set in require_login())
        that no widget ever owns, specifically so the buttons below can
        freely write to it without that restriction ever applying.
    _render_demo_login/_render_production_login below are unchanged, still
    the actual auth logic - only the wrapper and its content changed.

    id="fl-signin-anchor" is the scroll target for the hero's "Sign in to
    your dashboard" link - a plain HTML anchor jump, no JS needed."""
    st.markdown(
        '<section class="fl-section fl-closing" style="padding-bottom:20px;"><div class="fl-wrap">'
        '<div class="fl-sectionhead fl-center"><span class="fl-kicker">Get Started</span>'
        "<h2>Ready to see where your cloud spend is leaking?</h2>"
        "<p>Sign in below — Demo Mode is pre-filled with sample credentials so you can explore the full "
        "platform in seconds, no cloud account required.</p></div>"
        "</div></section>",
        unsafe_allow_html=True,
    )

    _, mid, _ = st.columns([1, 1.3, 1])
    with mid:
        # Plain anchor span, not part of the styled card - only exists as a
        # scroll target for the top bar's mode-change handler and the
        # hero's "Sign in to your dashboard" link. Kept separate from the
        # card itself (see below) since st.container(key=...) has no way
        # to carry an explicit id attribute.
        st.markdown('<span id="fl-signin-anchor"></span>', unsafe_allow_html=True)
        # Real Streamlit container, not a raw HTML div - the previous div-
        # wrap trick didn't actually nest the widgets that followed it (see
        # the CSS file's own comment on .st-key-fl_signin_card for why),
        # which is exactly what produced the empty floating box the user
        # screenshotted. key="fl_signin_card" gives this a real, stable
        # `st-key-fl_signin_card` CSS class Streamlit itself guarantees.
        with st.container(border=True, key="fl_signin_card"):
            st.markdown('<div class="fl-login-card-title">Sign in</div>', unsafe_allow_html=True)
            # In-card mode switcher, always visible regardless of scroll
            # position. Both buttons write the plain "login_mode" session-
            # state key require_login() reads at the top of the script -
            # not a widget's own key, so there's no "modified after
            # instantiation" restriction to run into.
            bc1, bc2 = st.columns(2)
            with bc1:
                if st.button("Demo Mode", key="fl_card_demo_btn", width="stretch",
                             type="primary" if mode == "Demo" else "secondary"):
                    st.session_state["login_mode"] = "Demo"
                    st.rerun()
            with bc2:
                if st.button("Production Mode", key="fl_card_prod_btn", width="stretch",
                             type="primary" if mode == "Production" else "secondary"):
                    st.session_state["login_mode"] = "Production"
                    st.rerun()
            st.write("")
            if mode == "Production":
                _render_production_login()
            else:
                _render_demo_login()


def _render_demo_login():
    ensure_demo_user()

    # No mode heading here (dropped 2026-08-27) - the "Demo Mode" button
    # above is already highlighted/active, repeating the label right below
    # it was flagged as literal duplicate text. The pre-filled-credentials
    # st.info() was also dropped the same round - its fixed alert-blue
    # background didn't sit well against this page's own dark palette
    # (real user feedback), and the fields right below already show the
    # actual pre-filled values, so the message wasn't adding information
    # anyway, just a color clash.
    # Wrapped in its own placeholder, 2026-08-30 - real gap the user caught
    # live: after a successful login, this exact form stayed on screen as a
    # "watermark" for the several seconds init_all_databases()/
    # load_live_data() take to actually run (both happen well after this
    # function returns, and Streamlit doesn't clear old elements until NEW
    # ones arrive to replace them - a plain st.rerun() alone doesn't blank
    # the screen at the instant it's called). Scoped to just this form (not
    # the whole hero/features/process landing page around it, which has its
    # own documented HTML-fragility history in this file) - redrawing INTO
    # the same placeholder replaces its content immediately, in this same
    # run, before the rerun/slow startup work even begins.
    _signin_ph = st.empty()
    with _signin_ph.container():
        with st.form("demo_login_form"):
            username = st.text_input("Username", value=DEMO_USERNAME)
            password = st.text_input("Password", value=DEMO_PASSWORD, type="password")
            submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)

    if submitted:
        user = verify_login(username, password, mode="demo")
        if user:
            with _signin_ph.container():
                st.info("Signing you in...")
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
    # No mode heading here either (see _render_demo_login's comment) - both
    # the "Production Mode" button above and the card's own "Sign in" title
    # already say this.
    # Same placeholder-wrap fix as _render_demo_login above, same reason -
    # see that function's comment.
    _signin_ph = st.empty()
    with _signin_ph.container():
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)

    if submitted:
        user = verify_login(username, password, mode="live")
        if user:
            with _signin_ph.container():
                st.info("Signing you in...")
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
    st.session_state.pop("login_mode", None)
    st.session_state.pop("env_mode_widget", None)
    st.session_state.pop("_session_token", None)


def render_logout_control():
    """Small sidebar widget showing who's signed in with a logout button."""
    user = st.session_state.get("auth_user")
    if not user:
        return
    st.caption(f"Signed in as **{user['display_name'] or user['username']}**")
    # A real link (genuine <a href>, not a Streamlit button + rerun) - see
    # require_login()'s own handling of "?logout=1" for why: only an
    # actual browser navigation clears the sidebar's st.navigation() nav
    # menu, which stays stuck from the prior logged-in run otherwise. One
    # click, no confirmation screen - the session teardown happens on the
    # destination page's own load, keyed off this same query param.
    #
    # NOT st.link_button - confirmed via the installed LinkButton.*.js
    # bundle that it hardcodes target="_blank" with no way to override, so
    # every click opened a second tab (real user feedback, 2026-08-30).
    # Switching to a raw <a> wasn't enough by itself, either - confirmed via
    # the installed StreamlitMarkdown.*.js bundle that st.markdown's own
    # link renderer ALSO force-defaults target to "_blank" for any anchor
    # that doesn't specify one itself (`target: i || "_blank"` - real user
    # re-test after a full server restart still showed a new tab, which is
    # what led to checking this bundle instead of assuming plain-HTML
    # semantics would hold). Explicitly setting target="_self" here wins
    # over that default, since a real (truthy) target on the source tag is
    # exactly what that check looks for.
    st.markdown(
        f'<a class="fl-sidebar-linkbtn" href="/?logout=1" target="_self">{sidebar_icon("logout")}Log out</a>',
        unsafe_allow_html=True,
    )


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
    # "Demo / Benchmark Mode" renamed to plain "Demo Mode" everywhere,
    # 2026-08-30 (real feedback - "replace benchmark with Demo everywhere"),
    # superseding an earlier, narrower fix that only shortened this value
    # for DISPLAY in this one function while _ENV_MODE_BY_MODE's real,
    # stored value stayed the long "Demo / Benchmark Mode" (see git history
    # around 2026-08-30 for that version) - env_mode is still this
    # function's return value, consumed as real logic elsewhere
    # (app.py's tenant_mode/is_live_mode), but there's no longer a mismatch
    # to work around now that the canonical value is already short and
    # "Demo"-only, so the separate _short()-for-display-only helper this
    # used to need is gone too.
    env_mode = st.session_state.get("env_mode_widget", _ENV_MODE_BY_MODE["demo"])
    other_mode = "Production" if env_mode == _ENV_MODE_BY_MODE["demo"] else _ENV_MODE_BY_MODE["demo"]
    st.markdown(f'<div class="fl-rail-env-line"><b>Environment:</b> {env_mode}</div>', unsafe_allow_html=True)
    # Same real-link fix as the Log out button (render_logout_control()
    # above) - this button signs out exactly the same way, so it needs the
    # identical treatment: a plain <a href target="_self">, not
    # st.link_button (forces a new tab) and not a Streamlit button + rerun
    # (doesn't clear the stale sidebar nav) - and the explicit target="_self"
    # matters here too, since st.markdown's own link renderer force-defaults
    # to target="_blank" for any anchor that doesn't set one (see the other
    # control's comment for how this was actually confirmed). One click, no
    # confirmation screen, same tab.
    st.markdown(
        f'<a class="fl-sidebar-linkbtn" href="/?logout=1" target="_self" '
        f'title="Switching signs you out - sign back in for the other mode. '
        f'Demo and Live are fully separate accounts and data now, not just a '
        f'view toggle.">{sidebar_icon("swap")}Switch to {other_mode}</a>',
        unsafe_allow_html=True,
    )
    return env_mode
