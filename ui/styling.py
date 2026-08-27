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

/* Alert boxes (st.info/warning/success/error) toned down app-wide -
   2026-08-27, real user feedback: the default saturated tint read as
   jarring against this app's own dark theme. A CSS filter, not a
   hardcoded background color, on purpose: st.info/warning/success/error
   all share the SAME data-testid/className regardless of which one they
   are - Streamlit picks the actual blue/amber/green/red internally via a
   styled-component prop that isn't exposed as a stable, targetable class,
   so there's no reliable hook to set four different literal colors per
   kind. A filter instead dims/desaturates WHATEVER color Streamlit
   already rendered, uniformly, for all four kinds, and in both light and
   dark theme - not just this one page. Still visibly tinted per kind
   (saturate isn't 0), just not shouting. */
[data-testid="stAlertContainer"] {
    filter: saturate(55%) brightness(0.94);
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

/* ── Login landing page (ui/auth_page.py) ────────────────────────────────
   2026-08-27: ported directly from the approved design-canvas source
   (Landing.dc.html, "Instrument Panel" direction) - exact hex values,
   spacing and class structure carried over verbatim rather than
   reinterpreted from a text summary (a first pass did that and didn't
   match what was approved - this is the correction). fl- prefix throughout
   to avoid colliding with Streamlit's own class names. */
.fl-page { background: #0B1120; color: #F1F5F9; }
.fl-mono { font-family: 'JetBrains Mono', ui-monospace, monospace; font-variant-numeric: tabular-nums; }
.fl-wrap { max-width: 1200px; margin: 0 auto; padding: 0 20px; }
.fl-section { padding: 60px 0; }
.fl-kicker {
    display: inline-flex; align-items: center; gap: 7px;
    font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
    color: #60A5FA; background: rgba(96,165,250,.1); border: 1px solid rgba(96,165,250,.25);
    padding: 6px 14px; border-radius: 999px;
}
.fl-center { text-align: center; }
.fl-sectionhead { max-width: 640px; margin: 0 auto 40px; }
.fl-sectionhead h2 { font-size: 28px; font-weight: 700; letter-spacing: -.015em; margin: 14px 0 10px; }
.fl-sectionhead p { font-size: 14px; color: #94A3B8; line-height: 1.6; margin: 0; }

/* ---- HERO ---- */
.fl-hero { padding: 56px 0 70px; position: relative; }
.fl-glow {
    position: absolute; width: 900px; height: 900px; border-radius: 50%;
    background: radial-gradient(circle, rgba(96,165,250,.16) 0%, rgba(96,165,250,0) 65%);
    top: -420px; left: 50%; transform: translateX(-50%); pointer-events: none;
}
.fl-herogrid { position: relative; display: flex; gap: 48px; align-items: center; flex-wrap: wrap; }
.fl-heroleft { flex: 1.05; min-width: 320px; }
.fl-heroright { flex: 1; min-width: 320px; }
.fl-hero h1 { font-size: 38px; font-weight: 800; line-height: 1.15; letter-spacing: -.02em; margin: 16px 0 16px; }
.fl-hero h1 .fl-accent { color: #60A5FA; }
.fl-hero p.fl-lead { font-size: 15px; color: #94A3B8; line-height: 1.65; max-width: 480px; margin: 0 0 24px; }
.fl-ctarow { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
.fl-btn-primary {
    display: inline-flex; align-items: center; gap: 9px; background: #60A5FA; color: #0B1120 !important;
    border: none; padding: 12px 20px; border-radius: 9px; font-size: 14px; font-weight: 700;
    text-decoration: none !important; cursor: pointer; transition: background 0.15s ease;
}
.fl-btn-primary:hover { background: #93C5FD; }
.fl-link-secondary {
    font-size: 13px; font-weight: 600; color: #CBD5E1 !important; text-decoration: none !important; cursor: pointer;
}
.fl-link-secondary:hover { color: #F1F5F9 !important; }
.fl-trustline { margin-top: 22px; font-size: 11.5px; color: #64748B; }

.fl-previewcard {
    background: #131F35; border: 1px solid #1E293B; border-radius: 16px; padding: 22px 22px 18px;
    box-shadow: 0 30px 70px -30px rgba(0,0,0,.55);
}
.fl-previewhead { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.fl-previewtitle { font-size: 12.5px; font-weight: 700; color: #E2E8F0; }
.fl-previewsub { font-size: 10.5px; color: #64748B; margin-top: 2px; }
.fl-livepill {
    display: inline-flex; align-items: center; gap: 6px; font-size: 10px; font-weight: 700;
    color: #34D399; background: rgba(52,211,153,.1); border: 1px solid rgba(52,211,153,.3);
    padding: 4px 10px; border-radius: 999px;
}
.fl-pulsedot { width: 6px; height: 6px; border-radius: 50%; background: #34D399; }
.fl-badgerow { display: flex; gap: 7px; margin-bottom: 16px; flex-wrap: wrap; }
.fl-pbadge {
    font-size: 10.5px; font-weight: 600; padding: 4px 10px; border-radius: 6px;
    background: #0F1A2E; border: 1px solid #263349; color: #94A3B8;
}
.fl-pbadge.azure { color: #60A5FA; border-color: rgba(96,165,250,.3); }
.fl-statgrid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: #1E293B;
    border: 1px solid #1E293B; border-radius: 10px; overflow: hidden;
}
.fl-pstat { background: #0F1A2E; padding: 12px 14px; }
.fl-pstat .lbl {
    font-size: 9.5px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
    color: #526279; margin-bottom: 5px;
}
.fl-pstat .val { font-size: 16px; font-weight: 700; }
.fl-pstat.alert .val { color: #F87171; }

/* ---- FEATURES ---- */
.fl-featuresrow { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.fl-featuresrow2 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; max-width: 900px; margin: 16px auto 0; }
.fl-fcard { border: 1px solid #1E293B; background: #101A2E; border-radius: 13px; padding: 18px 16px; }
.fl-ficon {
    width: 34px; height: 34px; border-radius: 10px; display: flex; align-items: center;
    justify-content: center; margin-bottom: 12px;
}
.fl-fcard h3 { font-size: 14px; font-weight: 700; margin: 0 0 6px; }
.fl-fcard p { font-size: 12px; color: #94A3B8; line-height: 1.5; margin: 0; }

/* ---- PROCESS ---- */
.fl-steps { display: flex; gap: 24px; flex-wrap: wrap; }
.fl-step { flex: 1; min-width: 200px; border-top: 2px solid #1E293B; padding-top: 16px; }
.fl-step .fl-num { font-size: 13px; font-weight: 800; font-family: 'JetBrains Mono', monospace; color: #60A5FA; margin-bottom: 10px; }
.fl-step h3 { font-size: 15px; font-weight: 700; margin: 0 0 6px; }
.fl-step p { font-size: 12.5px; color: #94A3B8; line-height: 1.55; margin: 0; }

/* ---- BADGES ---- */
.fl-badgesection { padding: 40px 0; border-top: 1px solid #16233A; border-bottom: 1px solid #16233A; }
.fl-badgesection .lbl {
    text-align: center; font-size: 10.5px; font-weight: 700; letter-spacing: .1em;
    text-transform: uppercase; color: #475569; margin-bottom: 18px;
}
.fl-stackrow { display: flex; justify-content: center; gap: 12px; flex-wrap: wrap; }
.fl-stackbadge {
    display: flex; align-items: center; gap: 8px; border: 1px solid #1E293B; background: #101A2E;
    padding: 8px 14px; border-radius: 9px; font-size: 12.5px; font-weight: 600; color: #CBD5E1;
}

/* ---- CLOSING CTA + LOGIN ---- */
.fl-closing { padding: 60px 0 40px; }
/* 2026-08-27, second pass: dropped the two-column "single card split into
   real st.columns() halves" version - Streamlit's own column gap/padding
   can't be fully suppressed, so the two sides never actually looked like
   one seamless card (real user feedback, screenshot showed a visible
   gap/misalignment). Replaced with the same *stacked full-width section*
   pattern the Features/Process sections above it already use successfully
   - a centered sectionhead, a centered checklist row, then the real
   sign-in form in its own small centered card underneath. Matches the
   section-by-section rhythm of the reference site the user pointed to,
   which is also literally what they asked for here. */
/* 2026-08-27, third pass: the previous approach ("open a raw <div> via
   st.markdown, render real widgets after it, close the div later") was a
   fragile hack that turned out not to work in this Streamlit version at
   all - each st.markdown call gets its HTML balanced/closed independently,
   so the div closed itself immediately, leaving an empty floating box
   (visible in the user's screenshot) with every widget after it rendered
   completely outside any scoping wrapper - which is *also* why the inputs
   never picked up the dark styling in the previous two passes: the CSS
   selectors were correct, they just never matched anything, because
   .fl-loginwidgets never actually contained them.

   Fixed properly this time using Streamlit's own documented mechanism for
   exactly this: st.container(key=...) gives a REAL, guaranteed-nested
   container a stable `st-key-<key>` CSS class (confirmed via
   st.container's own docstring in the installed 1.60 build - "if key is
   provided, it will be used as a CSS class name prefixed with st-key-").
   ui/auth_page.py now wraps the sign-in form in
   st.container(border=True, key="fl_signin_card") instead of raw HTML -
   everything below is scoped to that real class, not a hoped-for one. */
.st-key-fl_signin_card {
    background: #101A2E !important; border: 1px solid #1E293B !important; border-radius: 18px !important;
    padding: 18px 20px !important; max-width: 440px; margin: 8px auto 0;
    box-shadow: 0 30px 70px -30px rgba(0,0,0,.5);
}
.fl-login-card-title { font-size: 15px; font-weight: 700; margin-bottom: 20px; text-align: center; }

/* Real Streamlit widgets inside the login card, reskinned to match the
   card's own dark/mono treatment - scoped to this specific container only
   so nothing elsewhere in the app is affected (st.form is also used for
   real, unrelated forms post-login - tenant connect, user management - so
   this can't be a global [data-testid="stForm"] rule without leaking
   there too). Selectors themselves checked against the actual installed
   Streamlit 1.60 bundle (TextInput.*.js, ErrorElement.*.js) rather than
   guessed - `stTextInputRootElement` is the real bordered input box
   (confirmed in the bundle), and `stAlertContainer`/`stAlertContent*` are
   both a data-testid AND a plain className on the same element (both
   targeted as belt-and-suspenders). */
.st-key-fl_signin_card [data-testid="stForm"] {
    border: none !important; box-shadow: none !important; padding: 0 !important;
}
.st-key-fl_signin_card [data-testid="stTextInputRootElement"] {
    background: #131F35 !important; border: 1px solid #334155 !important;
    border-radius: 8px !important; box-shadow: none !important;
}
.st-key-fl_signin_card input {
    background: transparent !important; font-family: 'JetBrains Mono', monospace !important;
    font-size: 12.5px !important; color: #E2E8F0 !important; -webkit-text-fill-color: #E2E8F0 !important;
}
.st-key-fl_signin_card label p { font-size: 11px !important; font-weight: 600 !important; color: #94A3B8 !important; }
.st-key-fl_signin_card [data-testid="stAlertContainer"],
.st-key-fl_signin_card .stAlertContainer {
    background: rgba(96,165,250,.09) !important; border: 1px solid rgba(96,165,250,.25) !important;
    border-radius: 9px !important;
}
.st-key-fl_signin_card [data-testid^="stAlertContent"] p,
.st-key-fl_signin_card [data-testid^="stAlertContent"] {
    font-size: 11px !important; color: #BFDBFE !important;
}
.st-key-fl_signin_card [data-testid="stAlertContainer"] svg { color: #60A5FA !important; }
</style>
"""


def inject_global_css():
    """Idempotent - safe to call multiple times per run (login screen +
    main app both call it); Streamlit just renders the same <style> tag
    again, which browsers dedupe with no visible effect."""
    st.markdown(_CSS, unsafe_allow_html=True)
