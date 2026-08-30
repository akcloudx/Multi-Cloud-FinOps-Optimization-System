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

/* st.dataframe's OUTER wrapper only - confirmed real via this Streamlit
   build's own JS bundle (DataFrame.Wny8mRut.js: data-testid="stDataFrame").
   The grid's actual cells/rows/borders are drawn on a <canvas>
   (glide-data-grid) and CSS can't reach inside that at all - this rounds
   just the wrapper so tables read as a card, matching the rest of the
   app's card language, app-wide rather than one table at a time. */
div[data-testid="stDataFrame"] {
    border-radius: 12px;
    overflow: hidden;
}

/* App runs layout="wide" with no cap, so st.columns() always splits the
   full browser width evenly - on an ultrawide monitor that stretches
   short metric labels/values across huge gaps instead of just showing
   more content. Capping+centering the real main container (confirmed via
   the installed Streamlit 1.60 bundle: stMainBlockContainer carries both
   this data-testid and the "block-container" class) fixes it app-wide
   rather than patching every page's column layout individually. Wider
   than the 1200px login-page cap since this holds tables/charts, not
   marketing copy. */
[data-testid="stMainBlockContainer"] {
    max-width: 1600px;
    margin-left: auto;
    margin-right: auto;
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
/* More breathing room between Home/Tenant Management/User Management
   (2026-08-30, real feedback: "very close tightened to each other") -
   the individual nav links themselves, not the outer nav container, so
   this only adds gaps BETWEEN items, not around the whole nav block. */
[data-testid="stSidebarNavLink"] {
    margin-bottom: 6px !important;
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

/* RI Coverage tab's orphaned-RI-drain alert (2026-08-29, real feedback:
   the default st.error() red/maroon box - even after the app-wide
   saturate/brightness toning above - still read as a generic default
   alert, inconsistent with this tab's own badge/card visual language
   right above it). Scoped to this one container only (same "st-key-*"
   pattern as the signin card above) - re-skinned to this app's own
   dark-card tokens (#131F35/#1E293B, the same ones .fl-previewcard/
   .fl-fcard already use elsewhere) with this app's own established
   "alert" red (#F87171, see .fl-pstat.alert above) instead of
   Streamlit's built-in alert coloring. filter:none overrides the
   app-wide desaturation rule above - these colors are chosen
   deliberately here, not Streamlit's default that needed toning down. */
.st-key-fl_ri_drain_alert [data-testid="stAlertContainer"],
.st-key-fl_ri_drain_alert .stAlertContainer {
    background: rgba(248,113,113,.08) !important; border: 1px solid rgba(248,113,113,.3) !important;
    border-radius: 12px !important; filter: none !important;
}
.st-key-fl_ri_drain_alert [data-testid^="stAlertContent"] p,
.st-key-fl_ri_drain_alert [data-testid^="stAlertContent"] {
    color: #FCA5A5 !important;
}
.st-key-fl_ri_drain_alert [data-testid="stAlertContainer"] svg { color: #F87171 !important; }

/* Home page's 5-metric KPI row (2026-08-29, real feedback across 3
   rounds): st.metric doesn't wrap either its value or its label by
   default (confirmed real via this Streamlit build's own
   Metric.CmkuJai4.js - stMetricValue/stMetricLabel are real, stable
   data-testids) - a long value (INR figures like "₹5,068.89/hr" are
   notably longer than their USD equivalent) or a long label
   ("Critical Recommendations") ellipsis-clips instead on a narrower
   (laptop-width) screen. Two rounds of column-width tuning (wider $
   columns, then two separate rows) each fixed one symptom while
   breaking something else or reading as visually disjointed - letting
   the text wrap onto a second line instead needs no column-width
   guessing at all, and keeps the original clean single-row-of-5 layout
   the user actually wanted kept. */
.st-key-fl_home_kpis [data-testid="stMetricValue"],
.st-key-fl_home_kpis [data-testid="stMetricLabel"] {
    white-space: normal !important;
    overflow-wrap: break-word !important;
    text-overflow: unset !important;
    overflow: visible !important;
}


/* Savings Plan pool flow (2026-08-28) - approved via mockup first. Round 2
   (same day): numbered circle badges on each step header were rejected as
   UI clutter (real feedback) - removed, plain headings only. Reused for
   all 3 pools (Compute/Database/SageMaker) since _render_sp_pool_economics
   is shared. */
.spflow-cardhead { font-size: 12.5px; color: #94A3B8; margin-bottom: 12px; }
.spflow-cardhead b { color: #F1F5F9; }
/* Wraps the "By resource type" rows so the WHOLE block shrinks to fit its
   own widest row's real content instead of stretching to the expander's
   full width - real feedback, 2026-08-30: fixing the "Every resource"
   table's own same over-stretching problem (below) made this section's
   identical issue stand out even more starkly, sitting content-tight next
   to it. display:inline-block + width:fit-content on the PARENT (not each
   row) so every row still shares one consistent width and the border-
   bottom dividers line up - a per-row fit-content would make each row a
   different width based on its own name length, misaligning the borders
   between rows instead of fixing anything. */
.spflow-list { display: inline-block; width: fit-content; max-width: 100%; }
.spflow-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid #1E293B; font-size: 12.5px; }
.spflow-row:last-child { border-bottom: none; }
.spflow-rowname { flex: 1; }
/* text-align: right added 2026-08-30 - real inconsistency caught live:
   this and .spflow-rowrate are the row's two numeric-ish columns sitting
   right next to each other, but only rate was right-aligned - count
   defaulted to left, so "5 resources"/"1 resource" started flush-left
   while "$0.6280/hr" ended flush-right, breaking the clean vertical scan
   two adjacent numeric columns should have. */
.spflow-rowcount { color: #94A3B8 !important; width: 90px; text-align: right; }
.spflow-rowrate { width: 90px; text-align: right; }
.spflow-econrow { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 14px; }
.spflow-econlabel { font-size: 10.5px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: #526279; }
.spflow-econval { font-size: 15px; font-weight: 700; }
.spflow-bridgetrack { height: 10px; border-radius: 999px; background: #0F1A2E; overflow: hidden; display: flex; margin-bottom: 8px; }
.spflow-bridge-committed { background: #60A5FA; }
.spflow-bridge-recommend { background: rgba(52,211,153,.5); }
.spflow-legend { display: flex; gap: 16px; font-size: 11px; color: #94A3B8; flex-wrap: wrap; }
.spflow-legend span { display: inline-flex; align-items: center; gap: 6px; }
.spflow-dot { width: 7px; height: 7px; border-radius: 2px; flex-shrink: 0; }
.spflow-planrow { display: flex; align-items: center; gap: 14px; padding: 8px 0; font-size: 12.5px; flex-wrap: wrap; }
.spflow-planid { font-family: 'JetBrains Mono', monospace; color: #F1F5F9; }
.spflow-plandetail { color: #94A3B8; flex: 1; }
.spflow-planrate { color: #F1F5F9; font-weight: 600; }

/* Recommendations tab redesign (2026-08-30) - replaced a 5-card list that
   mostly duplicated RI Coverage/Savings Plan Analysis's own headlines
   (often with a WORSE $0.0 figure where those tabs already had real
   pricing - see _render_recommendations_tab()'s own comment) with a
   single combined savings projection - the one number neither tab can
   answer alone. Approved via an HTML mockup (Artifact) first; these
   classes are ported near-verbatim from that mockup (same colors,
   spacing, gradients) rather than approximated with native st.metric/
   st.container, specifically to keep the built page close to what was
   actually approved - real feedback that previous ports had drifted
   further from their mockups than necessary. */
.rec-headline-card {
    background: linear-gradient(160deg, #101A2E 0%, #0D2A22 130%);
    border: 1px solid rgba(52,211,153,.28); border-radius: 16px; padding: 26px 28px; margin-bottom: 20px;
}
.rec-headline-eyebrow { font-size: 11.5px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: #34D399; margin-bottom: 10px; }
/* No max-width here (the mockup had 620px, scoped to its own 800px-wide
   preview container) - real feedback, 2026-08-30: ported literally into
   the real app, where this card spans much more width, that same cap
   forced an unnecessarily cramped wrap. Lets the sentence use the card's
   own real width instead. */
.rec-headline-sentence { font-size: 22px; font-weight: 700; line-height: 1.35; }
.rec-headline-sentence b { color: #34D399; }
.rec-headline-sub { font-size: 12.5px; color: #94A3B8; margin-top: 10px; }
/* Warning tone (2026-08-30, Savings Plan Analysis's own headline card) -
   that card has a real "you're over-committed" state, unlike
   Recommendations' headline which was always good news (green). Same
   red this app already uses for a real problem elsewhere (Rightsizing's
   Overutilized, Maturity's Below Crawl). */
.rec-headline-card.warn { background: linear-gradient(160deg, #101A2E 0%, #2A1414 130%); border-color: rgba(248,113,113,.3); }
.rec-headline-card.warn .rec-headline-eyebrow { color: #F87171; }
.rec-headline-card.warn .rec-headline-sentence b { color: #F87171; }

.rec-metric-card { background: #101A2E; border: 1px solid #1E293B; border-radius: 12px; padding: 16px 18px; height: 100%; }
.rec-metric-card .lbl { font-size: 10.5px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: #526279; margin-bottom: 8px; }
.rec-metric-card .val { font-size: 19px; font-weight: 700; }
.rec-metric-card .val .unit { font-size: 11px; color: #94A3B8; font-weight: 600; }
.rec-metric-card .sub { font-size: 11px; color: #94A3B8; margin-top: 4px; }
.rec-metric-card.combined { border-color: rgba(96,165,250,.35); background: linear-gradient(160deg, #101A2E, #0F1E33); }
.rec-metric-card.combined .val { color: #60A5FA; }
/* Rightsizing tab's KPI row (2026-08-30) - same card family, tenant-
   verdict-colored (amber/red/green) rather than the neutral/blue tones
   above, matching this tab's own existing Classification-column palette
   (Overutilized=red/critical, Underutilized=amber/wasting money,
   savings=green) rather than inventing a new one. */
.rec-metric-card.under { border-color: rgba(251,191,36,.3); }
.rec-metric-card.under .val { color: #FBBF24; }
.rec-metric-card.over { border-color: rgba(248,113,113,.3); }
.rec-metric-card.over .val { color: #F87171; }
.rec-metric-card.savings { border-color: rgba(52,211,153,.3); background: linear-gradient(160deg, #101A2E, #0D2A22); }
.rec-metric-card.savings .val { color: #34D399; }
/* RI Coverage's "Not RI-Eligible" card (2026-08-30) - a genuinely neutral
   state (no Reservation product exists for this service), not good news
   or a problem, so it gets this app's existing gray token (already used
   for volume-based "—" placeholders elsewhere) rather than reusing under/
   over/savings' emotionally-loaded colors. */
.rec-metric-card.neutral .val { color: #64748B; }

/* Rightsizing Settings popover (2026-08-30) - groups 8 previously-flat
   number inputs into labeled sections (Data Window/CPU/Memory/Safety),
   approved via mockup. Real st.selectbox/st.number_input widgets on
   either side, unchanged - this is only a section-header divider between
   them. First group header sits right after the Preset field (no
   margin-top needed there), later ones get breathing room above. */
.rs-group-head {
    font-size: 10px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: #60A5FA;
    display: flex; align-items: center; gap: 6px; margin: 14px 0 6px;
    padding-bottom: 6px; border-bottom: 1px solid rgba(96,165,250,.15);
}

/* Grouped by destination tab, not one row per recommendation - real
   feedback, 2026-08-30: RI Coverage owns most categories, so a flat row
   list repeated "→ RI Coverage" 3-4 times with most of each row empty.
   One card per destination tab, items listed compactly inside instead. */
.rec-group-card {
    background: #101A2E; border: 1px solid #1E293B; border-radius: 10px; padding: 14px 16px; margin-bottom: 10px;
}
.rec-group-head { font-size: 12px; font-weight: 700; color: #60A5FA; margin-bottom: 8px; }
.rec-group-item {
    display: flex; align-items: center; gap: 10px; padding: 7px 0; font-size: 13px; color: #F1F5F9;
    border-top: 1px solid rgba(30,41,59,.6);
}
.rec-group-item:first-of-type { border-top: none; }
.rec-group-item .dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.rec-group-item .dot.hi { background: #F87171; }
.rec-group-item .dot.med { background: #FBBF24; }

/* Inventory tab's headline strip (2026-08-30) - visual polish only, real
   feedback to match Recommendations' look. Same gradient-card language as
   .rec-headline-card, but a horizontal stats row instead of a sentence -
   this tab's "at a glance" moment is 4 numbers (total spend, resource
   count, running, stopped), not a single answer to lead with. Approved
   via mockup first. */
.inv-headline {
    background: linear-gradient(160deg, #101A2E 0%, #17213A 130%);
    border: 1px solid rgba(96,165,250,.25); border-radius: 14px; padding: 18px 22px;
    display: flex; align-items: center; gap: 28px; flex-wrap: wrap; margin-bottom: 16px;
}
.inv-stat { display: flex; flex-direction: column; }
.inv-stat .n { font-size: 22px; font-weight: 700; }
.inv-stat .lbl { font-size: 10.5px; color: #526279; text-transform: uppercase; letter-spacing: .04em; margin-top: 2px; }
.inv-stat.total .n { color: #60A5FA; }
.inv-divider { width: 1px; height: 34px; background: #1E293B; }

/* Sidebar "Log out" / "Switch mode" pseudo-buttons (ui/auth_page.py) - a
   plain <a href>, not st.link_button, which the installed LinkButton.*.js
   bundle confirms hardcodes target="_blank" with no override (every click
   opened a second tab - real user feedback, 2026-08-30). A bare anchor has
   no target attribute, so browsers default to _self (same tab) while still
   being a genuine navigation (needed to clear st.navigation()'s sidebar
   menu, which nothing short of a real page load resets - see
   require_login()'s own comment). Colors hand-matched to a default
   Streamlit secondary button against this app's dark sidebar theme
   (.streamlit/config.toml [theme.dark.sidebar]) since a raw anchor gets
   none of st.link_button's automatic theme-matched styling - same
   dark-only-not-light-aware tradeoff this file already makes for the
   signin card/drain alert above, not a new gap. */
.fl-sidebar-linkbtn {
    display: flex; align-items: center; justify-content: center;
    width: 100%; box-sizing: border-box;
    padding: 0.5rem 1rem; margin: 0.25rem 0 0.75rem;
    background: #1E293B; border: 1px solid #334155; border-radius: 8px;
    color: #F1F5F9 !important; text-decoration: none !important;
    font-size: 14px; font-weight: 400; line-height: 1.6; cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease;
}
.fl-sidebar-linkbtn:hover { background: #26364D; border-color: #45566E; }
.fl-sidebar-linkbtn:visited { color: #F1F5F9 !important; }
.fl-sidebar-linkbtn svg { flex-shrink: 0; opacity: .85; }

/* Sidebar "rail" layout (2026-08-30, real feedback pass: "some new
   visuals" for the 3 config sections, which had read as repetitive
   stacked boxes in the prior bordered-card round). No boxes at all now -
   a thin vertical gradient spine connects a colored icon node per
   section, content sits indented to its right. Each section keeps its
   own accent color (Cloud=blue, Display=green, Account=violet) so the
   3 read as distinct without needing a border. Mocked up via Artifact
   and approved before porting - inline SVG icons (Feather, MIT licensed),
   see sidebar_icon() below.

   Known limitations, disclosed rather than faked/left for the user to
   discover live (this project's standing rule is no live browser testing
   on my end, so anything not verifiable was simplified rather than
   guessed): the mockup's absolute-positioned icon node on a continuous
   gradient spine required precise cross-container CSS this app's real
   Streamlit container boundaries make fragile to get right blind - ported
   as a plain flex row (st.columns, icon column + content column) instead,
   real Streamlit layout primitives rather than absolute positioning, at
   the cost of the literal connecting line between sections. Separately,
   the top nav (Home/Tenant Management/User Management) is Streamlit's own
   native st.navigation() widget, a DOM region this app can style but
   can't inject custom rail-node HTML into - its own spacing fix is
   separate, see [data-testid="stSidebarNavLink"] above. */
.fl-rail-node {
    width: 32px; height: 32px; border-radius: 50%; margin-top: 2px;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 0 0 1px currentColor;
}
.fl-rail-node.cloud   { background: rgba(96,165,250,.14); color: #60A5FA; }
.fl-rail-node.display { background: rgba(52,211,153,.14); color: #34D399; }
.fl-rail-node.account { background: rgba(167,139,250,.14); color: #A78BFA; }
.fl-rail-title { font-size: 13.5px; font-weight: 700; color: #F1F5F9; padding-top: 6px; margin-bottom: 4px; }
.fl-rail-env-line { font-size: 12px; color: #94A3B8; margin: 10px 0 12px; line-height: 1.5; }
.fl-rail-env-line b { color: #F1F5F9; font-weight: 600; }
</style>
"""

# Feather Icons (MIT licensed) path data, inlined - see the .fl-rail-node
# comment above for why this isn't a webfont. currentColor + no fixed fill
# so each icon inherits whatever text color it's placed in.
SIDEBAR_ICONS = {
    "cloud":  '<path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/>',
    "card":   '<rect x="1" y="4" width="22" height="16" rx="2" ry="2"/><line x1="1" y1="10" x2="23" y2="10"/>',
    "lock":   '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "swap":   '<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
}


def sidebar_icon(name: str, size: int = 15) -> str:
    """Inline <svg> string for the sidebar's icon set (see SIDEBAR_ICONS) -
    embed directly into any unsafe_allow_html markdown/HTML string."""
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{SIDEBAR_ICONS[name]}</svg>'
    )


def inject_global_css():
    """Idempotent - safe to call multiple times per run (login screen +
    main app both call it); Streamlit just renders the same <style> tag
    again, which browsers dedupe with no visible effect."""
    st.markdown(_CSS, unsafe_allow_html=True)
