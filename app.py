"""
app.py — Multi-Cloud FinOps Optimization System (Azure & AWS)
Enterprise Cloud Cost & Commitment Engine

Left sidebar (Manage): Tenants, User Management.
Left sidebar (Workspace): Analyze — a single page whose top tabs are
  Inventory | Rightsizing | Savings Plan Analysis | RI Coverage |
  Recommendations | Maturity Assessment.
"""

import sys, os, glob, html, re
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.getcwd())

# Add local vendorized Azure SDK packages to sys.path as a Linux-only fallback
# (the bundle ships Linux .so binaries and must never shadow a real pip install,
# especially not on Windows where those binaries can't even load)
vendor_dir = os.path.join(_PROJECT_ROOT, "azure_sdk_vendor")
if sys.platform.startswith("linux") and os.path.exists(vendor_dir) and vendor_dir not in sys.path:
    sys.path.append(vendor_dir)

# Auto-discover Azure App Service virtualenv site-packages
for ant_path in [
    "/home/site/wwwroot/antenv/lib/python3.12/site-packages",
    "/home/site/wwwroot/antenv/lib/python3.11/site-packages",
    "/home/site/wwwroot/antenv/lib/python3.10/site-packages",
    "/home/site/wwwroot/.python_packages/lib/site-packages",
]:
    if os.path.exists(ant_path) and ant_path not in sys.path:
        sys.path.insert(0, ant_path)

for site_pkg in glob.glob("/home/site/wwwroot/antenv/lib/python*/site-packages"):
    if os.path.exists(site_pkg) and site_pkg not in sys.path:
        sys.path.insert(0, site_pkg)

import json
import streamlit as st
import pandas as pd

# Detect initial currency from query parameters to persist across browser refresh
if "currency" in st.query_params:
    initial_currency = st.query_params["currency"]
    if initial_currency not in ["USD", "INR"]:
        initial_currency = "USD"
else:
    initial_currency = "USD"

# _pending_currency takes priority over the query string, same reasoning as
# _pending_provider below - real bug found 2026-08-30 (a temporary debug
# caption on the sidebar confirmed it directly): the Home page's "Dash"
# button calls st.switch_page(analyze_page), which clears st.query_params
# AND resets the currency widget's own session_state entry on its way to
# the destination page - "reassert st.query_params every rerun" (the fix
# that closes this same gap for a plain sidebar-link click) never applied
# here, since switch_page() doesn't carry that rerun's query params
# forward into the next page's script execution at all. Set right before
# switch_page, consumed exactly once here, one rerun later, before the
# widget ever reads a default - see that button's own comment.
if "_pending_currency" in st.session_state:
    initial_currency = st.session_state.pop("_pending_currency")

# Cloud Platform (Azure/AWS) had NO persistence at all before this - a
# hardcoded default="Azure" and nothing reading/writing query params for it,
# so it reverted to Azure on every single refresh regardless of what was
# selected (real bug reported live 2026-08-20, alongside currency - this one
# was unconditionally broken, not the same subtler "works until you click a
# sidebar link" bug currency/the login token had). Same fix pattern as
# currency: read the initial value from the query string here, re-assert it
# into query_params on every rerun down in the sidebar section below.
#
# _pending_provider takes priority over the query string - confirmed live
# 2026-08-21 (screen recording) that st.switch_page() (used by the "Dashboard"
# button in Tenant Management) clears st.query_params on its way to the
# destination page, AND the Cloud Platform segmented_control's own widget
# state is treated as a fresh instantiation on that same landing, so its
# `default=` argument wins - meaning re-asserting st.query_params["provider"]
# right before calling switch_page (the same fix that worked for the sidebar
# nav-link/session-token bug) does NOT survive switch_page specifically, and
# the app silently fell back to Azure regardless of which tenant's Dashboard
# button was actually clicked. A plain (non-widget) session_state key isn't
# subject to either limitation - it's set right before switch_page and
# consumed exactly once here, one rerun later, before the widget ever reads
# a default.
if "_pending_provider" in st.session_state:
    initial_provider = st.session_state.pop("_pending_provider")
elif "provider" in st.query_params:
    initial_provider = st.query_params["provider"]
    if initial_provider not in ["Azure", "AWS"]:
        initial_provider = "Azure"
else:
    initial_provider = "Azure"

st.set_page_config(
    page_title="FinOps Optimization System",
    page_icon=":material/bolt:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialize Databases (Cached for instant reloads)
from db.schema import init_db, get_engine
from db.seed import seed_if_empty, COMPUTE_SP_ELIGIBLE_TYPES, DATABASE_SP_ELIGIBLE_TYPES
from db.aws_seed import seed_aws_if_empty, AWS_COMPUTE_SP_TYPES, AWS_DATABASE_SP_TYPES, AWS_SAGEMAKER_SP_TYPES

@st.cache_resource(show_spinner="Preparing your workspace...")
def init_all_databases():
    # Only the demo scope is eagerly seeded here - the live scope for each
    # provider initializes lazily on first real use (tenant registration,
    # first sync) via db/tenants.py and data/sync_pipeline.py, both of which
    # already call init_db() inline before every operation.
    try:
        init_db("Azure", "demo")
        seed_if_empty()
        init_db("AWS", "demo")
        seed_aws_if_empty()
    except Exception as e:
        print(f"[Warning] Database initialization error: {e}")

try:
    init_all_databases()
except Exception as _db_err:
    print(f"[Warning] Failed to initialize databases on startup: {_db_err}")

# Auth gate — must run before anything else renders. Halts here (st.stop())
# until someone is logged in; see ui/auth_page.py for why this is deliberately
# minimal (placeholder ahead of real Entra ID / OIDC login).
from ui.auth_page import require_login, render_logout_control, render_switch_mode_control, render_change_password_control
current_user = require_login()

# require_login() only injects CSS on the pre-auth screens (it returns early
# once already authenticated) - apply it here too so the setup gate and main
# dashboard get the same card/button polish.
from ui.styling import inject_global_css, sidebar_icon
inject_global_css()

# Imports
from data.inventory_loader import get_compute_inventory
from data.sync_pipeline import run_ingestion_pipeline
from pricing.retail_pricing import usd, fmt_currency, get_inr_rate
from pricing.commitment_pricing import get_commitment_prices, MONTH_HOURS
from pricing.azure_vm_flexibility import get_vm_flexibility_groups
from pricing.cache_admin import clear_pricing_caches
from pricing.unpriced_by_design import unpriced_by_design_reason
from commitments.existing_commitments import (
    get_existing_savings_plans,
    get_existing_reservations,
    get_compute_savings_plans,
    get_database_savings_plans,
    get_sagemaker_savings_plans,
)
from analysis.engine import (
    run_waterfall,
    savings_plan_analysis,
    reservation_analysis,
    generate_recommendations,
    compute_orphaned_status,
    DEFAULT_SAFETY_BUFFER,
    DEFAULT_SIMULATE_DAYS,
)
from analysis.sp_eligibility import check_sp_eligibility
from analysis.ri_eligibility import check_eligibility
from pricing.sku_mapping import resolve_sku_query
from analysis.focus_mapping import to_focus_view, map_service_category, FOCUS_COLUMN_DEFINITIONS, FOCUS_SPEC_VERSION, FOCUS_SPEC_URL
from analysis.maturity import run_maturity_assessment
from analysis.commitment_economics import (
    savings_plan_term_comparison, aws_savings_plan_term_comparison, ri_gap_pricing, combined_monthly_savings, TERM_LABELS,
    term_row_exists,
)
from db.tenants import (
    list_tenants, get_active_tenant, get_tenant_credentials, upsert_tenant,
    update_tenant_name, touch_last_synced, set_active_tenant, delete_tenant,
    resource_count, list_subscriptions, upsert_subscription,
    update_tenant_permission_status, record_sync_result, update_sync_interval,
    update_aws_account_id, update_rightsizing_settings,
)
# Moved to module level, 2026-08-30 - list_users/create_user were previously
# imported locally inside page_users() only; update_user/update_user_password/
# set_user_active need to be reachable from _manage_user_dialog() too (a
# separate top-level function, not nested inside page_users()), same as
# every db.tenants function above is already reachable from both
# page_tenant_management() and _manage_tenant_dialog().
from db.users import list_users, create_user, update_user, update_user_password, set_user_active, delete_user
from analysis.rightsizing import (
    classify_vm_utilization, get_rightsizing_settings, suggest_target_instance_type,
    estimate_resize_monthly_impact, PRESETS, SETTINGS_FIELDS,
)
from azure_conn.connector import (
    AzureCredentials, save_credentials_to_env_file,
    test_connection, check_role_assignments, check_tenant_role_assignments,
    list_accessible_subscriptions, status_from_role_check,
    REQUIRED_ROLES, REQUIRED_SUBSCRIPTION_ROLES, REQUIRED_TENANT_ROLES,
    HAS_AZURE_IDENTITY,
)
from aws.connector import (
    AWSCredentials, save_aws_credentials_to_env_file,
    test_aws_connection, check_aws_permissions, REQUIRED_AWS_POLICIES, HAS_BOTO3,
)

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"), override=False)
except ImportError:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# SHARED AZURE TENANT CONNECT/LIST WIDGETS
# Used by both the post-login Setup gate and the Home page - extracted once
# here instead of duplicated, since both need the exact same "enter Service
# Principal creds -> test/connect -> ingest" flow.
# ─────────────────────────────────────────────────────────────────────────────
def _render_azure_sp_setup_guide(expanded: bool = False):
    """Shared with _manage_tenant_dialog()'s Credentials tab - extracted
    2026-08-30, real onboarding gap the user caught live: this guidance
    (how to create a Service Principal, minimum roles to get Inventory/Cost
    data working, tenant-wide roles as an optional add-later step) used to
    exist ONLY inside Manage Tenant's Credentials tab - reachable only for a
    tenant that's already connected. A brand-new user in a fresh environment
    has no tenant yet, so no way to ever reach that guidance from the Add a
    new tenant form where they actually need it first."""
    with st.expander("How to set up a Service Principal", icon=":material/menu_book:", expanded=expanded):
        st.markdown('<span class="fl-setup-num">1</span><span class="fl-setup-head">Create the Service Principal</span>', unsafe_allow_html=True)
        st.markdown('<div class="fl-setup-desc">Run this once - it creates the identity this app authenticates as.</div>', unsafe_allow_html=True)
        st.code('az ad sp create-for-rbac --name "finops-optimizer-sp" --role "Reader" --scopes /subscriptions/<SUBSCRIPTION_ID> --output json', language="bash")

        st.markdown('<span class="fl-setup-num">2</span><span class="fl-setup-head">Assign subscription-level roles</span>', unsafe_allow_html=True)
        st.markdown('<div class="fl-setup-desc"><b>Reader</b> + <b>Cost Management Reader</b> for each subscription this tenant should cover - this is the minimum needed for Inventory and Cost data.</div>', unsafe_allow_html=True)
        st.code(
            'az role assignment create --assignee <CLIENT_ID> --role "Reader" --scope /subscriptions/<SUB_ID>\n'
            'az role assignment create --assignee <CLIENT_ID> --role "Cost Management Reader" --scope /subscriptions/<SUB_ID>',
            language="bash",
        )
        st.markdown('<div class="fl-setup-desc">To cover every subscription in the tenant at once instead of one at a time:</div>', unsafe_allow_html=True)
        st.code(
            'for sub in $(az account list --query "[].id" -o tsv); do\n'
            '  az role assignment create --assignee <CLIENT_ID> --role "Reader" --scope "/subscriptions/$sub"\n'
            '  az role assignment create --assignee <CLIENT_ID> --role "Cost Management Reader" --scope "/subscriptions/$sub"\n'
            'done',
            language="bash",
        )

        st.markdown(
            '<span class="fl-setup-num">3</span><span class="fl-setup-head">Assign tenant-level roles'
            '<span class="fl-badge-optional">Optional - add later</span></span>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="fl-setup-desc">For Reservations and Savings Plans - a separate permission system from step 2, '
            'since neither is a subscription resource. Needs <b>User Access Administrator</b> at the tenant level, a '
            "materially higher bar many student/trial accounts can't get. <b>Not required to get started</b> - "
            'inventory and cost data sync independently of this; add it any time from Manage &gt; Tenant-wide permissions.</div>',
            unsafe_allow_html=True,
        )
        st.code(
            'az role assignment create --assignee <CLIENT_ID> --role "Reservations Reader" --scope "/providers/Microsoft.Capacity"\n'
            'az role assignment create --assignee <CLIENT_ID> --role "Savings Plan Reader" --scope "/providers/Microsoft.BillingBenefits"',
            language="bash",
        )
        st.markdown(
            '<div class="fl-setup-callout">✅ <div>Missing step 3 is <b>not fatal</b> - a sync will report which parts succeeded.</div></div>',
            unsafe_allow_html=True,
        )


def _render_help_page_link(label: str):
    """Links to Help & Support in a NEW browser tab, not st.page_link's
    normal same-tab in-app navigation - real feedback, 2026-08-30: Azure and
    AWS's own consoles open their help/docs links in a new tab specifically
    so you don't lose your place (a half-filled Add tenant form). st.page_link
    has no target="_blank" option (confirmed against its real signature) -
    it always navigates the current tab, since it's Streamlit's own in-app
    routing, not a plain hyperlink. Built as a real <a target="_blank"> to
    this app's own URL instead. Preserves the session token
    (st.query_params["s"], see ui/auth_page.py's _start_session/
    require_login) in that URL so the NEW tab is already signed in rather
    than landing back on the login screen - confirmed that token is exactly
    what a fresh page load/tab already uses to restore a session, so this
    is the same mechanism, just reused deliberately.
    help_page is a module-level global (st.Page(page_help, ...), NAVIGATION
    section further down) - safe to reference here via the same late-binding
    closure pattern this whole file already relies on, since this only runs
    once pg.run() reaches a page body, well after that assignment executes."""
    _token = st.query_params.get("s")
    _url = f"/{help_page.url_path}" + (f"?s={_token}" if _token else "")
    st.markdown(
        f'<a href="{_url}" target="_blank" rel="noopener noreferrer">'
        f'{label} ↗</a>',
        unsafe_allow_html=True,
    )


def _render_azure_connect_form(key_prefix: str, mode: str = "live"):
    """Renders the credential form + Test/Connect buttons and handles both
    actions. Returns True if a connect just succeeded (caller may want to
    st.rerun() immediately rather than wait for the natural rerun)."""
    # Real bugs caught live, 2026-08-30, both about this form pre-filling
    # itself with a PREVIOUS real tenant's data instead of starting blank:
    # (1) az_name had a hardcoded value="Prod Azure Tenant" (a leftover
    #     example value passed as `value=` instead of `placeholder=` -
    #     confirmed live, it showed up verbatim on a fresh Add form for the
    #     user's own real tenant name); (2) the other 4 fields read
    #     load_credentials_from_env(), which reflects whatever tenant was
    #     LAST connected (save_credentials_to_env_file() writes it on every
    #     successful connect, data/sync_pipeline.py's own fallback path is
    #     the only real remaining consumer) - fine as a single-tenant dev
    #     convenience, wrong for "Add a NEW tenant" in a real multi-tenant
    #     flow, where it silently pre-filled a second tenant's form with
    #     the first tenant's real Tenant/Client/Subscription IDs. This form
    #     now always starts genuinely blank - simplest fix, and correct: a
    #     brand-new tenant's real values were never going to happen to
    #     match whatever was last connected anyway.
    #
    # Second real gap in the same round: the placeholder examples for
    # Tenant ID/Client ID/Subscription ID were the user's own REAL GUIDs
    # (confirmed against their actual "Prod Azure Tenant" values) hardcoded
    # into source as if they were generic examples - replaced with clearly
    # fabricated example GUIDs instead.
    # Real feedback, 2026-08-30: this form used to show BOTH the full
    # inline "How to set up a Service Principal" guide AND a link to the
    # same guide on the Help & Support page right next to each other - two
    # things doing the same job. The full guide now lives in one place
    # only (Help & Support, plus the Manage Tenant dialog's own Credentials
    # tab for an already-connected tenant); this form just links out to it.
    _render_help_page_link("New here? See the Getting Started guide")

    with st.form(f"{key_prefix}_azure_form"):
        col1, col2 = st.columns(2)
        with col1:
            az_name   = st.text_input("Tenant / Subscription Name", value="", placeholder="e.g. Main Production Azure", key=f"{key_prefix}_az_name")
            az_tenant = st.text_input("Tenant ID", value="", placeholder="a1b2c3d4-5e6f-4a1b-9c2d-3e4f5a6b7c8d", key=f"{key_prefix}_az_tenant")
            az_client = st.text_input("Client ID (Application ID)", value="", placeholder="b2c3d4e5-6f7a-4b2c-8d3e-4f5a6b7c8d9e", key=f"{key_prefix}_az_client")
            az_domain = st.text_input("Domain (optional)", value="", placeholder="contoso.onmicrosoft.com", key=f"{key_prefix}_az_domain")
        with col2:
            az_sub = st.text_input("Subscription ID", value="", placeholder="c3d4e5f6-7a8b-4c3d-9e4f-5a6b7c8d9e0f", key=f"{key_prefix}_az_sub")
            az_sec = st.text_input("Client Secret", value="", type="password", key=f"{key_prefix}_az_sec", autocomplete="new-password")

        c_btn1, c_btn2 = st.columns(2)
        with c_btn1:
            az_test_btn = st.form_submit_button("🧪 Test Access Permissions", use_container_width=True)
        with c_btn2:
            az_sub_btn = st.form_submit_button("⚡ Connect, Save & Ingest Live Tenant Data", type="primary", use_container_width=True)

    if az_test_btn:
        new_az = AzureCredentials(az_tenant, az_sub, az_client, az_sec)
        if new_az.is_complete:
            with st.spinner("Auditing Tenant & Subscription RBAC permissions..."):
                test_res = test_connection(new_az)
                # Real bug fixed 2026-08-30, caught live on a production
                # deployment: this button only ever ran test_connection(),
                # which only checks SUBSCRIPTION-scoped roles (Reader/Cost
                # Management Reader) via check_role_assignments() - it never
                # called check_tenant_role_assignments() at all, so the
                # TENANT-wide Reservations Reader/Savings Plan Reader roles
                # were never actually verified here, even though "Permission
                # Audit Passed!" implied everything was checked. Worse: the
                # "Verified Roles" caption below used to print
                # test_res["roles_verified"] directly - check_role_
                # assignments() returns the Service Principal's FULL raw
                # assigned-role list there (every real role it happens to
                # have), not just the ones this app actually requires, so a
                # tenant with extra roles like "Billing Reader"/"Reservation
                # Purchaser" saw those listed as if "verified" - genuinely
                # misleading, since "Reservation Purchaser" is a real but
                # completely DIFFERENT Azure role from "Reservations
                # Reader" (the one actually required), easy to mistake for
                # coverage it doesn't provide. The Manage Tenant dialog
                # never had this bug (it always called both checks via
                # _run_tenant_permission_check/_render_role_checklist) -
                # this now matches that same accurate pattern instead of
                # its own separate, incomplete one.
                tenant_check = check_tenant_role_assignments(new_az)
                if test_res["success"]:
                    st.success(f"✅ **Permission Audit Passed!** {test_res['message']}")
                    st.info(f"📋 **Accessible Subscriptions in Tenant ({len(test_res['subscriptions'])}):** {', '.join(test_res['subscriptions'])}")
                    st.markdown("**Subscription-level roles** (Inventory & Cost data):")
                    _render_role_checklist(REQUIRED_SUBSCRIPTION_ROLES, "", ", ".join(test_res.get("roles_verified", [])))
                    st.markdown("**Tenant-level roles** (Reserved Instance / Savings Plan data):")
                    if tenant_check["checked"]:
                        _render_role_checklist(REQUIRED_TENANT_ROLES, "", ", ".join(tenant_check["assigned_roles"]))
                        if not tenant_check["ready"]:
                            st.caption(
                                "Missing one or more tenant-wide roles above - Inventory/Cost sync will still "
                                "work, but Reservation/Savings Plan data will fail to sync until these are granted."
                            )
                    else:
                        st.caption(f"Could not check tenant-level roles: {tenant_check.get('error') or 'unknown error'}")
                else:
                    st.error(f"❌ **Permission Test Failed:** {test_res['message']}")
        else:
            st.error("Please fill in all credential fields before testing permissions.")

    if az_sub_btn:
        new_az = AzureCredentials(az_tenant, az_sub, az_client, az_sec)
        if new_az.is_complete:
            save_credentials_to_env_file(new_az)
            with st.spinner("Testing connection & ingesting live inventory from Azure Tenant into Azure SQL DB..."):
                tenant_db_id = upsert_tenant(
                    provider="Azure", mode=mode,
                    tenant_name=az_name or "Azure Tenant",
                    tenant_id=az_tenant,
                    subscription_id=az_sub,
                    client_id=az_client,
                    client_secret=az_sec,
                    domain=az_domain or None,
                )
                # Connecting a tenant IS a sync, not just an inventory pull -
                # run_ingestion_pipeline() itself now also discovers/checks
                # subscriptions on every call (moved there from being a
                # UI-only action - real gap found live 2026-08: without it,
                # only a separate manual click ever populated
                # TenantSubscription, and the hourly cron never refreshed it
                # at all), so this one call is enough - no separate
                # subscription-sync call needed here anymore.
                res = run_ingestion_pipeline("Azure", creds=new_az, tenant_db_id=tenant_db_id)
                record_sync_result("Azure", mode, tenant_db_id, res["status"], res["message"])
                # PARTIAL treated as real progress, not a failure (2026-08-30,
                # real bug caught live: a tenant whose inventory genuinely
                # synced but hit a KNOWN, already-disclosed tenant-wide role
                # gap for Reservations/Savings Plan data was shown a scary
                # red error box identical to a true connection failure, and
                # the form stayed open even though the tenant was already
                # saved and real inventory was already synced - same
                # "PARTIAL is progress, not failure" principle
                # run_ingestion_pipeline() itself already applies to its own
                # status field, just not respected here before now. Still
                # returns True (closes the form) since there's nothing left
                # to retry from this form - the missing role has to be
                # granted in Azure, not fixed by re-submitting credentials.
                if res["status"] == "SUCCESS":
                    st.success(f"🎉 **Live Tenant Ingestion Complete!** {res['message']}")
                    return True
                elif res["status"] == "PARTIAL":
                    st.warning(f"⚠️ **Tenant connected, partial sync:** {res['message']}")
                    return True
                else:
                    st.error(f"❌ Connection or Ingestion Error: {res['message']}")
        else:
            st.error("Please fill in all Azure credential fields.")
    return False


# Post-login setup gate (a separate "connect a tenant before you can see
# anything" screen) removed 2026-08 - the Home page's tenant table + "Add a
# new tenant" button (both using the same _render_azure_connect_form above)
# now cover everything the gate did, so Production logins land directly on
# Home like Demo logins always have, instead of a redundant extra screen.


# ─────────────────────────────────────────────────────────────────────────────
# PAGE BODIES — each top-level function is registered as an st.Page() below
# and only executes once pg.run() reaches it, by which point every module-
# level global it reads (selected_provider, inv_raw, prices_df, ...) has
# already been computed further down this script (standard Python
# late-binding closures).
# ─────────────────────────────────────────────────────────────────────────────

def _finops_tag(domain: str, capability: str):
    """Traces a section back to its real FinOps Framework (finops.org/framework)
    Domain and Capability, so the mapping is explicit and citable rather than
    implied."""
    st.caption(f":material/explore: **FinOps Framework:** {domain} → *{capability}*")


def _render_top_header():
    """Persistent tenant identity/connection-status strip shown once above
    the Analyze tabs. Used to also carry a 6-metric grid (Running VMs/DBs,
    PAYG rates, Total SP Committed, Critical Recommendations) - dropped
    2026-08-28, real feedback: every one of those 6 numbers is already the
    specific headline subject of one of the other 6 tabs (Inventory owns
    the VM/DB counts, Savings Plan Analysis owns the PAYG rates and SP
    commitment, Recommendations owns the critical count), so showing all of
    them on every tab regardless of which one you're on was pure repetition
    rather than useful cross-tab orientation. What's left here - tenant
    identity and connection status - is genuinely cross-cutting, since no
    single tab's job is to say which tenant you're looking at."""
    st.markdown(f"## :material/cloud: {active_tenant.tenant_name}")

    if is_live_mode and not is_live_configured:
        st.warning(
            f"**Production Mode Active — No Connection Configured for {selected_provider}:** "
            f"Please go to the **:material/home: Home** page to configure your {selected_provider} credentials, "
            "or switch to **Demo Mode** in the sidebar to view sample data.",
            icon=":material/warning:",
        )
    elif is_live_mode and is_live_configured:
        st.success(
            f"**Connected to Production {selected_provider} API** — Tenant: **{active_tenant.tenant_name}** "
            f"({len(inv_raw)} resource{'s' if len(inv_raw) != 1 else ''} synced)",
            icon=":material/check_circle:",
        )
    st.divider()


# ═══════════════════════════════════════════════════════════════════════════════
# HOME — portfolio summary + tenant list (replaces the old standalone Tenants
# page; per-tenant editing/credentials/sync/permissions now live in the
# Manage Tenant dialog below instead of a flat connect-and-list page).
# ═══════════════════════════════════════════════════════════════════════════════
def _render_aws_iam_setup_guide(expanded: bool = False):
    """Shared with page_help()'s Getting Started guide - extracted 2026-08-30
    alongside the same fix for Azure (_render_azure_sp_setup_guide), for the
    same reason: this guidance previously only existed on the Add tenant
    form itself, with no way to see it before landing there."""
    # Real feedback, 2026-08-30: this rendered all of REQUIRED_AWS_POLICIES
    # (18 actions and counting, as more AWS services got inventory support
    # over time) as one long bulleted list, each line carrying a full
    # sentence of "Purpose" prose - "difficult to read", especially here.
    # What you actually DO is attach a much shorter list of distinct
    # managed policies (12 today) - leads with that as scannable chips,
    # calls out the handful of actions with no dedicated policy separately,
    # and pushes the full action-by-action justification into a collapsed
    # reference table below. Policy names/counts are all still computed
    # FROM REQUIRED_AWS_POLICIES, not hand-typed - the stale caption this
    # replaced had hardcoded "9 policies... 13 actions" from an earlier,
    # smaller version of that list and had quietly drifted wrong (real
    # count by now: 12 policies / 18 actions) - computing it live means it
    # can't drift out of sync again as the list keeps growing.
    _distinct_policies = list(dict.fromkeys(
        p["AWS Managed Policy"] for p in REQUIRED_AWS_POLICIES if not p["AWS Managed Policy"].startswith("(")
    ))
    _custom_actions = [p["Policy / Action"] for p in REQUIRED_AWS_POLICIES if p["AWS Managed Policy"].startswith("(")]

    with st.expander("📖 Instructions: How to obtain AWS IAM Access Keys", expanded=expanded):
        st.markdown(
            '<div class="fl-setup-steps">'
            '<div class="fl-setup-step-row"><span class="num">1</span><span>Sign in to the <b>AWS Management Console</b> and open the <b>IAM Console</b>.</span></div>'
            '<div class="fl-setup-step-row"><span class="num">2</span><span>Under <b>Users</b>, select your user or create a dedicated <code>FinOpsOptimizerUser</code>.</span></div>'
            '<div class="fl-setup-step-row"><span class="num">3</span><span>Open the <b>Security credentials</b> tab, then <b>Create access key</b>.</span></div>'
            '<div class="fl-setup-step-row"><span class="num">4</span><span>Select <b>Command Line Interface (CLI)</b> and copy the <b>Access Key ID</b> + <b>Secret Access Key</b>.</span></div>'
            '</div>',
            unsafe_allow_html=True,
        )

        st.markdown(f'<span class="fl-setup-num">5</span><span class="fl-setup-head">Attach these {len(_distinct_policies)} managed policies</span>', unsafe_allow_html=True)
        st.markdown(
            '<div class="fl-setup-desc">One per AWS service this app reads from. Each is a stock, read-only '
            'AWS-managed policy - attach exactly these, nothing broader is needed.</div>',
            unsafe_allow_html=True,
        )
        _chip_html = "".join(f'<div class="fl-policy-chip"><span class="dot"></span>{p}</div>' for p in _distinct_policies)
        st.markdown(f'<div class="fl-policy-grid">{_chip_html}</div>', unsafe_allow_html=True)

        if _custom_actions:
            st.markdown(
                f'<div class="fl-custom-note">⚠️ <div><b>{len(_custom_actions)} actions have no dedicated AWS-managed policy</b> '
                f'({", ".join(f"<code>{a.split(" ")[0]}</code>" for a in _custom_actions)}) - cover them with a small '
                'custom inline policy below, or the broad <code>ReadOnlyAccess</code> policy.</div></div>',
                unsafe_allow_html=True,
            )
            # Real gap the user caught, 2026-08-30: the callout above told
            # you to "attach a small custom inline policy" but never showed
            # HOW - no console steps, no actual policy document. Individual
            # actions flattened from _custom_actions ("dms:Describe... /
            # dms:Describe... / dms:Describe...") into one real JSON policy
            # document, generated FROM REQUIRED_AWS_POLICIES (not
            # hand-typed) so it can't silently drift from the actions listed
            # above it as more services are added later.
            _custom_flat_actions = sorted({
                action.strip() for group in _custom_actions for action in group.split(" / ")
            })
            _policy_json = json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Sid": "FinOpsOptimizerCustomActions",
                        "Effect": "Allow",
                        "Action": _custom_flat_actions,
                        "Resource": "*",
                    }],
                },
                indent=2,
            )
            st.markdown(
                f'<span class="fl-setup-num">6</span><span class="fl-setup-head">Add one custom inline policy for the remaining {len(_custom_flat_actions)} actions</span>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="fl-setup-desc">In the IAM Console: open your user &gt; <b>Permissions</b> tab &gt; '
                '<b>Add permissions</b> &gt; <b>Create inline policy</b> &gt; <b>JSON</b> tab - paste this, then '
                'name it (e.g. <code>FinOpsOptimizerCustomActions</code>) and create it:</div>',
                unsafe_allow_html=True,
            )
            st.code(_policy_json, language="json")

        st.caption("🧪 Use **Test Access Permissions** on the Add tenant form to check all of this for real - it reports these exact same policy names.")

        # Generated FROM REQUIRED_AWS_POLICIES (aws/connector.py), not
        # hand-typed a second time here - keeps this table and the live Test
        # checklist saying the exact same managed-policy name for the exact
        # same action, permanently, instead of the two silently drifting out
        # of sync the way they had before (real gap the user caught live).
        with st.expander(f"See exactly which action needs which policy ({len(REQUIRED_AWS_POLICIES)} actions)", icon=":material/list_alt:", expanded=False):
            st.dataframe(
                pd.DataFrame(REQUIRED_AWS_POLICIES)[["Policy / Action", "AWS Managed Policy", "Required", "Purpose"]],
                hide_index=True, width="stretch",
                column_config={
                    "Policy / Action": st.column_config.TextColumn("Action", width=280),
                    "AWS Managed Policy": st.column_config.TextColumn("Policy", width=220),
                    "Required": st.column_config.TextColumn(width=170),
                    "Purpose": st.column_config.TextColumn(width=380),
                },
            )


def _render_aws_connect_form(key_prefix: str, mode: str = "live"):
    """AWS's equivalent of _render_azure_connect_form - simpler, since AWS
    live ingestion (EC2/RDS via boto3) isn't built yet; this only registers
    the account in the tenant registry."""
    # Same fix as _render_azure_connect_form above, same real bug: this form
    # used to pre-fill from load_aws_credentials_from_env(), which reflects
    # whatever tenant was LAST connected - wrong for "Add a NEW tenant".
    # Starts genuinely blank now (region keeps a plain, non-identifying
    # "us-east-1" default - not tied to any previously-connected tenant).
    _render_help_page_link("New here? See the Getting Started guide")
    with st.form(f"{key_prefix}_aws_form"):
        col1, col2 = st.columns(2)
        with col1:
            aws_key = st.text_input("AWS Access Key ID", value="", placeholder="AKIAXXXXXXXXXXXXXXXX")
            aws_reg = st.text_input("Default AWS Region", value="us-east-1", placeholder="us-east-1")
        with col2:
            aws_sec = st.text_input("AWS Secret Access Key", value="", type="password", autocomplete="new-password")

        c_btn1, c_btn2 = st.columns(2)
        with c_btn1:
            aws_test_btn = st.form_submit_button("🧪 Test Access Permissions", use_container_width=True)
        with c_btn2:
            aws_sub_btn = st.form_submit_button("💾 Connect & Save AWS Credentials", type="primary", use_container_width=True)

    if aws_test_btn:
        new_aws = AWSCredentials(aws_key, aws_sec, aws_reg)
        if new_aws.is_complete:
            with st.spinner("Checking IAM permissions..."):
                check = check_aws_permissions(new_aws)
            if check["checked"]:
                _render_aws_permission_checklist(check["results"])
            else:
                st.error(f"❌ Could not check permissions: {check['error']}")
        else:
            st.error("Please fill in Access Key ID and Secret Access Key before testing.")

    if aws_sub_btn:
        new_aws = AWSCredentials(aws_key, aws_sec, aws_reg)
        if new_aws.is_complete:
            save_aws_credentials_to_env_file(new_aws)
            tenant_db_id = upsert_tenant(
                provider="AWS", mode=mode, tenant_name=f"AWS ({aws_reg})",
                tenant_id=aws_reg, subscription_id=aws_reg,
                client_id=aws_key, client_secret=aws_sec,
            )
            # Best-effort - resolves the real AWS Account ID via STS
            # (shown in the Tenant Management table's Account ID column)
            # without blocking the save if it fails; a bad credential still
            # gets registered, just without an account ID until fixed.
            conn_check = test_aws_connection(new_aws)
            if conn_check["success"]:
                update_aws_account_id("AWS", mode, tenant_db_id, conn_check["account_id"])
            # Connecting a tenant IS a sync, same as Azure's "Connect, Save &
            # Ingest Live Tenant Data" button - this used to only save
            # credentials and leave "Last synced" at "Never" until someone
            # separately found and clicked "Run sync now" (real gap caught
            # live 2026-08-21, right after the underlying AWS fetch itself
            # was verified working). The old message here ("Live AWS
            # inventory fetch isn't implemented yet") was also stale by this
            # point - that fetch was built two rounds earlier and just never
            # got wired into this button.
            with st.spinner(f"Ingesting live EC2/RDS inventory from AWS ({aws_reg})..."):
                res = run_ingestion_pipeline("AWS", creds=new_aws, tenant_db_id=tenant_db_id)
            record_sync_result("AWS", mode, tenant_db_id, res["status"], res["message"])
            if res["status"] == "SUCCESS":
                st.success(f"🎉 **Live Tenant Ingestion Complete!** {res['message']}")
            else:
                st.error(f"❌ Credentials saved, but ingestion failed: {res['message']}")
            return True
        else:
            st.error("Please fill in Access Key ID and Secret Access Key.")
    return False


_SYNC_INTERVAL_LABELS = {1: "Every hour", 3: "Every 3 hours", 6: "Every 6 hours", 12: "Every 12 hours", 24: "Daily (24h)"}


def _clear_manage_tenant_dialog_state():
    """on_dismiss callback (user closed the dialog via the X, Escape, or
    clicking outside) - clears the session_state flag that keeps the dialog
    open across its own internal st.rerun() calls, so it doesn't immediately
    reopen on the very next script run. See page_home()'s caller for why a
    plain `if button: _manage_tenant_dialog(...)` doesn't work at all here -
    a button's True state only lasts one rerun, so every action *inside* the
    dialog (which all end in st.rerun()) used to close it immediately."""
    st.session_state["_manage_tenant_id"] = None
    st.session_state.pop("_tenant_mgmt_toast", None)


# _status_from_role_check moved to azure_conn.connector.status_from_role_check
# (imported below) so data/sync_pipeline.py - which can't import app.py, since
# that pulls in streamlit and isn't shipped to the Function App - can use the
# exact same status logic instead of a duplicated copy.


def _render_role_checklist(required_roles: list, status: str, assigned_str: str):
    """Shows every required role's real assigned/missing state, one line
    each - replaces the old single aggregate "Missing X, Y" line, which
    couldn't distinguish "checked, 0 missing" from "checked, all missing"
    without reading the detail text. Only meaningful when status is "ready"
    or "missing_role" (the check actually ran) - callers handle
    "error"/"unchecked" themselves, since there's no real per-role data for
    those states."""
    assigned_set = {r.strip() for r in (assigned_str or "").split(",") if r.strip()}
    for role in required_roles:
        name = role["Role Name"]
        if name in assigned_set:
            st.markdown(f"✅ {name}")
        else:
            st.markdown(f"❌ {name}")


def _render_aws_permission_checklist(results: list):
    """AWS equivalent of _render_role_checklist above - one line per
    REQUIRED_AWS_POLICIES action, straight from check_aws_permissions()'s
    real per-action results. Not collapsed into one aggregate status the way
    Azure's assigned/missing comma-string is, because AWS permissions are
    fundamentally checked action-by-action (no single named "role" the way
    Azure RBAC has) - see check_aws_permissions()'s docstring for the full
    reasoning, including why ce:* actions show "unverified" rather than a
    real pass/fail here."""
    icons = {"ready": "✅", "missing": "❌", "error": "⚠️", "unverified": "🕓"}
    for r in results:
        icon = icons.get(r["status"], "❓")
        # Same managed-policy name as the Instructions expander above -
        # check_aws_permissions() attaches it to every result specifically
        # so there's one name to attach in AWS, shown consistently here and
        # in Instructions, not two different labels to cross-reference.
        policy = r.get("managed_policy")
        policy_suffix = f" — `{policy}`" if policy and not policy.startswith("(") else ""
        st.markdown(f"{icon} `{r['action']}`{policy_suffix}")
        if r.get("detail") and r["status"] in ("missing", "error", "unverified"):
            st.caption(r["detail"])


def _run_tenant_permission_check(t, mode: str) -> dict:
    """Shared by the Tenant-wide permissions tab's "Check tenant permissions"
    button (and formerly the Connection health card's "Re-check everything",
    since folded into this simpler tab-based layout) - real tenant-scope
    RBAC check (Reservations Reader / Savings Plan Reader)."""
    creds = AzureCredentials(t.tenant_id, t.subscription_id, t.client_id, get_tenant_credentials(t))
    role_check = check_tenant_role_assignments(creds)
    status, missing, assigned = status_from_role_check(role_check)
    update_tenant_permission_status(selected_provider, mode, t.id, status, missing, assigned)
    return role_check


def _run_subscription_sync(t, mode: str) -> list:
    """Shared by the Subscriptions tab's "Sync subscriptions" button -
    (re)discovers every subscription the Service Principal can see and
    checks its subscription-level role assignment (Reader / Cost Management
    Reader)."""
    creds = AzureCredentials(t.tenant_id, t.subscription_id, t.client_id, get_tenant_credentials(t))
    live_subs = list_accessible_subscriptions(creds)
    for s in live_subs:
        role_check = check_role_assignments(creds, s["subscription_id"])
        status, missing, assigned = status_from_role_check(role_check)
        upsert_subscription(
            provider=selected_provider, mode=mode, tenant_db_id=t.id,
            subscription_id=s["subscription_id"], subscription_name=s["display_name"],
            permission_status=status, missing_role=missing, assigned_roles=assigned,
        )
    return live_subs


@st.dialog("Manage tenant", width="large", on_dismiss=_clear_manage_tenant_dialog_state)
def _manage_tenant_dialog(t, mode: str):
    """Per-tenant editing, laid out as tabs (Credentials / Subscriptions /
    Tenant-wide permissions / Sync / Features) rather than a stack of
    bordered cards - approved sketch 2026-08, referencing Azure Portal's own
    resource blade pattern (Essentials-style plain status lines, one focused
    section visible at a time instead of everything scrolling past at once).
    Each tab whose status matters shows a small icon in its own label so you
    can tell what needs attention without opening it. Production only for
    anything beyond the name edit - Demo tenants have no real Azure behind
    them, and every control here stays visible but disabled for Demo
    (confirmed "same UI, different live-ness" design).

    IMPORTANT: this function is called from page_home() every rerun while
    st.session_state["_manage_tenant_id"] == t.id - NOT gated on a button's
    return value. Every action below ends in st.rerun(); if this were only
    reachable via `if button: _manage_tenant_dialog(...)`, the dialog would
    close itself after the very first action (a real bug found live 2026-08:
    a button's clicked state doesn't persist past the one rerun immediately
    following the click)."""
    is_demo = (mode == "demo")
    st.caption(f"{selected_provider} · {'Demo' if is_demo else 'Production'}")

    # Real bug confirmed live in production, 2026-08-30 (user drove a real
    # browser session against this exact dialog): every "Saved" confirmation
    # below (Tenant name/Credentials/Sync schedule, both AWS and Azure
    # branches) called st.success(...) immediately followed by st.rerun() -
    # the rerun aborts the script right there, so the message never actually
    # reaches the frontend. Confirmed by reproducing it twice live (Tenant
    # name Save, Sync schedule Save) - neither ever showed a confirmation.
    # Same toast-relay fix already used for the Add User form's identical
    # shape: every st.success() below is replaced with setting this flag,
    # displayed here at the very top (before name_row/segmented_control) so
    # it's visible regardless of which tab triggered it.
    _toast_msg = st.session_state.pop("_tenant_mgmt_toast", None)
    if _toast_msg:
        st.success(_toast_msg)

    # Reserved now, filled in further down (after segmented_control has run) -
    # a Streamlit container's screen position is fixed at creation, not at
    # the point its content is written, so this stays visually first. Needed
    # because the Save button below calls st.rerun(), and if that happens
    # BEFORE segmented_control executes even once in a given script run,
    # Streamlit prunes that widget's session-state entry as "not used this
    # run" - which is what caused the segmented control to keep resetting to
    # its first option every time this Save button was clicked (real bug
    # found live 2026-08, several layers deeper than it first looked).
    name_row = st.container()

    if not is_azure:
        # Same two lessons already learned on the Azure side, applied here
        # too: the segmented control must register BEFORE the name-Save
        # button's st.rerun() (or it gets pruned and resets), and the
        # name row's screen position is reserved via a placeholder so it
        # still renders first visually.
        aws_sync_icon = {"SUCCESS": ":material/check_circle:", "FAILED": ":material/cancel:", "PARTIAL": ":material/warning:"}.get(t.last_sync_status, "")
        _AWS_SECTIONS = ["Credentials", "Permissions", "Sync"]
        _aws_section_icons = {"Sync": aws_sync_icon}
        aws_section_key = f"mgmt_aws_section_{t.id}"
        active_aws_section = st.segmented_control(
            "Section", options=_AWS_SECTIONS,
            format_func=lambda name: f"{name} {_aws_section_icons.get(name, '')}".rstrip(),
            default=st.session_state.get(aws_section_key, _AWS_SECTIONS[0]),
            required=True, key=aws_section_key, label_visibility="collapsed",
        )

        with name_row:
            name_col, save_col = st.columns([4, 1])
            new_name = name_col.text_input("Tenant name", value=t.tenant_name, key=f"mgmt_name_{t.id}")
            if save_col.button("Save", key=f"mgmt_save_name_{t.id}", width="stretch"):
                update_tenant_name(selected_provider, mode, t.id, new_name)
                st.session_state["_tenant_mgmt_toast"] = "Tenant name updated."
                st.rerun()

        if active_aws_section == "Credentials":
            if is_demo:
                st.caption("Not applicable - a demo tenant has no real AWS credentials behind it.")
            else:
                with st.form(f"mgmt_aws_creds_{t.id}"):
                    aws_key_edit = st.text_input("AWS Access Key ID", value=t.client_id)
                    aws_reg_edit = st.text_input("Default AWS Region", value=t.tenant_id)
                    aws_sec_edit = st.text_input("AWS Secret Access Key", value="", type="password",
                                                  placeholder="Leave blank to keep the current secret", autocomplete="new-password")
                    if st.form_submit_button("Save credentials", type="primary"):
                        secret_to_save = aws_sec_edit if aws_sec_edit else get_tenant_credentials(t)
                        upsert_tenant(
                            provider="AWS", mode=mode, tenant_name=new_name or t.tenant_name,
                            tenant_id=aws_reg_edit, subscription_id=aws_reg_edit,
                            client_id=aws_key_edit, client_secret=secret_to_save,
                        )
                        st.session_state["_tenant_mgmt_toast"] = "Credentials updated."
                        st.rerun()

            # Added 2026-08-30 for parity with the Azure branch's Credentials
            # tab, which already links out to Help & Support - AWS's own
            # Manage dialog never had a guidance link of any kind before.
            _render_help_page_link("Need the setup guide? Open Help & Support")
        elif active_aws_section == "Permissions":
            if is_demo:
                st.caption("Not applicable - a demo tenant has no real AWS credentials behind it.")
            else:
                st.caption("Checked live against AWS each time - not persisted, same as the Add a new tenant form's test.")
                if st.button("Check permissions", icon=":material/sync:", disabled=is_demo, key=f"mgmt_aws_check_{t.id}"):
                    creds = AWSCredentials(t.client_id, get_tenant_credentials(t), t.tenant_id)
                    with st.spinner("Checking IAM permissions..."):
                        check = check_aws_permissions(creds)
                        conn_check = test_aws_connection(creds)
                    if conn_check["success"]:
                        update_aws_account_id(selected_provider, mode, t.id, conn_check["account_id"])
                    if check["checked"]:
                        _render_aws_permission_checklist(check["results"])
                    else:
                        st.error(f"Could not check permissions: {check['error']}", icon=":material/cancel:")
        elif active_aws_section == "Sync":
            if t.last_sync_status == "SUCCESS":
                st.success(t.last_sync_message or "Last sync succeeded.", icon=":material/check_circle:")
            elif t.last_sync_status == "PARTIAL":
                st.warning(t.last_sync_message or "Last sync partially completed.", icon=":material/warning:")
            elif t.last_sync_status == "FAILED":
                st.error(t.last_sync_message or "Last sync failed.", icon=":material/cancel:")
            else:
                st.caption("No sync attempted yet.")

            current_interval = t.sync_interval_hours if t.sync_interval_hours in _SYNC_INTERVAL_LABELS else 1
            i1, i2 = st.columns([3, 2])
            new_interval = i1.selectbox(
                "Automated sync interval", options=list(_SYNC_INTERVAL_LABELS.keys()),
                format_func=lambda h: _SYNC_INTERVAL_LABELS[h],
                index=list(_SYNC_INTERVAL_LABELS.keys()).index(current_interval),
                key=f"mgmt_aws_interval_{t.id}", disabled=is_demo, width=260,
                help="A single hourly cron checks every tenant and only re-syncs the ones due, based on this setting.",
            )
            i2.caption("")
            if i2.button("Save schedule", disabled=is_demo, key=f"mgmt_aws_save_interval_{t.id}", width="stretch"):
                update_sync_interval(selected_provider, mode, t.id, new_interval)
                st.session_state["_tenant_mgmt_toast"] = "Sync schedule updated."
                st.rerun()

            st.caption(f"Last synced: {t.last_synced_at[:16] if t.last_synced_at else 'Never'}")

            if st.button("Run sync now", icon=":material/bolt:", disabled=is_demo, key=f"mgmt_aws_run_sync_{t.id}", type="primary",
                         help="Only available for Production tenants." if is_demo else "Fetches live EC2/RDS inventory from this tenant right now."):
                with st.spinner(f"Running ingestion for '{t.tenant_name}'..."):
                    sync_creds = AWSCredentials(t.client_id, get_tenant_credentials(t), t.tenant_id)
                    res = run_ingestion_pipeline(selected_provider, creds=sync_creds, tenant_db_id=t.id)
                record_sync_result(selected_provider, mode, t.id, res["status"], res["message"])
                st.cache_data.clear()
                st.rerun()

        st.divider()
        if st.button("Delete tenant", icon=":material/delete:", disabled=is_demo, key=f"mgmt_delete_{t.id}"):
            delete_tenant(selected_provider, mode, t.id)
            st.session_state["_manage_tenant_id"] = None
            st.rerun()
        return

    subs = list_subscriptions(selected_provider, mode, t.id)
    sub_ready = bool(subs) and all(s.permission_status == "ready" for s in subs)
    sub_missing = any(s.permission_status == "missing_role" for s in subs)
    sub_error = any(s.permission_status == "error" for s in subs)
    sub_icon = ":material/check_circle:" if sub_ready else ":material/cancel:" if sub_error else ":material/warning:" if sub_missing else ""
    tenant_icon = {"ready": ":material/check_circle:", "error": ":material/cancel:", "missing_role": ":material/warning:"}.get(t.tenant_permission_status, "")
    sync_icon = {"SUCCESS": ":material/check_circle:", "FAILED": ":material/cancel:", "PARTIAL": ":material/warning:"}.get(t.last_sync_status, "")

    # st.tabs() has no session-state-backed selection - every st.rerun() call
    # below (Verify, Sync, Save, Run sync now...) remounts it fresh and it
    # snaps back to the first tab, losing whatever section the user was
    # looking at (real bug found live 2026-08, same root cause as the
    # @st.dialog session-state gating above: Streamlit UI state that isn't
    # explicitly stored in st.session_state doesn't survive a rerun).
    # st.segmented_control IS a real keyed widget - its value round-trips
    # through st.session_state[key] like any other widget, so it stays on
    # the active section across every action in this dialog. Option VALUES
    # are kept stable ("Subscriptions", not "Subscriptions ⚠️") and the
    # status icon is applied only for display via format_func - if the icon
    # were part of the value itself, a status change (e.g. ⚠️ -> ✅ right
    # after Verify) would make the stored selection match nothing in the
    # next render's options list and reset anyway.
    _SECTIONS = ["Credentials", "Subscriptions", "Tenant-wide permissions", "Sync", "Features"]
    _section_icons = {"Subscriptions": sub_icon, "Tenant-wide permissions": tenant_icon, "Sync": sync_icon}
    _section_key = f"mgmt_section_{t.id}"
    # `default` is only the seed value for the FIRST render - passing a
    # hardcoded default here every time (the original mistake) silently wins
    # over the widget's own persisted selection on every rerun, which looked
    # identical to st.tabs()'s reset bug. Reading the prior value back out of
    # session_state before rendering is what actually makes it sticky.
    active_section = st.segmented_control(
        "Section", options=_SECTIONS,
        format_func=lambda name: f"{name} {_section_icons.get(name, '')}".rstrip(),
        default=st.session_state.get(_section_key, _SECTIONS[0]),
        required=True, key=_section_key,
        label_visibility="collapsed",
    )

    # Filled in now (registered above, drawn into the reserved slot from
    # before) - see name_row's creation comment for why this ordering matters.
    with name_row:
        name_col, save_col = st.columns([4, 1])
        new_name = name_col.text_input("Tenant name", value=t.tenant_name, key=f"mgmt_name_{t.id}")
        if save_col.button("Save", key=f"mgmt_save_name_{t.id}", width="stretch"):
            update_tenant_name(selected_provider, mode, t.id, new_name)
            st.session_state["_tenant_mgmt_toast"] = "Tenant name updated."
            st.rerun()

    # ── Credentials ───────────────────────────────────────────────────────
    if active_section == "Credentials":
        if is_demo:
            st.caption("Not applicable - a demo tenant has no real Service Principal behind it.")
        else:
            with st.form(f"mgmt_creds_{t.id}"):
                c1, c2 = st.columns(2)
                with c1:
                    new_tenant_id = st.text_input("Tenant ID", value=t.tenant_id)
                    new_domain = st.text_input("Domain (optional)", value=t.domain or "")
                with c2:
                    new_client_id = st.text_input("Application ID", value=t.client_id)
                    new_secret = st.text_input("Client secret", value="", type="password",
                                                placeholder="Leave blank to keep the current secret", autocomplete="new-password")
                creds_submitted = st.form_submit_button("Save credentials", type="primary")
            if creds_submitted:
                secret_to_save = new_secret if new_secret else get_tenant_credentials(t)
                upsert_tenant(
                    provider=selected_provider, mode=mode, tenant_name=new_name or t.tenant_name,
                    tenant_id=new_tenant_id, subscription_id=t.subscription_id,
                    client_id=new_client_id, client_secret=secret_to_save, domain=new_domain or None,
                )
                st.session_state["_tenant_mgmt_toast"] = "Credentials updated."
                st.rerun()

        # Simplified to the same new-tab link used on the Add tenant form,
        # 2026-08-30 - the original objection to doing this here (a same-tab
        # st.page_link would close this modal dialog entirely) doesn't apply
        # to _render_help_page_link's real <a target="_blank">, which opens
        # a separate tab and leaves this dialog untouched.
        _render_help_page_link("Need the setup guide? Open Help & Support")

    # ── Subscriptions ─────────────────────────────────────────────────────
    elif active_section == "Subscriptions":
        if st.button("Sync subscriptions", icon=":material/sync:", disabled=is_demo, key=f"mgmt_sync_subs_{t.id}",
                      help="Only available for Production tenants." if is_demo else "Discovers subscriptions and checks Reader/Cost Management Reader on each."):
            with st.spinner("Enumerating subscriptions and checking permissions..."):
                live_subs = _run_subscription_sync(t, mode)
                if not live_subs:
                    st.error("Could not enumerate subscriptions - check the credentials in the Credentials tab.")
            st.rerun()

        if subs:
            for s in subs:
                st.markdown(f"**{s.subscription_name or s.subscription_id}**")
                st.caption(s.subscription_id)
                if s.permission_status in ("ready", "missing_role"):
                    _render_role_checklist(REQUIRED_SUBSCRIPTION_ROLES, s.permission_status, s.assigned_roles)
                elif s.permission_status == "error":
                    st.error(s.missing_role or "Could not check", icon=":material/cancel:")
                else:
                    st.caption("Not checked yet.")
                if st.button("Verify", key=f"mgmt_verify_sub_{s.id}", disabled=is_demo,
                             help="Only available for Production tenants." if is_demo else None):
                    with st.spinner("Checking permissions..."):
                        creds = AzureCredentials(t.tenant_id, t.subscription_id, t.client_id, get_tenant_credentials(t))
                        role_check = check_role_assignments(creds, s.subscription_id)
                        status, missing, assigned = status_from_role_check(role_check)
                        upsert_subscription(
                            provider=selected_provider, mode=mode, tenant_db_id=t.id,
                            subscription_id=s.subscription_id, subscription_name=s.subscription_name,
                            permission_status=status, missing_role=missing, assigned_roles=assigned,
                        )
                    st.rerun()
                st.divider()
        else:
            st.caption("No subscriptions recorded yet - click **Sync subscriptions** above.")

        # Moved here from the Tenant-wide permissions tab, 2026-08-30 - real
        # UX gap the user caught live: this reference table is about
        # subscription-scoped roles, which is what THIS tab checks (the
        # per-subscription checklist above) - it was previously duplicated
        # into the Tenant-wide permissions tab as well, where it didn't
        # match that tab's own scope at all.
        with st.expander("Required Azure RBAC roles (subscription-scoped)", icon=":material/checklist:", expanded=False):
            st.dataframe(pd.DataFrame(REQUIRED_SUBSCRIPTION_ROLES)[["Role Name", "Scope", "Purpose"]], hide_index=True, width="stretch")

    # ── Tenant-wide permissions (Reservations / Savings Plans) ──────────
    elif active_section == "Tenant-wide permissions":
        st.caption("Reservations and Savings Plans are tenant-wide resources with their own separate permission system, not covered by the subscription-level roles checked in the Subscriptions tab.")
        if t.tenant_permission_status in ("ready", "missing_role"):
            _render_role_checklist(REQUIRED_TENANT_ROLES, t.tenant_permission_status, t.tenant_assigned_roles)
            if t.tenant_permission_status == "missing_role":
                st.caption("Needs User Access Administrator at the tenant level - often unavailable on student/trial accounts. Inventory and cost data are unaffected; only Reservations/Savings Plan data needs this.")
        elif t.tenant_permission_status == "error":
            st.error(t.tenant_missing_roles or "Could not check", icon=":material/cancel:")
        else:
            st.caption("Not checked yet.")
        if st.button("Check tenant permissions", icon=":material/sync:", disabled=is_demo, key=f"mgmt_tenant_check_{t.id}",
                      help="Only available for Production tenants." if is_demo else None):
            with st.spinner("Checking tenant-level permissions..."):
                _run_tenant_permission_check(t, mode)
            st.rerun()

        with st.expander("Required Azure RBAC roles (tenant-scoped)", icon=":material/checklist:", expanded=False):
            st.dataframe(pd.DataFrame(REQUIRED_TENANT_ROLES)[["Role Name", "Scope", "Purpose"]], hide_index=True, width="stretch")

    # ── Sync ───────────────────────────────────────────────────────────
    elif active_section == "Sync":
        if t.last_sync_status == "SUCCESS":
            st.success(t.last_sync_message or "Last sync succeeded.", icon=":material/check_circle:")
        elif t.last_sync_status == "PARTIAL":
            st.warning(t.last_sync_message or "Last sync partially completed.", icon=":material/warning:")
        elif t.last_sync_status == "FAILED":
            st.error(t.last_sync_message or "Last sync failed.", icon=":material/cancel:")
        else:
            st.caption("No sync attempted yet.")

        current_interval = t.sync_interval_hours if t.sync_interval_hours in _SYNC_INTERVAL_LABELS else 1
        i1, i2 = st.columns([3, 2])
        new_interval = i1.selectbox(
            "Automated sync interval", options=list(_SYNC_INTERVAL_LABELS.keys()),
            format_func=lambda h: _SYNC_INTERVAL_LABELS[h],
            index=list(_SYNC_INTERVAL_LABELS.keys()).index(current_interval),
            key=f"mgmt_interval_{t.id}", disabled=is_demo, width=260,
            help="A single hourly cron checks every tenant and only re-syncs the ones due, based on this setting.",
        )
        i2.caption("")
        if i2.button("Save schedule", disabled=is_demo, key=f"mgmt_save_interval_{t.id}", width="stretch"):
            update_sync_interval(selected_provider, mode, t.id, new_interval)
            st.session_state["_tenant_mgmt_toast"] = "Sync schedule updated."
            st.rerun()

        st.caption(f"Last synced: {t.last_synced_at[:16] if t.last_synced_at else 'Never'}")

        if st.button("Run sync now", icon=":material/bolt:", disabled=is_demo, key=f"mgmt_run_sync_{t.id}", type="primary",
                     help="Only available for Production tenants." if is_demo else "Fetches live inventory, reservations, and savings plans from this tenant right now."):
            with st.spinner(f"Running ingestion for '{t.tenant_name}'..."):
                sync_creds = AzureCredentials(t.tenant_id, t.subscription_id, t.client_id, get_tenant_credentials(t))
                res = run_ingestion_pipeline(selected_provider, creds=sync_creds, tenant_db_id=t.id)
            record_sync_result(selected_provider, mode, t.id, res["status"], res["message"])
            st.cache_data.clear()
            st.rerun()

    # ── Features ─────────────────────────────────────────────────────
    elif active_section == "Features":
        st.markdown("##### Pricing cache")
        st.caption(
            "Clears every cached PAYG rate, commitment (RI/Savings Plan) rate, and (Azure) VM "
            "flexibility-group/memory-spec row for this provider, then re-fetches everything from "
            "scratch on the next sync. None of these caches are specific to this one tenant - they're "
            f"shared by SKU/region across every {selected_provider} tenant in this environment, so "
            "clearing here clears them for all of those tenants, not just this one."
        )
        if st.button(
            "Clear cached pricing", icon=":material/restart_alt:", key=f"mgmt_clear_pricing_{t.id}",
            disabled=is_demo,
            help="Only available for Production tenants." if is_demo else
                 "Use this if a resource's shown cost looks wrong and you suspect a stale/incorrect cached rate.",
        ):
            deleted = clear_pricing_caches(get_engine(selected_provider, mode), selected_provider)
            total = sum(deleted.values())
            st.session_state["_tenant_mgmt_toast"] = (
                f"Cleared {total} cached pricing row(s). Run a sync to re-fetch live rates."
            )
            st.rerun()

    st.divider()
    if st.button("Delete tenant", icon=":material/delete:", disabled=is_demo, key=f"mgmt_delete_prod_{t.id}",
                 help="Only available for Production tenants." if is_demo else None):
        delete_tenant(selected_provider, mode, t.id)
        st.session_state["_manage_tenant_id"] = None
        st.rerun()


def _compute_portfolio_kpis():
    """Aggregates the same real per-tenant numbers the Analyze page already
    computes (run_waterfall / savings_plan_analysis / reservation_analysis /
    generate_recommendations - not a separate/approximate calculation) across
    EVERY tenant on BOTH providers for the current mode, added 2026-08-27 for
    the Home page redesign. This is deliberately the only new cross-tenant
    view in the app - every other page is scoped to whichever single
    provider/tenant is currently selected in the sidebar.

    Demo mode always has exactly one seeded tenant per provider (tenant_id=
    None) - same convention Tenant Management's own table already uses, not
    a Home-specific special case. Live mode enumerates whatever's actually
    connected via list_tenants(); a provider with nothing connected simply
    contributes nothing, no fabricated zero-tenant row.

    Sync staleness is only evaluated in live mode - a demo tenant has no
    real "last synced" concept to flag."""
    from datetime import datetime, timedelta
    STALE_AFTER_DAYS = 7

    total_tenants = 0
    total_resources = 0
    total_payg_hr = 0.0
    total_committed_hr = 0.0
    total_critical = 0
    stale = []

    for provider in ("Azure", "AWS"):
        sp_types = COMPUTE_SP_ELIGIBLE_TYPES if provider == "Azure" else AWS_COMPUTE_SP_TYPES
        if tenant_mode == "demo":
            tenants = [(None, f"{provider} Demo Tenant", None)]
        else:
            tenants = [(t.id, t.tenant_name, t.last_synced_at) for t in list_tenants(provider, "live")]

        for tenant_id, tname, last_synced in tenants:
            total_tenants += 1
            inv = get_compute_inventory(provider=provider, mode=tenant_mode, tenant_id=tenant_id)
            total_resources += len(inv)
            # Filtered to Running only (2026-08-30, real known bug fixed -
            # tracked as an open follow-up since the Cost Analysis tab was
            # removed for this identical issue: a Stopped resource's own
            # PAYG rate is stored at its full on-demand rate regardless of
            # state, so summing unconditionally silently inflated this KPI
            # by every stopped resource's rate too. Same filter pattern
            # already used for the Recommendations tab's own baseline calc.
            total_payg_hr += float(inv.loc[inv["Resource State"] == "Running", "PAYG Hourly Cost USD"].sum()) if not inv.empty else 0.0

            sp_df = get_existing_savings_plans(provider=provider, mode=tenant_mode, tenant_id=tenant_id)
            ri_df = get_existing_reservations(provider=provider, mode=tenant_mode, tenant_id=tenant_id)
            total_committed_hr += float(sp_df["hourly_usd_commitment"].sum()) if not sp_df.empty else 0.0
            total_committed_hr += float((ri_df["hourly_usd_commitment"] * ri_df["reserved_qty"]).sum()) if not ri_df.empty else 0.0

            if not inv.empty:
                inv = inv.copy()
                inv["Is Orphaned"] = compute_orphaned_status(inv, ri_df)
            wf = run_waterfall(inv, ri_df, sp_df, simulate_days=simulate_days)
            sp_res = savings_plan_analysis(inv, sp_df, safety_buffer=safety_buffer, eligible_types=list(sp_types))
            flex_groups_df = get_vm_flexibility_groups(get_engine(provider, tenant_mode)) if provider == "Azure" else None
            ri_res = reservation_analysis(inv, ri_df, flex_groups_df)
            recs = generate_recommendations(sp_res, ri_res, wf, safety_buffer=safety_buffer)
            total_critical += sum(1 for r in recs if r["severity"] == "HIGH")

            if tenant_mode == "live" and last_synced:
                try:
                    synced_at = datetime.strptime(last_synced.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")
                    if datetime.utcnow() - synced_at > timedelta(days=STALE_AFTER_DAYS):
                        stale.append(tname)
                except ValueError:
                    pass

    return {
        "tenants": total_tenants, "resources": total_resources,
        "payg_hr": total_payg_hr, "committed_hr": total_committed_hr,
        "critical": total_critical, "stale": stale,
    }


def page_home():
    """Portfolio overview - rebuilt 2026-08-27 (previously a pure landing
    page with just a welcome line, no metrics, no tenant table - see git
    history for that version's own reasoning, superseded here). The one
    thing genuinely missing from the rest of the app is a cross-tenant
    rollup - every other KPI anywhere is scoped to whichever single
    provider/tenant the sidebar has selected - so that's Home's whole job
    now: real aggregate numbers, a nudge if something needs attention, one
    way in to Tenant Management for anything actionable.

    Deliberately does NOT re-list tenants here (name/status/last-synced/
    actions) - that's Tenant Management's table, verbatim, and duplicating
    it here was flagged directly and removed during design review."""
    st.markdown("## :material/home: Home")
    st.caption(f"Welcome back, {current_user['display_name'] or current_user['username']} — here's your portfolio across Azure and AWS.")

    kpis = _compute_portfolio_kpis()

    if kpis["tenants"] == 0:
        st.info("No tenants connected yet. Connect your first one to see your portfolio here.")
    else:
        # No divider, no bordered box - matches _render_top_header()'s own
        # convention for the Analyze page's top-level KPI row
        # (st.container(border=False)), not _render_sp_pool_economics'
        # bordered sub-widget style, which was the wrong precedent to copy
        # (real user feedback: the box + the divider above it read as more
        # framing than a simple summary row needs).
        # Back to one row of 5 equal columns (2026-08-29, real feedback -
        # 2 rounds of column-width juggling both failed and the 2-row
        # split that avoided the tradeoff read as misaligned/"floating"
        # instead: round 1 clipped the $ VALUES, round 2's weighted
        # columns fixed that but clipped the count metrics' LABELS
        # instead, and the 2-row split fixed both but broke the single
        # clean row's visual alignment the user actually wanted kept).
        # Real fix instead of another layout guess: st.metric's value
        # text just doesn't wrap by default (confirmed via this
        # Streamlit build's own real stMetricValue/stMetricLabel
        # data-testids, Metric.CmkuJai4.js) - ui/styling.py's
        # .st-key-fl_home_kpis rule below lets it wrap onto a second line
        # instead of ellipsis-clipping, so a long INR value has somewhere
        # to go without needing extra column width at all.
        # Gradient .rec-metric-card cards, not st.metric (2026-08-30, real
        # feedback - same "bring it in line with the KPI-card treatment"
        # pass already applied to RI Coverage/SP Analysis/Recommendations/
        # Rightsizing/Maturity). Critical Recommendations keeps the same
        # tone logic st.metric's delta_color used (red when actionable,
        # green when optimal), just as a card tone instead of a delta pill.
        _crit_tone = "over" if kpis["critical"] > 0 else "savings"
        _crit_note = "Action Required" if kpis["critical"] > 0 else "Optimal"
        with st.container(key="fl_home_kpis"):
            for col, (label, val, tone, note) in zip(st.columns(5), [
                ("Tenants Connected", kpis["tenants"], "savings", ""),
                ("Resources Tracked", kpis["resources"], "combined", ""),
                ("Total PAYG Rate", fmt(kpis["payg_hr"], 2) + "/hr", "under", ""),
                ("Total Committed", fmt(kpis["committed_hr"], 2) + "/hr", "combined", ""),
                ("Critical Recommendations", f"{kpis['critical']} items" if kpis["critical"] > 0 else "0 items", _crit_tone, _crit_note),
            ]):
                with col:
                    st.markdown(
                        f'<div class="rec-metric-card {tone}">'
                        f'<div class="lbl">{label}</div>'
                        f'<div class="val fl-mono">{val}</div>'
                        + (f'<div class="sub">{note}</div>' if note else "")
                        + "</div>",
                        unsafe_allow_html=True,
                    )

        if kpis["stale"]:
            names = ", ".join(kpis["stale"])
            plural = "s" if len(kpis["stale"]) > 1 else ""
            st.warning(f"⚠️ {len(kpis['stale'])} tenant{plural} hasn't synced in over a week: **{names}**.", icon=":material/warning:")

    st.write("")
    st.markdown("Connect a cloud tenant, review its sync status, or jump into its dashboard - all from Tenant Management.")
    if st.button("Go to Tenant Management", icon=":material/domain:", type="primary"):
        # Same _pending_currency/_pending_provider relay as the Dash
        # button's switch_page(analyze_page) call below - found live
        # 2026-08-30 via the browser: this is a SEPARATE st.switch_page()
        # call site that never got the same fix, so currency (and
        # provider - untested before, but switch_page's own behavior is
        # provider-agnostic, so it's exposed to the identical reset) also
        # reverted to USD/Azure landing on Tenant Management specifically.
        # Every st.switch_page() call in this app needs this relay, not
        # just the one that happened to get reported first.
        st.session_state["_pending_provider"] = selected_provider
        st.session_state["_pending_currency"] = selected_currency
        st.switch_page(tenant_mgmt_page)


def page_tenant_management():
    st.markdown("## :material/domain: Tenant Management")
    st.caption(f"Connect, sync, and manage {selected_provider} tenants.")

    hdr_l, hdr_r = st.columns([4, 1])
    with hdr_l:
        st.markdown(f"#### {selected_provider} Tenants")
    with hdr_r:
        add_disabled = (tenant_mode == "demo")
        # Real gap the user caught, 2026-08-30: this button already TOGGLES
        # _show_add_tenant_form (clicking it again while the form is open
        # closes it, no refresh needed) - but the label never changed to
        # say so, so nothing on screen suggested that was possible. Unlike
        # the Add User form (a real st.expander, whose header IS the
        # familiar click-to-collapse affordance), this section is a plain
        # button revealing a st.container() below it, with no built-in
        # "close" gesture of its own - the button itself has to say it.
        _form_open = st.session_state.get("_show_add_tenant_form", False)
        if st.button(
            "Cancel" if _form_open else "Add a new tenant",
            icon=":material/close:" if _form_open else ":material/add:",
            use_container_width=True, type="secondary" if _form_open else "primary",
            disabled=add_disabled,
            help="Only available in Production Mode." if add_disabled else None,
        ):
            # st.rerun() added - real bug caught live: `_form_open` is read
            # at the TOP of this run, before this click's effect is known,
            # so without forcing an immediate fresh run the button's own
            # label always lagged one click behind the form's actual open/
            # closed state (the form itself updated correctly on the same
            # run, since its `if st.session_state.get(...)` check further
            # down reads the flag AFTER this block already wrote it - only
            # the button's label, computed too early, was stale). Same
            # "force a rerun so state and its own label repaint together"
            # pattern already used everywhere else in this app for exactly
            # this reason.
            st.session_state["_show_add_tenant_form"] = not _form_open
            st.rerun()

    if st.session_state.get("_show_add_tenant_form") and tenant_mode == "live":
        with st.container(border=True):
            if is_azure:
                st.markdown("### :material/cloud: Azure Service Principal Integration")
                if _render_azure_connect_form("home_page", tenant_mode):
                    st.session_state["_show_add_tenant_form"] = False
                    st.rerun()
            else:
                st.markdown("### :material/dns: AWS IAM Credentials Integration")
                if _render_aws_connect_form("home_page", tenant_mode):
                    st.session_state["_show_add_tenant_form"] = False
                    st.rerun()

    tenants = list_tenants(selected_provider, tenant_mode)
    if not tenants:
        st.info(
            f"No {selected_provider} tenants connected yet. Click **Add a new tenant** above."
            if tenant_mode == "live" else "No demo tenant seeded yet."
        )
        return

    # "Subscriptions" is Azure-only (a count of sub-scopes under one
    # tenant) - AWS has no equivalent (one credential set = one account),
    # so that column becomes "Account ID" (resolved via STS, see
    # aws/connector.py's test_aws_connection) for AWS rows instead, per the
    # user's own call confirmed live 2026-08.
    fourth_col_label = "Subscriptions" if is_azure else "Account ID"
    # Back to a table (fifth pass, 2026-08-27 - the card layout was tried
    # and rejected). This time the Actions column genuinely gets more
    # relative width (was 2.2/11.6 ≈ 19% of the row, now 3.0/10.4 ≈ 29%,
    # by trimming the columns that can afford it - Status is a compact
    # badge, Subscriptions/Account ID and Last synced are short values)
    # instead of just shortening the button labels again, which is what
    # failed twice before without ever addressing why the column was too
    # narrow in the first place. Short label + icon ("Dash"/"Manage") on
    # top of that real width margin, not stacked - single-row height,
    # aligned with every other column, no misalignment. "Dash" not "Open" -
    # a new user has no way to know what a bare "Open" opens; "Dash" reads
    # as short for "Dashboard" instead, same length so it still fits.
    with st.container(border=True):
        col_ratios = [2.2, 1.1, 1.6, 1.1, 1.4, 3.0]
        header_cols = st.columns(col_ratios)
        for c, label in zip(header_cols, ["Tenant name", "Status", "Authentication", fourth_col_label, "Last synced", "Actions"]):
            c.caption(f"**{label}**")
        for t in tenants:
            fourth_col_value = len(list_subscriptions(selected_provider, tenant_mode, t.id)) if is_azure else (t.aws_account_id or "—")
            cols = st.columns(col_ratios)
            cols[0].markdown(f"**{t.tenant_name}**")
            with cols[1]:
                if t.is_active:
                    st.badge("Active", icon=":material/check_circle:", color="green")
                else:
                    st.badge("Inactive", icon=":material/radio_button_unchecked:", color="gray")
            cols[2].caption("Service principal" if is_azure else "IAM access key")
            cols[3].caption(str(fourth_col_value))
            cols[4].caption(t.last_synced_at[:16] if t.last_synced_at else "Never")
            with cols[5]:
                b1, b2 = st.columns(2)
                if b1.button("Dash", icon=":material/open_in_new:", key=f"home_dash_{t.id}", width="stretch"):
                    set_active_tenant(selected_provider, tenant_mode, t.id)
                    # st.switch_page() clears st.query_params on its way to the
                    # destination page, and the Cloud Platform segmented_control
                    # is treated as a fresh instantiation there too (its `default=`
                    # wins over any prior selection) - confirmed live via screen
                    # recording 2026-08-21: an AWS tenant's Dashboard button
                    # silently landed on the Azure dashboard instead, because
                    # BOTH of the app's usual "provider" persistence mechanisms
                    # (query param re-assertion, widget session_state) turned out
                    # not to survive switch_page specifically, unlike a plain
                    # sidebar-link click or refresh. A directly-assigned widget
                    # session_state key was tried first and raises
                    # StreamlitAPIException ("cannot be modified after the widget
                    # is instantiated") since the widget already rendered earlier
                    # in this same script run. _pending_provider is a plain,
                    # non-widget key instead - consumed once at the top of the
                    # script (see the "provider" persistence comment there) before
                    # the widget ever reads a default, so it can't be stale.
                    st.session_state["_pending_provider"] = selected_provider
                    # currency needs the identical relay (real bug found
                    # 2026-08-30 via a temporary debug caption on the
                    # sidebar: switching to INR on Home, then clicking
                    # this exact Dash button, landed on the Analyze page
                    # with currency back to USD - AND session_state's own
                    # "currency_selector_widget" entry had reset too, not
                    # just the query string, confirming switch_page()
                    # really does start a fresh script context here, same
                    # as it does for provider - the "reassert every
                    # rerun" fix for a plain sidebar-link click/refresh
                    # (below, in the sidebar section) never could have
                    # covered this path, since switch_page() doesn't carry
                    # THAT rerun's query params forward at all.
                    st.session_state["_pending_currency"] = selected_currency
                    st.switch_page(analyze_page)
                if b2.button("Manage", icon=":material/settings:", key=f"home_manage_{t.id}", width="stretch"):
                    st.session_state["_manage_tenant_id"] = t.id

    # Kept OUTSIDE the button's if-block and OUTSIDE the tenant loop above,
    # gated on session_state instead of the button's return value - a
    # button's clicked state only lasts the one rerun immediately after the
    # click, but every action inside _manage_tenant_dialog() ends in
    # st.rerun(). Calling it directly inside `if button:` meant the dialog
    # closed itself the instant you clicked anything inside it (real bug
    # found live 2026-08). This form persists across those internal reruns
    # and only clears via the dialog's own on_dismiss callback.
    manage_id = st.session_state.get("_manage_tenant_id")
    if manage_id is not None:
        active_t = next((tt for tt in tenants if tt.id == manage_id), None)
        if active_t is not None:
            _manage_tenant_dialog(active_t, tenant_mode)
        else:
            st.session_state["_manage_tenant_id"] = None

    # Required-roles reference and setup guidance live inside each tenant's
    # Manage dialog now, not here.


# ═══════════════════════════════════════════════════════════════════════════════
# MANAGE — USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
def _clear_manage_user_dialog_state():
    """on_dismiss callback (user closed the dialog via the X, Escape, or
    clicking outside) - same reason and shape as Tenant Management's
    _clear_manage_tenant_dialog_state: page_users() calls this dialog every
    rerun while _manage_user_id is set, not gated on a button's return
    value (a button's clicked state only lasts one rerun, and every action
    inside the dialog ends in st.rerun()) - without this callback the
    dialog would immediately reopen itself on the very next script run."""
    st.session_state["_manage_user_id"] = None
    st.session_state.pop("_user_mgmt_toast", None)


@st.dialog("Manage user", on_dismiss=_clear_manage_user_dialog_state)
def _manage_user_dialog(u, mode: str):
    """Added 2026-08-30 - real gap the user caught: User Management could
    list accounts and add new ones, but never edit an existing one at all
    (username, display name, password). Mirrors Manage Tenant's own dialog
    shape: separate forms for identity vs. credentials (a single form with
    two submit buttons would submit BOTH together, forcing a password
    change any time you just wanted to fix a typo'd display name), a
    toast-relay for post-save confirmations (st.success() immediately
    followed by st.rerun() never actually renders - see this file's own
    Manage Tenant fix for the same bug), and an Active/Inactive toggle -
    that badge already existed on the list below with no way to ever set
    it, a second pre-existing gap found while building this."""
    st.caption("Production" if mode == "live" else "Demo")

    _toast = st.session_state.pop("_user_mgmt_toast", None)
    if _toast:
        st.success(_toast)

    with st.form(f"mgmt_user_identity_{u.id}"):
        new_username = st.text_input("Username", value=u.username, autocomplete="username")
        new_display = st.text_input("Display name", value=u.display_name or u.username)
        identity_submitted = st.form_submit_button("Save", type="primary")
    if identity_submitted:
        try:
            update_user(u.id, mode, username=new_username, display_name=new_display)
            st.session_state["_user_mgmt_toast"] = "User details updated."
            st.rerun()
        except ValueError as e:
            st.error(str(e))

    st.divider()
    st.markdown("**Change password**")
    with st.form(f"mgmt_user_pw_{u.id}"):
        pw1 = st.text_input("New password", type="password", autocomplete="new-password")
        pw2 = st.text_input("Confirm new password", type="password", autocomplete="new-password")
        pw_submitted = st.form_submit_button("Update password", type="primary")
    if pw_submitted:
        if pw1 != pw2:
            st.error("Passwords don't match.")
        else:
            try:
                update_user_password(u.id, pw1, mode)
                st.session_state["_user_mgmt_toast"] = "Password updated."
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    # Real gap the user caught live, 2026-08-30: the last-active-account
    # guard in db/users.py only blocks dropping to ZERO active accounts -
    # it correctly allowed deactivating this account when a SECOND active
    # account still existed, but that's not the only real risk. Deactivating
    # your own currently-signed-in account is a separate, session-level
    # risk this dialog can see and that one can't: your session stays live
    # for now (session_state/the URL token don't get invalidated by this),
    # but the next time you needed to sign back in, you'd be locked out
    # with no one else's action required to trigger it. current_user is a
    # module-level global (set once near the top of this file via
    # require_login()) - reachable here via the same late-binding pattern
    # already used for every other module-level global this file relies on.
    is_self = (u.id == current_user["id"])
    _self_help = "You're signed in as this account - sign in as someone else first." if is_self else None

    st.divider()
    if u.is_active:
        if st.button("Deactivate account", icon=":material/block:", key=f"mgmt_user_deactivate_{u.id}", disabled=is_self, help=_self_help):
            # Both wrapped in try/except now, 2026-08-30 - set_user_active()/
            # delete_user() can both raise ValueError (the last-active-
            # account lockout guard added the same day); calling either
            # unhandled would crash the whole page instead of showing the
            # real reason as a normal, recoverable st.error().
            try:
                set_user_active(u.id, False, mode)
                st.session_state["_user_mgmt_toast"] = "Account deactivated."
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    else:
        # Redesigned 2026-08-30 - real gap the user caught: reactivating
        # used to just flip is_active back on, leaving the account's OLD
        # password (from before it was deactivated) as the only way back
        # in - if the person genuinely forgot it across the gap (the whole
        # reason `must_change_password` gets forced on reactivation is that
        # this app assumes they might have), they'd be stuck: verify_login()
        # still requires knowing that old password correctly before the
        # forced-change screen is ever reached, so there was no actual path
        # in for someone who'd forgotten it. Matches how Entra ID/AD really
        # handle a re-enabled account: an admin sets a fresh TEMPORARY
        # password as PART of reactivating it (relayed to the user
        # out-of-band - Slack, in person, however), not "their old one
        # still works, they'll just be forced to change it." The user signs
        # in with THIS temp password, then is still forced to pick their
        # own new one (clear_must_change_password=False here is what keeps
        # that second step required - update_user_password() would
        # otherwise clear it, correctly, for every other caller).
        st.caption("Set a temporary password for this user - they'll sign in with it, then be required to choose their own.")
        with st.form(f"mgmt_user_reactivate_form_{u.id}"):
            temp_pw = st.text_input("Temporary password", type="password", autocomplete="new-password")
            reactivate_submitted = st.form_submit_button("Reactivate & Set Temporary Password", type="primary")
        if reactivate_submitted:
            try:
                set_user_active(u.id, True, mode)
                update_user_password(u.id, temp_pw, mode, clear_must_change_password=False)
                st.session_state["_user_mgmt_toast"] = "Account reactivated with a temporary password - share it with the user directly."
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    st.divider()
    if st.button("Delete user", icon=":material/delete:", key=f"mgmt_user_delete_{u.id}", disabled=is_self, help=_self_help):
        # No toast on success, matching Manage Tenant's own "Delete tenant"
        # (no confirmation message there either) - a toast set here would
        # never actually render anyway, since it closes the dialog that
        # displays it (_manage_user_id below) on this same rerun. The row
        # disappearing from the list is confirmation enough, same as
        # tenant deletion already relies on.
        try:
            delete_user(u.id, mode)
            st.session_state["_manage_user_id"] = None
            st.rerun()
        except ValueError as e:
            st.error(str(e))


def page_users():
    hdr_l, hdr_r = st.columns([4, 1])
    with hdr_l:
        st.subheader(":material/group: Dashboard User Accounts")
    with hdr_r:
        # Same top-right toggle-button pattern as Tenant Management's own
        # "Add a new tenant", 2026-08-30 (real feedback - match that exact
        # placement/behavior) - the st.rerun() here matters for the same
        # reason it did there: _form_open is read at the top of this run,
        # before this click's effect is known, so without forcing an
        # immediate fresh run the button's own label lags one click behind.
        add_disabled = not is_live_mode
        _form_open = st.session_state.get("_show_add_user_form", False)
        if st.button(
            "Cancel" if _form_open else "Add a new user",
            icon=":material/close:" if _form_open else ":material/person_add:",
            use_container_width=True, type="secondary" if _form_open else "primary",
            disabled=add_disabled,
            help="Only available in Production Mode." if add_disabled else None,
        ):
            st.session_state["_show_add_user_form"] = not _form_open
            st.rerun()
    st.caption("Accounts that can sign in to this dashboard. Shared across everyone - not tied to a cloud tenant.")
    _finops_tag("Manage the FinOps Practice", "FinOps Practice Operations & Automation, Tools & Services")

    # Demo and live accounts are separate scopes now (db/schema.py) - this
    # page always reflects whichever scope the current session is in, same
    # as every other data read in the app. In Demo Mode this correctly shows
    # only the single fixed demo account with no "add a user" ability to
    # abuse - real account management only makes sense in Production mode.
    users_mode = "live" if is_live_mode else "demo"

    _toast_user = st.session_state.pop("_user_added_toast", None)
    if _toast_user:
        st.success(f"User '{_toast_user}' added.")

    users = list_users(mode=users_mode)
    with st.container(border=True):
        for u in users:
            ucols = st.columns([3, 3, 1.4, 1.2])
            ucols[0].markdown(f"**{u.display_name or u.username}** (`{u.username}`)")
            ucols[1].caption(f"Added {u.created_at[:10]} · Last login: {u.last_login_at[:10] if u.last_login_at else 'never'}")
            with ucols[2]:
                if u.is_active:
                    st.badge("Active", icon=":material/check_circle:", color="green")
                else:
                    st.badge("Inactive", icon=":material/radio_button_unchecked:", color="gray")
            with ucols[3]:
                if is_live_mode and st.button("Manage", icon=":material/settings:", key=f"user_manage_{u.id}", width="stretch"):
                    st.session_state["_manage_user_id"] = u.id

    # Kept OUTSIDE the button's if-block and OUTSIDE the loop above, gated
    # on session_state instead of the button's return value - same reason
    # as Manage Tenant's identical structure (see _manage_tenant_dialog's
    # caller for the full explanation): a button's clicked state only
    # lasts the one rerun immediately after the click, but every action
    # inside _manage_user_dialog() ends in st.rerun().
    manage_uid = st.session_state.get("_manage_user_id")
    if manage_uid is not None:
        active_u = next((uu for uu in users if uu.id == manage_uid), None)
        if active_u is not None:
            _manage_user_dialog(active_u, users_mode)
        else:
            st.session_state["_manage_user_id"] = None

    if not is_live_mode:
        st.info("Switch to **Production** mode to add or manage real user accounts - the demo account is fixed.", icon=":material/info:")
        return

    if st.session_state.pop("_reset_add_user_form", False):
        st.session_state["_show_add_user_form"] = False
        for _k in ("add_user_username", "add_user_display", "add_user_pw1", "add_user_pw2"):
            st.session_state.pop(_k, None)

    if st.session_state.get("_show_add_user_form"):
        with st.container(border=True):
            with st.form("add_user_form"):
                nu_username = st.text_input("Username", key="add_user_username", autocomplete="username")
                nu_display = st.text_input("Display name (optional)", key="add_user_display")
                nu_pw1 = st.text_input("Password", type="password", key="add_user_pw1", autocomplete="new-password")
                nu_pw2 = st.text_input("Confirm password", type="password", key="add_user_pw2", autocomplete="new-password")
                nu_submit = st.form_submit_button("Add User", icon=":material/person_add:", type="primary")
            if nu_submit:
                if not nu_username or not nu_pw1:
                    st.error("Username and password are both required.")
                elif nu_pw1 != nu_pw2:
                    st.error("Passwords don't match.")
                else:
                    # Password strength (length + upper/lower/digit/special)
                    # is now enforced INSIDE create_user() itself
                    # (db/users.py's shared validate_password_strength()) -
                    # this used to duplicate just the 8-char part of that
                    # rule inline here, which is exactly the kind of
                    # same-rule-copy-pasted-in-3-places drift this app has
                    # already been bitten by elsewhere. The ValueError below
                    # now carries the real, specific reason.
                    try:
                        create_user(nu_username, nu_pw1, nu_display, mode="live")
                        st.session_state["_reset_add_user_form"] = True
                        st.session_state["_user_added_toast"] = nu_username
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))


def page_help():
    """Help & Support - scoped deliberately, 2026-08-30, per the user's own
    call: Azure/AWS's own Help & Support is a full enterprise system (live
    chat, ticketing, docs search, community forums) with real backend
    infrastructure behind it that this app has no equivalent of. Matching
    that literally isn't the goal - what's actually missing, and what this
    page actually fixes, is a real onboarding gap: the Service
    Principal/IAM setup guidance already built for the Add tenant forms
    only appeared AFTER landing on that form, with no way for a first-time
    user in a fresh environment to see it beforehand. This page surfaces
    the exact same guidance (same helpers, not a rewritten copy) up front,
    reachable from the sidebar before a tenant even exists."""
    st.subheader(":material/help: Help & Support")
    st.caption("Getting started with your first Azure or AWS tenant connection.")
    _finops_tag("Manage the FinOps Practice", "FinOps Practice Operations & Automation, Tools & Services")

    st.markdown("#### Getting Started")
    st.caption("Same setup guidance shown on the Add a new tenant form - available here too, before you've connected anything.")
    az_tab, aws_tab = st.tabs(["☁️ Azure", "🟧 AWS"])
    with az_tab:
        _render_azure_sp_setup_guide(expanded=True)
    with aws_tab:
        _render_aws_iam_setup_guide(expanded=True)


def _split_sp_eligible(df_24x7: pd.DataFrame, azure_provider: bool):
    """Splits a 24x7-running slice into (eligible_df, excluded_df) using the
    real per-SKU Savings Plan rules (analysis/sp_eligibility.py) - e.g. an
    App Service Basic plan shouldn't count toward the Compute SP baseline at
    all, matching the same real-eligibility discipline built for RI Coverage.
    AWS isn't covered by this Azure-specific ruleset yet, so it passes through
    unfiltered."""
    if not azure_provider or df_24x7.empty:
        return df_24x7, df_24x7.iloc[0:0].assign(**{"SP Eligibility Note": []})
    elig = df_24x7.apply(lambda r: check_sp_eligibility(r["Resource Type"], r["SKU"]), axis=1)
    is_elig = elig.apply(lambda t: t[0])
    reason = elig.apply(lambda t: t[1])
    excluded_df = df_24x7[~is_elig].copy()
    excluded_df["SP Eligibility Note"] = reason[~is_elig]
    return df_24x7[is_elig], excluded_df


def _payg_blank_reason(resource_type: str, sku: str, is_free_limit_enabled: bool = False) -> str:
    """A blank PAYG cell can mean genuinely different things - conflating
    them into one generic 'Not RI/SP-metered' label (the old behavior) reads
    as a bug to anyone who doesn't already know why a specific resource has
    no hourly rate. Distinguishes: (0) a database enrolled in Azure's real
    free-limits program (added 2026-09-02, real feedback: this used to fall
    through to a technically-true-but-less-useful "Not eligible for RI" -
    Serverless genuinely isn't RI-eligible, but that's not why THIS resource
    shows no cost; a resource-level ARM property (properties.useFreeLimit,
    threaded through azure_conn/connector.py's KQL) is a real, static signal
    this app can check, unlike "is it STILL within the free quota this
    month", which would need actual usage/billing data this app doesn't
    ingest - so this is deliberately worded as "on the program", not "this
    month is free"), (1) genuinely free/ineligible tiers (Free App Service,
    Consumption-plan Functions) via the same real eligibility reasons
    already computed for the RI Coverage/Savings Plan tabs, (2) resources
    this app can't price per-instance at all (e.g. Storage, sold in 100TB+
    blocks) via sku_mapping.py's own documented reason, (3) a genuine,
    currently-unresolved pricing gap - kept distinct from the others so it
    doesn't get mistaken for 'this is fine, it's just free'."""
    if is_free_limit_enabled:
        return "On Azure's free-limits program (up to 100K vCore-seconds/month) - actual cost may be $0"
    # Check the pricing layer's OWN reason first - it's the most direct
    # explanation (e.g. Storage's "sold in 100TB+ blocks" note) and is more
    # specific than a generic eligibility message whenever both apply.
    plan = resolve_sku_query(resource_type, sku)
    if not plan.supported and plan.reason:
        return plan.reason
    ri_ok, ri_reason = check_eligibility(resource_type, sku)
    sp_ok, sp_reason = check_sp_eligibility(resource_type, sku)
    if not ri_ok and not sp_ok:
        # SP reasons in this codebase tend to have more specific per-SKU
        # detection (e.g. Y1/Y2/Y3 Consumption plan explicitly called out)
        # than RI's more general tier regex - prefer it when both apply.
        return f"Not eligible for RI or Savings Plan - {sp_reason}"
    if not ri_ok:
        return f"Not eligible for RI - {ri_reason}"
    if not sp_ok:
        return f"Not eligible for Savings Plan - {sp_reason}"
    return "Pricing data not available for this resource yet"


def _csv_download_button(df: pd.DataFrame, filename: str, key: str, label: str = "Download CSV", right_aligned: bool = False):
    """Consistent CSV export control for any real tenant-data table on this
    page (2026-08-30, real feedback: "export csv feature for all the
    section where table is there and it is required" - one shared helper
    so every table's export button behaves and looks identically, rather
    than each call site reimplementing st.download_button separately).
    `df` should be the exact DataFrame already shown (post display-
    formatting, e.g. "$1,234.56" strings) - what a user sees on screen is
    what they'd expect in the file, not a raw/differently-shaped export.
    `filename` excludes the .csv extension (added here). `key` must be
    unique per call site - Streamlit requires it for any widget that can
    appear more than once per rerun, which every one of this button's call
    sites can (e.g. AWS vs Azure, or a filtered/re-rendered table).

    right_aligned (2026-08-30 follow-up, real feedback + screenshot: the
    button belongs at the table's top-right, not floating below it, "for
    all the tabs SP, RI, etc"). Tables that already have their own
    filter-pill toolbar row (Inventory, RI Coverage's main table) instead
    slot a real Download CSV button directly into that row via a deferred
    column reference (see those call sites) - right_aligned is for every
    OTHER table here, which has no existing row to join, so this renders
    its own single-row placeholder immediately above wherever it's
    called - call this BEFORE the matching st.dataframe(...), not after.

    Uses a flex `justify-content: flex-end` wrapper, not a ratio-based
    st.columns([N, 1]) split - real bug caught live, 2026-08-30
    (screenshot: the Savings Plan pool's "Every resource" table sits
    inside a deliberately width-constrained expander, ~a few hundred px,
    not the page's full width - a fixed [6, 1] ratio split of THAT narrow
    container left barely 60-70px for the button, wrapping "Download CSV"
    across 4 lines). flex-end sizes the button to its own real content
    width and pushes it to the container's right edge regardless of how
    wide that container actually is - it can never wrap from a ratio
    squeeze, only from a container narrower than the button itself, an
    edge case none of this app's real tables hit."""
    if right_aligned:
        # The CSS class this maps to must be alphanumeric/underscore-safe,
        # but `key` itself doesn't have to be - two real call sites pass a
        # raw pool_label containing a space ("SageMaker AI", "Database
        # Services") straight through. Sanitized independently here rather
        # than trusting every call site to remember to pre-sanitize its own
        # key before passing it in.
        wrap_key = re.sub(r"[^0-9a-zA-Z_-]", "_", f"{key}_wrap")
        with st.container(key=wrap_key):
            st.markdown(
                f'<style>div.st-key-{wrap_key} {{ display: flex; justify-content: flex-end; }}</style>',
                unsafe_allow_html=True,
            )
            st.download_button(
                label, data=df.to_csv(index=False), file_name=f"{filename}.csv",
                mime="text/csv", icon=":material/download:", key=key,
            )
        return
    st.download_button(
        label, data=df.to_csv(index=False), file_name=f"{filename}.csv",
        mime="text/csv", icon=":material/download:", key=key,
    )


def _value_checklist(all_opts, key, defaults, search_label="Search values"):
    # A checkbox list, not st.multiselect - real bug seen live (video,
    # 2026-08-24): a multiselect's dropdown is a floating overlay tall
    # enough to cover the Apply/Cancel buttons beneath it (worse the
    # more options a field has, e.g. Resource Type), and it doesn't
    # close after a click, so there was no way to see what got picked
    # without dismissing the dropdown first. Checkboxes render inline -
    # nothing to overlap, and checked/unchecked state is visible
    # immediately. Bounded height keeps a long list (SKU, Resource
    # Type) from growing the popover indefinitely; the search box
    # narrows it further, same as Azure's own "Search values" field.
    #
    # Module-level (hoisted 2026-08-26 from inside _render_inventory_section)
    # so the VM Rightsizing tab can reuse the exact same filter-picker
    # pattern instead of a second copy - it only ever touches its own
    # params and st.session_state, no closure over the Inventory
    # section's variables, so the hoist is behavior-preserving.
    query = st.text_input(search_label, key=f"{key}_search", placeholder=f"🔍 {search_label}", label_visibility="collapsed")
    shown = [o for o in all_opts if query.strip().lower() in o.lower()] if query.strip() else all_opts

    # Excel's real AutoFilter "(Select All)" - a single master checkbox,
    # not two separate buttons (2026-08-24, matched against the video
    # the user shared of it): checked means every currently-shown item
    # is selected, unchecked means none are, and toggling it drives
    # every visible item at once. st.checkbox has no indeterminate
    # visual state, so a genuinely partial selection just reads as
    # unchecked here - the closest honest approximation Streamlit's
    # widget allows, not a full tri-state like Excel's filled square.
    # Wired with on_change (not a plain button) so the master and the
    # individual boxes below stay in sync with each other either way:
    # toggling the master cascades to every visible item, AND toggling
    # any individual item recomputes whether the master should still
    # show as checked - a bare button could only do the first half.
    master_key = f"{key}_master"

    def _sync_master():
        st.session_state[master_key] = bool(shown) and all(
            st.session_state.get(f"{key}_cb_{o}", o in defaults) for o in shown
        )

    def _apply_master():
        new_val = st.session_state[master_key]
        for o in shown:
            st.session_state[f"{key}_cb_{o}"] = new_val

    _sync_master()
    st.checkbox("(Select All)", key=master_key, on_change=_apply_master)

    with st.container(height=220):
        if not shown:
            st.caption("No matches.")
        for o in shown:
            st.checkbox(o, value=(o in defaults), key=f"{key}_cb_{o}", on_change=_sync_master)
    # Reads final selection from session_state across ALL options, not
    # just the currently search-filtered ones - a checkbox's state
    # persists in session_state even while hidden by the search text,
    # so narrowing then widening the search can't silently drop a
    # selection made before the search was typed.
    return [o for o in all_opts if st.session_state.get(f"{key}_cb_{o}", o in defaults)]


def _render_inventory_section(df: pd.DataFrame, key_prefix: str):
    """Renders the single, unified Inventory table (2026-08-23: previously
    called once per resource-type tab - removed per user feedback that a
    24+-tab horizontal scroll strip was harder to use than just filtering
    by Resource Type, which the filter bar already does for every other
    dimension; see the Resource Type entry in filter_specs below)."""
    if df.empty:
        st.caption("No resources in this category.")
        return

    disp = df.copy()
    # The real power state, not overridden with "Orphaned" text - real
    # feedback, 2026-08-25: Orphaned and "is it running" are two different
    # facts (a resource's power state vs. whether an active Reservation/
    # Savings Plan is going to waste because of it), and conflating them
    # here lost the actual power state entirely. Resource State is already
    # exactly "Running" or the provider's own real term for stopped
    # ("Stopped (deallocated)" for Azure, "Stopped" for AWS - see
    # azure_conn/connector.py's/aws/connector.py's _map_power_state /
    # _map_ec2_state), so this is a direct passthrough, not a re-derivation.
    #
    # No separate "Orphaned" column here (tried it, explicitly not wanted -
    # 2026-08-26: it read as clutter on the Inventory tab, and a stopped
    # resource's power state alone is enough signal at this level). The
    # underlying detection (Is Orphaned, analysis/engine.py's
    # compute_orphaned_status) is untouched and still real - it's just
    # consumed where it actually belongs: the RI Coverage/orphan-drain
    # math (run_waterfall, reservation_analysis) and the Maturity
    # Assessment's detection-capability score, not as an extra Inventory
    # tab field.
    disp["Status"] = disp["Resource State"]
    # A Stopped (deallocated) resource genuinely isn't accruing compute
    # charges - real gap caught live 2026-08-21: this table was showing
    # the full running rate for stopped resources regardless of state.
    # Both columns zero out for a stopped resource here.
    #
    # NOTE (2026-08-30): this same gap - summing PAYG Hourly Cost USD with
    # no Resource State == "Running" filter - was confirmed to ALSO be
    # present in the Home page's "Total PAYG Rate" KPI (_compute_portfolio_
    # kpis(), line ~914: inv["PAYG Hourly Cost USD"].sum(), no filter) -
    # found while investigating a Cost Analysis tab card that had the
    # identical gap (that card has since been removed entirely, real user
    # call, rather than fixed - see git history around 2026-08-30 for the
    # full reasoning). The Home KPI fix was deliberately deferred as its
    # own follow-up, not bundled into that pass - still open as of this
    # note.
    # Hourly PAYG column dropped entirely, 2026-08-30 (real feedback - not
    # needed for an inventory view, monthly is the figure that matters
    # here). It wasn't PURELY a duplicate of the monthly column though: for
    # a Running resource with no cached price, it was the only place
    # explaining WHY (free tier, an unpriceable service like Storage sold
    # in 100TB+ blocks, or a genuine pricing-cache gap - see
    # _payg_blank_reason's own docstring). That reason now feeds the
    # monthly column's own blank cells below instead of being dropped -
    # same information, one column instead of two.
    def _monthly_blank_reason(r):
        if r["Resource State"] != "Running" or r["PAYG Hourly Cost USD"]:
            return ""
        reason = _payg_blank_reason(r["Resource Type"], r["SKU"], r.get("Is Free Limit Enabled", False))
        # Cap raised 90 -> 150, 2026-09-02, real feedback: the 4 reasons an
        # actual tenant is likely to hit (Storage's 100TB+ note, App
        # Service Basic/Consumption-plan RI/SP notes, the new free-limits
        # note) are all 87-123 chars - all were getting cut mid-sentence at
        # the old 90-char cap even after the column itself was widened,
        # since widening the COLUMN doesn't un-truncate a STRING already
        # cut by this line. 150 shows every one of those in full. A
        # genuinely rare, much longer reason (checked across every real
        # reason string in analysis/ri_eligibility.py, analysis/
        # sp_eligibility.py, pricing/sku_mapping.py - a couple of edge
        # cases like AWS EC2 Mac instances/Azure-SSIS Integration Runtime
        # run up to ~265 chars) still gets truncated deliberately - showing
        # those in full would need a ~2000px column, which would wreck
        # this table's scannability for the sake of 2-3 rare edge cases.
        return (reason[:147] + "...") if len(reason) > 150 else reason

    disp["_monthly_blank_reason"] = disp.apply(_monthly_blank_reason, axis=1)
    # MONTH_HOURS (730, pricing/commitment_pricing.py) is the same constant
    # RI/Savings Plan annualization already uses - previously this used a
    # flat "* 30" (24hr/day x 30-day month = 720hrs), silently
    # INCONSISTENT with the commitment-pricing side of the same app, and a
    # small but real mismatch against AWS's own pricing calculator (which
    # uses 730, the true yearly average) - caught live 2026-08-21 when a
    # user compared this exact figure against AWS's calculator by hand.
    # Scaled by (Avg Daily Running Hours / 24) so a resource that isn't
    # running the full day still gets a proportional monthly estimate.
    is_running = disp["Resource State"] == "Running"
    est_monthly = disp["PAYG Hourly Cost USD"] * (disp["Avg Daily Running Hours"] / 24.0) * MONTH_HOURS

    # 2026-08-28: switched from a pre-formatted string column to a raw
    # numeric one so the table can render it as a real column_config
    # ProgressColumn (a mini bar per cell, scaled to the currently-filtered
    # rows' own max) instead of plain text - makes the biggest-cost
    # resources visible at a glance instead of requiring reading every row.
    # Stopped resources get an explicit 0.0 (a known fact, matching the old
    # "(stopped)" string's own reasoning) rather than NaN/blank; a running
    # resource with no cached pricing gets NaN so it renders blank, not a
    # misleading $0 bar - was the "—" placeholder before. The "(stopped)"
    # suffix itself is dropped: it's now redundant with the Power State
    # column, which this same round color-codes (see below), so a $0 bar
    # next to a colored "Stopped" cell doesn't need to say so twice.
    def _monthly_numeric(running: bool, x: float) -> float:
        if not running:
            return 0.0
        return float(x) if x else float("nan")

    _currency_mult = _inr_rate if selected_currency == "INR" else 1.0
    disp["Est. Monthly PAYG Cost"] = [
        _monthly_numeric(r, x) * _currency_mult for r, x in zip(is_running, est_monthly)
    ]

    # Headline strip - visual polish only, 2026-08-30 (real feedback: match
    # Recommendations' look). Same gradient-card language (ui/styling.py's
    # .rec-headline-card family, reused verbatim rather than a near-copy,
    # so a future palette tweak only has one place to change) as the one
    # real "at a glance" moment this tab was missing - total spend +
    # running/stopped counts, computed from the exact same disp/is_running
    # data the table itself renders, so it can't disagree with what's below
    # it. Approved via mockup (Artifact) before porting.
    _running_count = int(is_running.sum())
    _stopped_count = int(len(disp) - _running_count)
    _total_mo = float(pd.Series(
        [_monthly_numeric(r, x) for r, x in zip(is_running, est_monthly)]
    ).fillna(0.0).sum()) * _currency_mult
    st.markdown(
        '<div class="inv-headline">'
        f'<div class="inv-stat total"><span class="n fl-mono">{fmt(_total_mo, 2)}<span style="font-size:12px;">/mo</span></span><span class="lbl">Est. Total Spend</span></div>'
        '<div class="inv-divider"></div>'
        f'<div class="inv-stat"><span class="n fl-mono">{len(disp)}</span><span class="lbl">Resources</span></div>'
        '<div class="inv-divider"></div>'
        f'<div class="inv-stat"><span class="n fl-mono" style="color:#34D399;">{_running_count}</span><span class="lbl">Running</span></div>'
        '<div class="inv-divider"></div>'
        f'<div class="inv-stat"><span class="n fl-mono" style="color:#526279;">{_stopped_count}</span><span class="lbl">Stopped</span></div>'
        "</div>",
        unsafe_allow_html=True,
    )

    # Live mode: show which subscription a resource actually belongs to.
    # Real feedback 2026-09-02: this used to show the app's own internal
    # tenant label (CloudTenant.tenant_name, e.g. "Local azure tenant" or
    # "Prod Azure Tenant" - whatever this connection happens to be named)
    # instead of the real Azure subscription's own display name (e.g.
    # "Azure for Students") - misleading, since a "subscription" column
    # showing a tenant label isn't actually naming the subscription. The
    # real name IS already fetched and cached (TenantSubscription, synced
    # via "Sync subscriptions" in Manage Tenant) - looked up per-row by
    # subscription_id here instead of assuming one name for the whole
    # tenant, since a tenant can genuinely span multiple subscriptions.
    # Falls back to the tenant label only if that subscription hasn't
    # been synced yet (same fallback the old code always used).
    if is_live_mode and is_live_configured and active_tenant is not None:
        _subs_by_id = {
            s.subscription_id: s.subscription_name
            for s in list_subscriptions(selected_provider, tenant_mode, active_tenant.id)
            if s.subscription_name
        }
        disp["Subscription"] = disp["Subscription"].apply(
            lambda sid: f"{_subs_by_id.get(sid, active_tenant.tenant_name)} ({sid})" if sid else active_tenant.tenant_name
        )

    # Azure-Portal-style filter bar. Third iteration, 2026-08-23 - the
    # previous round's nested st.popover "pill" (click a field chip to
    # reveal its value-picker inside another popover) turned out to NOT be
    # reliably discoverable in practice - real user testing found no way to
    # actually pick values for an added filter. Simplified: an active
    # filter's value-picker now renders INLINE, directly below "Add
    # filter", with no extra click or nesting - guaranteed visible the
    # moment a field is added, at the cost of being slightly less
    # pixel-faithful to Azure's own collapsed-chip look.
    #   - Resource Type - added this round, replacing the old per-type tab
    #     strip (24+ tabs, "hard to scroll right and select" per direct
    #     feedback) - Azure Portal's own "All resources" page uses a Type
    #     FILTER, not per-type tabs, so this is closer to the reference,
    #     not just a workaround.
    #   - Status/Region/Subscription/OS/SKU: bounded, categorical, real
    #     filter candidates. Status is the real power state (Running /
    #     Stopped (deallocated) / Stopped) - Orphaned is tracked
    #     separately (Is Orphaned) but deliberately not surfaced as its
    #     own Inventory tab column/filter, see the comment on disp["Status"]
    #     above for why.
    #   - Resource Group (Azure) / Availability Zone (AWS): whichever
    #     actually has real data in this section, decided from the DATA
    #     itself (not a provider global) so it's correct even if a live
    #     tenant hasn't synced that field yet.
    #   - Resource ID/Name deliberately excluded - unbounded, unique per
    #     row; the dataframe's own built-in Search (toolbar button) already
    #     covers free-text lookup, a checklist filter would be useless here.
    #   - The two cost columns deliberately excluded - numeric, not
    #     categorical; sorting (click the column header, already built in)
    #     is the right tool, not a multi-select filter.
    extra_col, extra_label = None, None
    for col, label in (("Resource Group", "Resource Group"), ("Availability Zone", "Availability Zone")):
        if col in disp.columns and disp[col].fillna("").astype(str).str.strip().ne("").any():
            extra_col, extra_label = col, label
            break

    normal_filter_specs = [("Resource Type", "Resource Type"), ("Status", "Status"), ("Region", "Region"),
                            ("Subscription", "Subscription"), ("OS", "OS"), ("SKU", "SKU")]
    if extra_col:
        normal_filter_specs.append((extra_col, extra_label))

    # FOCUS View swaps the table to real FOCUS v1.2 column names - the
    # filter picker needs to offer fields matching what's actually on
    # screen there instead of the app's internal names, or what you can
    # filter by stops corresponding to what you can see (real report,
    # 2026-08-24: toggling FOCUS View on left "Add filter" still showing
    # internal names like Status/OS with nothing in the FOCUS table to
    # match them against). ChargeCategory/PricingUnit are excluded as
    # candidates since they're always a single constant value in this
    # per-resource view - not a real choice, same reason OS/Resource Group
    # are excluded from the normal list when they don't apply.
    disp["Service Category"] = disp["Resource Type"].apply(map_service_category)
    focus_filter_specs = [
        ("Resource Type", "ServiceName"), ("Service Category", "ServiceCategory"),
        ("SKU", "ResourceType"), ("Region", "RegionId"), ("Subscription", "BillingAccountId"),
    ]

    # Restore FOCUS View from the URL on a fresh session/refresh, before its
    # own session_state key exists - Azure Portal keeps a filtered/FOCUS-ed
    # view alive across a refresh; this app's toggle otherwise lives only in
    # st.session_state, which a hard reload wipes (confirmed live - every
    # refresh logs the whole session out too, not just this toggle).
    # Treated as a simple shared preference like currency, not scoped to
    # key_prefix - landing back in FOCUS View regardless of which provider
    # you refreshed on is the more coherent behavior, not a leak.
    focus_widget_key = f"{key_prefix}_focus"
    if focus_widget_key not in st.session_state:
        try:
            _raw_focus = st.query_params.get("inv_focus")
            if _raw_focus is not None:
                st.session_state[focus_widget_key] = (_raw_focus == "1")
        except Exception:
            pass

    focus_view_now = st.session_state.get(f"{key_prefix}_focus", False)
    filter_specs = focus_filter_specs if focus_view_now else normal_filter_specs
    # Spans BOTH vocabularies, not just the currently-active one: a filter
    # created before a FOCUS View toggle must keep resolving by its real
    # underlying column afterward, rather than KeyError-ing because its
    # label isn't among the set currently being offered for NEW filters.
    label_to_col = {label: col for col, label in normal_filter_specs + focus_filter_specs}

    # NaN-safe normalization (real bug caught 2026-08-23: a blank/NaN cell
    # in a sparse column like Resource Group used to silently disappear
    # from the table entirely, since NaN never matches .isin() against a
    # list of real values, even in a filter's "untouched" default state -
    # confirmed live, 31 of 33 Azure demo resources would vanish the moment
    # that filter rendered). Normalizes blanks to a literal "(Not set)"
    # sentinel so they're just another selectable category - included by
    # default, and lets someone explicitly filter for "only resources
    # missing this field" if they want to, same as picking any other value.
    def _norm(v):
        return str(v).strip() if pd.notna(v) and str(v).strip() else "(Not set)"

    all_cols = ["Resource ID", "Resource Name", "Subscription", "Resource Type",
                "Status", "Region", "Resource Group", "Availability Zone", "OS", "SKU",
                "Est. Monthly PAYG Cost"]
    all_cols = [c for c in all_cols if c in disp.columns]
    # Resource ID hidden by default (toggle back on if needed); of Resource
    # Group/Availability Zone, only the one actually populated for this
    # provider (extra_col, computed above) defaults on - the other is a
    # different cloud's concept and would just be a column of blanks.
    _other_scope_col = ({"Resource Group", "Availability Zone"} - {extra_col}) if extra_col else set()
    default_cols = [c for c in all_cols if c != "Resource ID" and c not in _other_scope_col]

    # Same show/hide picker for FOCUS View's own column set (real report,
    # 2026-08-24: Columns was simply unavailable there) - FOCUS_COLUMN_
    # DEFINITIONS already lists every real FOCUS v1.2 column name in order,
    # so this doesn't need actual row data to know what's pickable, only
    # to render the table once filtering has happened later.
    focus_all_cols = [c for c, _, _ in FOCUS_COLUMN_DEFINITIONS]
    focus_default_cols = focus_all_cols

    # Real "Filter results" panel, rebuilt properly this round (2026-08-23)
    # after direct confirmation - on BOTH local and the deployed app, ruling
    # out a stale-deployment explanation - that the previous inline-
    # multiselect version still gave no working way to pick a filter's
    # values. Uses st.form so a filter only takes effect on an explicit
    # "Apply" click, matching Azure's own Filter/Value/Apply-Cancel modal
    # structurally, not just visually - state lives explicitly in
    # st.session_state as a list of {label, values} dicts, mutated ONLY
    # inside a form's submit handler followed by st.rerun(), so there's no
    # ambiguity about whether a selection "took" the way a bare reactive
    # widget's return value could leave open.
    def _filter_opts(label):
        col = label_to_col[label]
        normalized = disp[col].apply(_norm)
        return normalized, sorted(normalized.unique().tolist())

    filters_state_key = f"{key_prefix}_active_filters_state"
    if filters_state_key not in st.session_state:
        # Restore from the URL on a fresh session/refresh - Azure Portal
        # keeps filters alive across a refresh via the URL; this app's
        # filters otherwise live only in st.session_state, which a hard
        # reload wipes (confirmed live - every refresh logs the whole
        # session out too, not just filters). Guarded by key_prefix so a
        # URL saved while viewing a different provider/mode combo can't
        # leak its filters into this one.
        _restored_filters = []
        try:
            _raw_filters = st.query_params.get("inv_filters")
            if _raw_filters:
                _filters_payload = json.loads(_raw_filters)
                if _filters_payload.get("kp") == key_prefix:
                    _restored_filters = _filters_payload.get("f", [])
        except Exception:
            _restored_filters = []
        st.session_state[filters_state_key] = _restored_filters
    active_filters = st.session_state[filters_state_key]
    # Self-healing prune: drops any filter whose label no longer exists in
    # this app's vocabulary (e.g. "Orphaned", removed as its own filter
    # 2026-08-26) - without this, a filter persisted in the URL or
    # session_state from before that change would KeyError the first time
    # _filter_opts() tried to resolve it, instead of just quietly no longer
    # applying.
    _valid_labels = set(label_to_col.keys())
    if any(f["label"] not in _valid_labels for f in active_filters):
        active_filters[:] = [f for f in active_filters if f["label"] in _valid_labels]

    # st.columns() reserves each column's full ratio-of-row width even when
    # its content (a small popover button) is far narrower, which is what
    # produced the wide dead gaps between pills the user flagged live. This
    # CSS override makes every column in this bar shrink to its content's
    # actual width instead, so pills/buttons pack together left-aligned -
    # like Azure Portal's own filter bar - with the column ratios passed to
    # st.columns() below no longer mattering.
    with st.container(key=f"{key_prefix}_filter_bar"):
        st.markdown(
            f"""<style>
            div.st-key-{key_prefix}_filter_bar div[data-testid="stHorizontalBlock"] {{ gap: 0.5rem; }}
            div.st-key-{key_prefix}_filter_bar div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {{
                width: fit-content !important; flex: 0 0 auto !important; min-width: 0 !important;
            }}
            /* Bordered card wrapper around this whole toolbar was tried
            2026-08-28 and reverted per direct feedback - back to plain
            floating buttons on the bare page background, keeping only the
            pill-shaped popover buttons and the right-aligned Columns fix
            below. stPopoverButton confirmed real via this Streamlit
            build's own JS bundle (index.BvGIeCyC.js). */
            div.st-key-{key_prefix}_filter_bar button[data-testid="stPopoverButton"] {{
                border-radius: 999px !important; border-color: #263349 !important;
                transition: border-color 0.15s ease, background-color 0.15s ease, color 0.15s ease;
            }}
            div.st-key-{key_prefix}_filter_bar button[data-testid="stPopoverButton"]:hover {{
                border-color: rgba(96,165,250,.5) !important; color: #60A5FA !important;
            }}
            /* Visual-only refresh, 2026-08-30 (real feedback: match
            Recommendations' look) - distinguishes an ACTION button ("Add
            filter", still visible even when active_filters is empty) from
            an APPLIED filter pill (a real, currently-set value), same
            "action vs. state" distinction Recommendations' pills already
            make. Scoped to filter_row's OWN key (st-key-{key_prefix}_filter_row,
            given to filter_row_container below), not nth-of-type - real
            bug found live, 2026-08-30 (screenshot showed "Add filter"
            hard-left and every applied pill pushed hard-right, confirmed
            via direct DOM inspection): `div[data-testid="stHorizontalBlock"]
            :nth-of-type(N)` does NOT mean "the Nth horizontal block in this
            container" the way the original "push Columns right" rule
            below assumed - Streamlit wraps EVERY st.columns() call in its
            own individual parent div, so EVERY stHorizontalBlock is
            trivially "the 1st (and only) div of its type" within its own
            micro-scope, and :nth-of-type(1) silently matched ALL of them,
            not just settings_row's. Confirmed live via
            getBoundingClientRect() + parentElement inspection before
            fixing - not guessed. Giving each row (settings_row,
            filter_row) its own real container key sidesteps the whole
            nth-of-type miscounting problem entirely - nth-of-type is still
            fine for STCOLUMNS within one row below, since those genuinely
            are siblings sharing one direct parent (confirmed the same way). */
            div.st-key-{key_prefix}_filter_row div[data-testid="stColumn"]:nth-of-type(1) button[data-testid="stPopoverButton"] {{
                border-style: dashed !important;
            }}
            div.st-key-{key_prefix}_filter_row div[data-testid="stColumn"]:nth-of-type(n+2) button[data-testid="stPopoverButton"] {{
                background-color: rgba(96,165,250,.1) !important; border-color: rgba(96,165,250,.3) !important;
                color: #BFDBFE !important;
            }}
            div.st-key-{key_prefix}_filter_row div[data-testid="stColumn"]:nth-of-type(n+2) button[data-testid="stPopoverButton"]:hover {{
                background-color: rgba(96,165,250,.18) !important; border-color: rgba(96,165,250,.55) !important; color: #F1F5F9 !important;
            }}
            /* Download CSV pushed to the far right of the SAME row as "Add
            filter" (2026-08-30, real feedback + screenshot circling the
            empty space to the right of this exact row) - :last-of-type,
            not a fixed nth-of-type index, since n_pills (and therefore
            this row's real column count) varies with how many filters are
            active. Same margin-left:auto flex trick already proven for
            "push Columns right" on settings_row below - fit-content
            columns don't stretch to fill the row, so the auto margin on
            the last one absorbs 100% of the leftover space and shoves
            just that column to the edge, leaving Add filter/pills packed
            left as before. */
            div.st-key-{key_prefix}_filter_row div[data-testid="stColumn"]:last-of-type {{
                margin-left: auto !important;
            }}
            /* Push "Columns" to the right edge of its row, away from FOCUS
            View (real feedback, 2026-08-28) - CSS-only (margin-left: auto
            on a flex item pushes it to the far edge regardless of the
            flex:0 0 auto rule above), deliberately NOT reordering the
            underlying st.toggle()/popover() calls: FOCUS View must stay
            the first widget instantiated in this function or its state
            gets silently reset by any filter button's st.rerun() (real bug,
            documented below at settings_row). Scoped to settings_row's own
            key (st-key-{key_prefix}_settings_row) - see the comment above
            explaining why nth-of-type(1) alone doesn't reliably mean "just
            this row" and was actually leaking into filter_row too. */
            div.st-key-{key_prefix}_settings_row div[data-testid="stColumn"]:nth-of-type(2) {{
                margin-left: auto !important;
            }}
            </style>""",
            unsafe_allow_html=True,
        )

        # Streamlit popovers don't close themselves after an in-popover
        # button click - they stay open until the user clicks elsewhere,
        # which read as "I have to click Cancel/away every time" (real
        # feedback, 2026-08-24). There's no direct "close" call, but a
        # popover re-mounts closed when its own `key` changes, so each
        # dismissing action (Apply/Cancel/Done) bumps a small per-popover
        # counter used in that popover's key before rerunning - the next
        # render is a fresh, closed instance.
        def _bump(gen_key):
            st.session_state[gen_key] = st.session_state.get(gen_key, 0) + 1

        # Bumping the popover's own key closes it (a fresh instance mounts
        # closed), but that alone left a _value_checklist's search box
        # showing leftover text after reopening - session_state.pop() on the
        # search key didn't reliably reset the already-mounted input's
        # on-screen value (reported live: searched "region" in Columns,
        # applied, reopened, "region" was still sitting in the box even
        # though the list itself was no longer filtered by it). The robust
        # fix is the same trick as the popover itself: fold the generation
        # counter into the checklist's own key too, so a bump forces a
        # genuinely new search box and checkboxes, not a state-side reset of
        # an old one.

        # FOCUS View and Columns render FIRST, before Add filter/the filter
        # pills below - not just a layout choice. Streamlit prunes a
        # widget's session_state if that widget hasn't been instantiated
        # yet in the CURRENT script run at the moment st.rerun() fires -
        # and Apply/Cancel/✕ on a filter all call st.rerun() explicitly.
        # With the FOCUS toggle defined AFTER those buttons in the code (as
        # it originally was), clicking any of them silently reset FOCUS
        # View back off mid-session, even though the toggle still visually
        # showed on - confirmed live (video, 2026-08-24: filtering while in
        # FOCUS View made the table quietly fall back to internal column
        # names) and isolated with a debug probe: Columns' "Done" button,
        # defined AFTER the toggle, never reset it; every filter button,
        # defined BEFORE it, always did. Registering the toggle earlier
        # than anything that can call st.rerun() is the actual fix.
        settings_row_container = st.container(key=f"{key_prefix}_settings_row")
        settings_row = settings_row_container.columns(2)
        with settings_row[0]:
            focus_view = st.toggle(":material/center_focus_strong: FOCUS View", key=f"{key_prefix}_focus", help="Show columns mapped to the FinOps Open Cost & Usage Specification (FOCUS) instead of the app's internal display names.")
        # Re-asserted every rerun, not just once - same "provider"/"currency"
        # fix pattern used in the sidebar (see the comment there): a query
        # param only survives Streamlit's sidebar nav links if it's
        # rewritten on every single script run.
        try:
            st.query_params["inv_focus"] = "1" if focus_view else "0"
        except Exception:
            pass

        # Columns adapts to whichever mode is active - same picker, pointed
        # at FOCUS's own column set when FOCUS View is on and the app's
        # internal columns otherwise. Every key below is suffixed with
        # mode_tag so each mode remembers its OWN picks (and its own search
        # text/popover-open state) independently - switching the toggle
        # shouldn't carry one mode's leftover UI state into the other.
        mode_tag = "focus" if focus_view else "normal"
        active_all_cols = focus_all_cols if focus_view else all_cols
        active_default_cols = focus_default_cols if focus_view else default_cols
        chosen_cols = active_default_cols
        with settings_row[1]:
            # Unlike the filter checklists below, a column checkbox IS the
            # live, persisted setting (no separate Apply/commit step) - so
            # unlike `defaults=[]`/`defaults=f["values"]` there, the default
            # fed into each new generation has to be "whatever was picked
            # last", tracked here explicitly, or bumping the generation to
            # fix the search-box staleness would also reset every checkbox
            # back to the hardcoded app default and silently discard the
            # user's choice.
            chosen_key = f"{key_prefix}_chosen_columns_{mode_tag}"
            if chosen_key not in st.session_state:
                # Restore from the URL on a fresh session/refresh, guarded
                # by key_prefix so a URL saved while viewing a different
                # provider can't leak its column choice into this one -
                # same reasoning and pattern as the filters restore below.
                _restored_cols = None
                try:
                    _raw_cols = st.query_params.get("inv_cols")
                    if _raw_cols:
                        _cols_payload = json.loads(_raw_cols)
                        if _cols_payload.get("kp") == key_prefix:
                            _restored_cols = _cols_payload.get(mode_tag)
                except Exception:
                    _restored_cols = None
                st.session_state[chosen_key] = _restored_cols if _restored_cols else active_default_cols
            cols_gen_key = f"{key_prefix}_colspopover_gen_{mode_tag}"
            cols_gen = st.session_state.get(cols_gen_key, 0)
            # A separate counter just for the checklist, decoupled from the
            # popover's own key: only "Done" needs to force a fresh search
            # box (the one thing session_state.pop() didn't reliably clear
            # visually) - it shouldn't also happen on every checkbox click,
            # which is otherwise a plain in-place update under the same,
            # unchanged key.
            cols_list_gen_key = f"{key_prefix}_colslist_gen_{mode_tag}"
            cols_list_gen = st.session_state.get(cols_list_gen_key, 0)
            with st.popover("Columns", icon=":material/view_column:", key=f"{key_prefix}_colspopover_{mode_tag}_{cols_gen}"):
                # Same single-column scrollable checklist (+ search + Select
                # all/Unselect all) as the filter Value picker below, reusing
                # _value_checklist directly rather than the earlier
                # 3-column checkbox grid - requested explicitly so Columns
                # matches the pattern already built for filters, not a
                # second, different one.
                st.caption("Columns to display")
                picked = _value_checklist(active_all_cols, key=f"{key_prefix}_colspicker_{mode_tag}_{cols_list_gen}", defaults=st.session_state[chosen_key], search_label="Search columns")
                st.session_state[chosen_key] = picked
                # Column visibility already applies live - this button
                # exists only to close the popover once picking is done,
                # same "Done" affordance requested for filters. Bumps both
                # counters: the popover's own (to close it) and the
                # checklist's (so the search box is blank next open too).
                if st.button("Done", width="stretch", key=f"{key_prefix}_cols_done_{mode_tag}"):
                    _bump(cols_gen_key)
                    _bump(cols_list_gen_key)
                    st.rerun()
        if picked:
            chosen_cols = picked

        # Re-asserted every rerun, not just once - same reasoning as
        # "provider"/"currency"/inv_focus above. Reads BOTH modes' current
        # session_state (not just the active one) so switching FOCUS View
        # mid-session and refreshing later doesn't lose whichever mode
        # isn't currently showing.
        try:
            st.query_params["inv_cols"] = json.dumps({
                "kp": key_prefix,
                "normal": st.session_state.get(f"{key_prefix}_chosen_columns_normal"),
                "focus": st.session_state.get(f"{key_prefix}_chosen_columns_focus"),
            })
        except Exception:
            pass

        n_pills = len(active_filters)
        filter_row_container = st.container(key=f"{key_prefix}_filter_row")
        # +2, not +1 - the extra trailing slot is the Download CSV button's
        # placeholder (dl_col below), pushed to the row's far right via the
        # :last-of-type CSS rule above. Streamlit column objects are real
        # container references, not one-shot render calls - `dl_col` is
        # written into much further down (2026-08-30), once the actual
        # display DataFrame (FOCUS or normal mode) is finally ready, but it
        # still renders in this exact grid slot regardless of when in the
        # script that happens - the same deferred-container pattern this
        # app already uses for kpi_cols/grid_cols elsewhere.
        filter_row = filter_row_container.columns(n_pills + 2)
        dl_col = filter_row[n_pills + 1]
        with filter_row[0]:
            add_gen_key = f"{key_prefix}_addfilter_gen"
            add_gen = st.session_state.get(add_gen_key, 0)
            with st.popover("Add filter", icon=":material/add:", key=f"{key_prefix}_addfilter_popover_{add_gen}"):
                available = [l for _, l in filter_specs if l not in [f["label"] for f in active_filters]]
                if not available:
                    st.caption("All filterable fields are already added.")
                else:
                    st.markdown("**Filter results**")
                    # The field selector must stay OUTSIDE the form: st.form
                    # only reports its contents on submit, so a selectbox
                    # inside the form would leave the Value list showing
                    # stale options until a second Apply click - a real bug
                    # caught in review before shipping this round. Keeping
                    # it reactive here means picking a field immediately
                    # refreshes the Value options below, matching Azure's
                    # own live-updating modal.
                    pending_key = f"{key_prefix}_pending_filter_field"
                    if st.session_state.get(pending_key) not in available:
                        st.session_state[pending_key] = available[0]
                    new_label = st.selectbox("Filter", available, key=pending_key)
                    _, opts = _filter_opts(new_label)
                    # defaults=[] is correct every generation - "Add filter"
                    # always starts a value picker from scratch, so there's
                    # no prior selection that needs carrying into a fresh key.
                    new_values = _value_checklist(opts, key=f"{key_prefix}_addfilter_{new_label}_{add_gen}", defaults=[])
                    fc1, fc2 = st.columns(2)
                    if fc1.button("Apply", type="primary", width="stretch", key=f"{key_prefix}_addfilter_apply_{new_label}"):
                        active_filters.append({"label": new_label, "values": new_values})
                        _bump(add_gen_key)
                        st.rerun()
                    if fc2.button("Cancel", width="stretch", key=f"{key_prefix}_addfilter_cancel_{new_label}"):
                        _bump(add_gen_key)
                        st.rerun()

        for i, f in enumerate(list(active_filters)):
            with filter_row[i + 1]:
                summary = "all" if not f["values"] else (f["values"][0] if len(f["values"]) == 1 else f"{len(f['values'])} selected")
                edit_gen_key = f"{key_prefix}_editfilter_gen_{i}"
                edit_gen = st.session_state.get(edit_gen_key, 0)
                # A direct X next to the pill, not just the Remove filter
                # button buried inside the popover - removing a filter
                # shouldn't require opening its panel first. The CSS on the
                # filter_bar container (shrink columns to content) already
                # applies to this nested row too, so the pill + X still pack
                # tightly together rather than spreading across the row.
                pill_col, x_col = st.columns(2)
                with x_col:
                    if st.button("✕", key=f"{key_prefix}_editfilter_x_{i}", help=f"Remove {f['label']} filter"):
                        active_filters.pop(i)
                        st.rerun()
                with pill_col, st.popover(f"{f['label']} equals {summary}", key=f"{key_prefix}_editfilter_popover_{i}_{edit_gen}"):
                    _, opts = _filter_opts(f["label"])
                    st.markdown("**Filter results**")
                    # defaults=f["values"] is always the current committed
                    # value, so a fresh generation's checkboxes rebuild
                    # correctly seeded from it every time - no separate
                    # reset step needed.
                    new_values = _value_checklist(opts, key=f"{key_prefix}_editfilter_{i}_{edit_gen}", defaults=f["values"])
                    fc1, fc2 = st.columns(2)
                    if fc1.button("Apply", type="primary", width="stretch", key=f"{key_prefix}_editfilter_apply_{i}"):
                        f["values"] = new_values
                        _bump(edit_gen_key)
                        st.rerun()
                    if fc2.button("Remove filter", width="stretch", key=f"{key_prefix}_editfilter_remove_{i}"):
                        active_filters.pop(i)
                        st.rerun()

    # Re-asserted every rerun, not just once - same "provider"/"currency"
    # fix pattern used in the sidebar (see the comment there): a query
    # param only survives Streamlit's sidebar nav links if it's rewritten
    # on every single script run, not just at the moment it was first set.
    # Placed here (after every Apply/Remove button above, which halts the
    # run via st.rerun() the instant one fires) so active_filters is always
    # its final, settled value for this render before being written out -
    # never a stale mid-click snapshot.
    try:
        st.query_params["inv_filters"] = json.dumps({"kp": key_prefix, "f": active_filters})
    except Exception:
        pass

    mask = pd.Series(True, index=disp.index)
    for f in active_filters:
        normalized, opts = _filter_opts(f["label"])
        active_vals = f["values"] if f["values"] else opts
        mask &= normalized.isin(active_vals)
    filtered = disp[mask]

    if focus_view:
        raw_filtered = df.loc[filtered.index]
        focus_df = to_focus_view(raw_filtered, selected_provider)
        with st.expander(f"ℹ️ FOCUS v{FOCUS_SPEC_VERSION} column reference", expanded=False):
            st.caption(f"Columns below follow the [FinOps Open Cost & Usage Specification]({FOCUS_SPEC_URL}) v{FOCUS_SPEC_VERSION}. Each is mapped from this app's internal schema as noted.")
            # Explicit widths - real bug caught live, 2026-08-30: this table
            # had none at all, so its longest "How it's populated here"
            # values got clipped (screenshot showed ChargeCategory's own
            # explanation cut off mid-sentence). Sized to this table's own
            # real longest values, same rule already established for every
            # other table in this app. The trailing note that used to sit
            # below this table (BilledCost/EffectiveCost) was removed the
            # same round - real feedback: it duplicated the BilledCost row
            # right above almost word-for-word, and its EffectiveCost
            # mention pointed at a column that isn't shown anywhere in this
            # table at all (deliberately excluded - see focus_mapping.py's
            # own module docstring for why), so it referenced something
            # impossible to actually find.
            st.dataframe(
                pd.DataFrame(FOCUS_COLUMN_DEFINITIONS, columns=["FOCUS Column", "Spec Definition", "How it's populated here"]),
                hide_index=True, width="stretch", key=f"{key_prefix}_focus_reference_table",
                column_config={
                    "FOCUS Column":              st.column_config.TextColumn(width=150),
                    "Spec Definition":            st.column_config.TextColumn(width=330),
                    "How it's populated here":    st.column_config.TextColumn(width=300),
                },
            )
        # Explicit, mode-specific key on both this and the normal-mode
        # table below - without one, st.dataframe's identity is inferred
        # from its position among the OTHER elements in this container,
        # and FOCUS mode renders a different number of elements above it
        # (the reference-table expander) than normal mode does. That
        # mismatch left a stale table on screen instead of replacing it
        # when toggling FOCUS View while a filter was active - a real,
        # persistent (not one-frame-flicker) bug caught on video,
        # 2026-08-24: both the old internal-schema table AND the new FOCUS
        # table stayed visible stacked on top of each other.
        st.dataframe(
            focus_df[chosen_cols], hide_index=True, width="stretch", key=f"{key_prefix}_table_focus",
            column_config={
                "ListUnitPrice": st.column_config.NumberColumn("ListUnitPrice", format="$%.4f"),
                "ListCost":      st.column_config.NumberColumn("ListCost", format="$%.2f"),
                "BilledCost":    st.column_config.NumberColumn("BilledCost", format="$%.2f"),
            },
        )
        with dl_col:
            _csv_download_button(focus_df[chosen_cols], f"{selected_provider.lower()}_inventory_focus", key=f"{key_prefix}_dl_focus")
        return

    # Blank/NaN cells (e.g. Resource Group on an AWS row, or vice versa)
    # otherwise render as a bare "None" - matches the "N/A" placeholder OS
    # already uses elsewhere in this same table instead of leaving it to
    # pandas' default. Scoped to this display copy only, not `disp`/
    # `filtered`, so it can't interfere with the filter/mask logic above,
    # which relies on real NaN to detect "(Not set)".
    show_df = filtered[chosen_cols].copy()
    for _blank_col in ("Resource Group", "Availability Zone"):
        if _blank_col in show_df.columns:
            show_df[_blank_col] = show_df[_blank_col].apply(lambda v: str(v).strip() if pd.notna(v) and str(v).strip() else "N/A")

    # Formatted back into a plain string here, not left as NumberColumn -
    # this column can't be purely numeric anyway: a blank cell (Running,
    # no cached price) now shows the real reason why instead of a bare
    # "—" (2026-08-30 - see this section's own comment above), and
    # column_config has no way to make a column numeric for some rows and
    # text for others. Values are already currency-converted
    # (disp["Est. Monthly PAYG Cost"] above), so this must NOT call fmt()
    # again - that would double-convert for INR.
    _currency_symbol = "₹" if selected_currency == "INR" else "$"
    if "Est. Monthly PAYG Cost" in show_df.columns:
        _blank_reasons = filtered["_monthly_blank_reason"].reindex(show_df.index) if "_monthly_blank_reason" in filtered.columns else None
        show_df["Est. Monthly PAYG Cost"] = [
            f"{_currency_symbol}{v:,.2f}" if pd.notna(v)
            else (_blank_reasons.loc[idx] if _blank_reasons is not None and _blank_reasons.loc[idx] else "—")
            for idx, v in show_df["Est. Monthly PAYG Cost"].items()
        ]

    # Color-code Power State so "Running" vs "Stopped" reads at a glance
    # without adding a new column - pandas.Styler is explicitly supported
    # as st.dataframe's `data` input (confirmed via this Streamlit build's
    # own st.dataframe docstring) and composes fine with column_config
    # below, which operates on column identity/formatting, not the cell
    # styling values. .map() (not the deprecated .applymap()) since this
    # app's pandas (3.0.5) has already dropped it.
    def _status_color(val):
        if val == "Running":
            return "color: #34D399;"
        if isinstance(val, str) and val.startswith("Stopped"):
            return "color: #64748B;"
        return ""

    styled_df = show_df.style.map(_status_color, subset=["Status"]) if "Status" in show_df.columns else show_df

    st.dataframe(
        styled_df, hide_index=True, width="stretch", key=f"{key_prefix}_table_normal",
        row_height=42,  # slightly taller than the grid's one-line default - approved via mockup, real param confirmed on st.dataframe's own docstring
        column_config={
            # Pinned + width tuning (2026-08-28) - approved via mockup
            # first. pinned/width confirmed real params on TextColumn's own
            # docstring in this Streamlit build. Resource Name pinned so
            # it's still visible while scrolling right through the other
            # ~10 columns, and given an explicit width since a wider (not
            # narrower) width matches this column's own real values.
            # "Service" (SKU too) similarly widened on purpose. Every other
            # column below has NO width override - real bug caught live,
            # 2026-08-28: forcing width="small" (75px) onto columns like
            # "Power State"/"Est. Monthly PAYG Cost" clipped their OWN
            # header label, which is longer than 75px, since that override
            # replaces Streamlit's smart default (fit to the longer of
            # header-or-value) with a fixed size smaller than the header
            # itself. Round 2 (2026-08-28): leaving width unset didn't
            # actually fix it either - "Power State"'s real longest value
            # ("Stopped (deallocated)", 21 chars, azure_conn/connector.py's
            # _map_power_state) still got clipped even though it's longer
            # than the 11-char header, so width=None isn't reliably
            # scanning full column content. Every column below now gets an
            # explicit width sized to whichever is actually longer - this
            # app's own real longest value or the header label - not a
            # generic guess.
            "Resource Name":          st.column_config.TextColumn(pinned=True, width=200),
            # 140 -> 420, 2026-09-02: this cell's real content changed (see
            # the Subscription-name fix above) from a short tenant label to
            # "{real subscription name} ({subscription id GUID})" - the
            # GUID alone is 36 chars, plus " ()" plus a variable-length
            # subscription name on top, routinely 50-60+ chars total. 140px
            # was never sized for that.
            "Subscription":           st.column_config.TextColumn(width=420),
            "Resource Type":          st.column_config.TextColumn("Service", width=220),
            "Status":                 st.column_config.TextColumn("Power State", width=170),
            "Region":                 st.column_config.TextColumn(width=110),
            "Resource Group":         st.column_config.TextColumn(width=120),
            "Availability Zone":      st.column_config.TextColumn(width=120),
            "OS":                     st.column_config.TextColumn(width=90),
            "SKU":                    st.column_config.TextColumn(width=140),
            # Widened 140 -> 480, 2026-08-30 - real bug seen live on a
            # production tenant: this column isn't always a short $ figure.
            # A "Running, no cached price" row shows a full explanatory
            # sentence instead (_payg_blank_reason() above, capped at 90
            # chars) - e.g. "Not eligible for RI and Savings Plan - Storage
            # is sold in 100TB+ blocks...". 140px was sized for the $ case
            # only and clipped the real longest value mid-sentence. Sized to
            # that 90-char cap using this table's own established ratio
            # (~5.4px/char, from "Resource Type" 220px/41-char real value).
            #
            # 480 -> 620, 2026-09-02: real feedback, seen live - even the
            # already-90-char-capped reason text was still visibly clipped
            # at 480px. The 5.4px/char ratio (derived from a DIFFERENT
            # column, "Resource Type") apparently doesn't hold for this
            # column's actual rendering - cell padding/font differences
            # this app has no way to measure without live browser
            # inspection (not done per standing instruction). Widened with
            # real headroom (~6.9px/char at the same 90-char cap) rather
            # than re-deriving an exact ratio that can't be verified
            # blind - re-check against a live screenshot, adjust further if
            # still clipped.
            #
            # 620 -> 1000, 2026-09-02: real feedback, still seen live -
            # widening the COLUMN didn't un-truncate the underlying STRING,
            # which was still being cut at 90 chars by _monthly_blank_
            # reason() above regardless of how much column space existed.
            # That cap is now 150 (same ~6.9px/char ratio applied to the
            # new cap) - this width follows it up to match.
            "Est. Monthly PAYG Cost": st.column_config.TextColumn("Est. Monthly Cost", width=1000),
        },
    )
    with dl_col:
        _csv_download_button(show_df, f"{selected_provider.lower()}_inventory", key=f"{key_prefix}_dl_normal")


def _render_inventory_tab():
    st.subheader(f"{selected_provider} Infrastructure Asset Inventory")
    st.caption(
        f"Real-time resource registry across compute and database domains in {selected_provider}."
    )
    _finops_tag("Understand Usage & Cost", "Data Ingestion, Reporting & Analytics")

    # The donut chart + Top Spend Categories card that used to live here
    # moved to a dedicated Cost Analysis tab on 2026-08-28 (an asset
    # inventory is a registry - "what do I have" - list/filter/search;
    # spend-distribution belongs in a dedicated cost view). That tab was
    # removed entirely on 2026-08-30 - real user question about what the
    # numbers actually meant surfaced that the card's $ figures were both
    # undiscounted on-demand list price (ignored any RI/SP coverage) and
    # didn't filter to Resource State == "Running" (a stopped resource's
    # full rate still counted), and a not-fully-accurate composition view
    # wasn't judged worth keeping even fixed. This tab stays a pure
    # registry, no forward pointer to replace it with.

    # Single unified table with a Resource Type filter, not a per-type tab
    # strip (2026-08-23, direct feedback: 24+ tabs were "very hard to
    # scroll right and select" - Azure Portal's own "All resources" page
    # uses a Type filter for exactly this, not per-type tabs, so the
    # filter bar already covers what the tabs did).
    mode_tag = env_mode.replace(" ", "_").replace("/", "_")
    key = f"{selected_provider}_{mode_tag}_all".replace(" ", "_")
    _render_inventory_section(inv_raw, key)


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYZE — SAVINGS PLAN ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════
def _render_sp_pool_economics(pool_label: str, pool_df: pd.DataFrame, existing_commitment_hr: float,
                               key_prefix: str, safety_buffer_frac: float, commitment_df: pd.DataFrame,
                               available_terms=("1yr", "3yr"), aws_sp_type: str = None):
    """Flow for one Savings Plan pool (Compute/Database/SageMaker) -
    redesigned 2026-08-28, approved via mockup first, then simplified
    further the same day after real feedback that even the reordered
    version was "much more confusion... hard to understand for me even"
    (from the person who built the app). Root cause wasn't the order, it
    was density: 6 related dollar figures + a resource breakdown + a term
    comparison table, all always visible and equally weighted. Current
    shape - one always-visible headline answer, everything else collapsed:
      - One-sentence answer + term choice + the committed/recommended/
        eligible bridge visual, always on screen
      - "What you already own" and "What's eligible" are collapsed
        st.expanders (their hint text in the expander label itself), opened
        only if someone wants the detail - same content as before, just not
        forced onto the page by default
      - "Detailed pricing by term" is its own small collapsed expander
        right under the headline, since it's tied to the term choice, not
        to ownership or eligibility

    commitment_df is this pool's real purchased-plan rows (compute_sp_df /
    db_sp_df / sagemaker_sp_df); existing_commitment_hr stays a separate
    float param (not derived from commitment_df here) since callers already
    compute it from the same source of truth used elsewhere in this app
    (commitments/existing_commitments.py).

    available_terms restricts which term(s) can be modeled - e.g. Database
    Savings Plans are 1-year only per Azure policy, so that pool never gets a
    3-year option here.

    aws_sp_type ("compute" | "sagemaker" | "database" | None) - which real
    AWS SavingsPlansType this pool maps to for
    aws_savings_plan_term_comparison (2026-08-28 addition). None for Azure
    pools (unused there)."""
    if pool_df.empty:
        st.caption(f"No resources are currently eligible for {pool_label} Savings Plan.")
        return

    baseline_hr = float(pool_df["PAYG Hourly Cost USD"].sum())
    remaining_hr = max(0.0, baseline_hr - existing_commitment_hr)
    recommended_hr = round(remaining_hr * safety_buffer_frac, 4)
    leakage_hr = max(0.0, existing_commitment_hr - baseline_hr)

    # Term read directly from the WIDGET'S OWN session_state key
    # (f"{key_prefix}_term_display", set further down), not a separate
    # manually-mirrored key - real bug fixed 2026-08-28 (same root cause
    # caught and fixed on the RI Coverage tab the same day): the previous
    # version read a separate "..._term_widget" mirror key that was only
    # written AFTER the widget call far below, so it was always one full
    # render stale relative to whatever term the user had just clicked -
    # the "avoids... lagging a run behind" comment this replaced was the
    # intent, but the mirror-key approach didn't actually achieve it.
    # Streamlit updates a keyed widget's session_state entry to the
    # CURRENT (post-click) value before the script body starts executing
    # on every rerun, so reading it directly here reflects a click
    # immediately.
    if len(available_terms) > 1:
        term_label = st.session_state.get(f"{key_prefix}_term_display", "1-Year")
        term_key = "1yr" if term_label == "1-Year" else "3yr"
        if term_key not in available_terms:
            term_key = available_terms[0]
    else:
        term_key = available_terms[0]

    # AWS branch added 2026-08-28: real per-term discount % cached on the
    # tenant row (aws_sp_pricing, see db/schema.py + data/sync_pipeline.py),
    # not a per-SKU cache lookup the way Azure's works - see
    # aws_savings_plan_term_comparison's own docstring for why.
    if is_azure:
        cmp_df = savings_plan_term_comparison(pool_df, prices_df) if (prices_df is not None and not prices_df.empty) else None
    elif aws_sp_type is not None:
        cmp_df = aws_savings_plan_term_comparison(pool_df, active_tenant, aws_sp_type)
    else:
        cmp_df = None
    if cmp_df is not None:
        cmp_df = cmp_df[cmp_df["term_key"].isin(available_terms)].reset_index(drop=True)
    has_real_pricing = cmp_df is not None and int(cmp_df["Priced Resources"].sum()) > 0

    discount_pct = None
    est_monthly_savings = None
    if has_real_pricing:
        chosen = cmp_df[cmp_df["term_key"] == term_key].iloc[0]
        discount_pct = float(chosen["Discount %"])
        est_monthly_savings = recommended_hr * (discount_pct / 100.0) * 730

    # ── Headline: one answer, always visible ────────────────────────────
    # Visual-only refresh, 2026-08-30 (real feedback: match Recommendations'
    # card language) - now the same .rec-headline-card family instead of a
    # plain st.markdown "####" line, with an eyebrow naming the pool (this
    # function is shared across Compute/Database/SageMaker, so the card
    # needs to say which one it's showing - the tab label alone doesn't
    # carry into the card itself). Approved via mockup first, using real
    # Azure demo pricing.
    #
    # No more _md() "$" escaping (was needed to dodge st.markdown's own
    # KaTeX "$...$" math-mode trigger) - this whole block is raw HTML via
    # unsafe_allow_html=True now, which has no such trigger; same real bug
    # already found and fixed once on the Recommendations tab (a literal,
    # unstripped backslash rendering on screen) applies here too, so
    # plain fmt() output is correct.
    #
    # Tone genuinely differs by state here, unlike Recommendations' headline
    # (always good news) - "you're over-committed" is a real warning, not
    # savings, so it gets the card's own red "warn" variant rather than
    # forcing the same green framing onto a bad-news number.
    # Real bug caught 2026-09-02, same root cause already fixed once on RI
    # Coverage the same way (see that tab's own "fully_covered > 0" comment):
    # recommended_hr <= 0.001 is also true when baseline_hr itself is $0
    # because this pool's PAYG rate couldn't be priced yet (a real,
    # confirmed gap - Azure SQL Database Serverless's rate wasn't
    # resolvable by the old lookup, see pricing/azure_retail_api.py) - NOT
    # because the resource is actually free or genuinely well covered. The
    # old code couldn't tell "$0 eligible spend, nothing to cover" apart
    # from "$0 because we don't know the real rate yet", and confidently
    # printed "well covered" either way. baseline_hr > 0 is now required
    # before that verdict is trusted - a pool with eligible resources but
    # no priceable spend gets its own honest "can't price this yet" state
    # instead, same "don't guess" discipline as the "Estimate only" caption
    # already applies to the separate commitment-rate gap below.
    # "Can't price this yet... re-run a sync" is only true when the gap is
    # genuinely transient (a real rate a fresh sync could still find).
    # Real feedback 2026-09-02: first written narrowly for the one case
    # actually on screen (Azure SQL Database Serverless), then correctly
    # pushed back on - the SAME situation applies to any OTHER resource
    # type this app has deliberately decided never to price (AWS Aurora
    # Serverless v2 is the confirmed sibling case, and a future tenant
    # could hit a different one entirely) - a narrow one-off check would
    # silently keep showing the misleading "re-run a sync" message for
    # every case except the one it happened to be written against.
    # pricing/unpriced_by_design.py centralizes this as a small, named
    # registry instead - extensible by adding an entry there, not by
    # writing a new check here each time. The message below reflects
    # whatever reason(s) actually matched, not a hardcoded resource name.
    # Real structural bug caught 2026-09-02 (not just wording): the FIRST
    # version of this check only looked at whether the WHOLE POOL's
    # baseline_hr summed to $0 - true for THIS tenant only because it
    # happens to have exactly one resource, and that resource is
    # unpriced-by-design. For any realistic tenant with a MIX of resources
    # (say 48 normally-priced databases + 2 Serverless ones), the 48 real
    # rates dominate the sum, baseline_hr is well above zero, and the
    # by-design resources silently vanish into that sum with NO
    # disclosure anywhere in the main card - the whole check was
    # effectively dead code outside this one degenerate single-resource
    # case. Fixed by making this an ADDITIVE disclosure (a sub-line
    # appended regardless of which main verdict below actually fires),
    # not a mutually-exclusive branch gated on the pool-wide sum - so a
    # pool that's genuinely "well covered" overall STILL discloses that 2
    # of its resources are permanently unpriced, instead of only ever
    # saying something when literally every resource is.
    _pool_size = len(pool_df)
    _unpriced_by_design_count = 0
    if not pool_df.empty:
        for _, _r in pool_df.iterrows():
            if unpriced_by_design_reason(selected_provider, _r.get("Resource Type"), _r.get("SKU")):
                _unpriced_by_design_count += 1
    _all_unpriced_by_design = _pool_size > 0 and _unpriced_by_design_count == _pool_size

    # sub mirrors RI Coverage's own headline pattern (see
    # _render_ri_coverage_tab's "sub" variable + .rec-headline-sub CSS
    # class, already used there and on the combined Recommendations card).
    sub = ""
    is_warning = leakage_hr > 0
    if is_warning:
        sentence = f"You've committed <b>{fmt(leakage_hr)}/hr</b> more than is currently eligible — worth reviewing this plan."
    elif baseline_hr <= 0.001 and _all_unpriced_by_design:
        # Real bug caught 2026-09-02, right after the structural fix below
        # shipped: for a pool that's 100% by-design-unpriced, the previous
        # version STILL said "Can't price this yet... Re-run a sync",
        # and then immediately appended the disclosure saying "re-syncing
        # won't change it" - a direct, visible contradiction in the same
        # sentence. When literally every eligible resource is a known,
        # permanently-unpriced type, the headline itself says so plainly,
        # with no "try syncing" advice to contradict.
        sentence = "<b>Can't price this by design</b> — not a data gap, and re-syncing won't change it."
        sub = "See \"Why $0.00/hr\" in \"What's eligible\" below for the reason."
    elif baseline_hr <= 0.001:
        sentence = "<b>Can't price this yet</b> — no cached PAYG rate for this pool's resources."
        sub = "Re-run a sync from the tenant's Manage dialog on the Home page."
    elif recommended_hr <= 0.001:
        sentence = "<b>You're already well covered</b> — no additional commitment recommended right now."
    elif has_real_pricing:
        sentence = (
            f"Committing <b>{fmt(recommended_hr)}/hr</b> more would save about "
            f"<b>{fmt(est_monthly_savings, 2)}/month</b> at the {TERM_LABELS[term_key]} rate."
        )
    else:
        sentence = f"We recommend committing <b>{fmt(recommended_hr)}/hr</b> more, based on your safety buffer setting."

    # Disclosure only added for a PARTIAL mix (some priced, some not) - the
    # realistic large-tenant case the structural fix above exists for. When
    # it's ALL unpriced-by-design, the headline branch above already says
    # so directly (adding this again would just repeat the same fact in
    # two places); when it's NONE, there's nothing to disclose.
    if 0 < _unpriced_by_design_count < _pool_size:
        _plural = _unpriced_by_design_count != 1
        _disclosure = (
            f"{_unpriced_by_design_count} resource{'s' if _plural else ''} here {'are' if _plural else 'is'} "
            f'priced $0 by design (not a gap - re-syncing won\'t change it) - see "Why $0.00/hr" in '
            '"What\'s eligible" below.'
        )
        sub = f"{sub} {_disclosure}" if sub else _disclosure
    st.markdown(
        f'<div class="rec-headline-card{" warn" if is_warning else ""}">'
        f'<div class="rec-headline-eyebrow">{html.escape(pool_label)} Savings Plan</div>'
        f'<div class="rec-headline-sentence">{sentence}</div>'
        + (f'<div class="rec-headline-sub">{sub}</div>' if sub else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    if len(available_terms) > 1:
        st.segmented_control(
            f"Model {pool_label} commitment at term",
            options=["1-Year", "3-Year"],
            default=TERM_LABELS[term_key],
            key=f"{key_prefix}_term_display",
            label_visibility="collapsed",  # help= tooltip is suppressed when the label is collapsed (confirmed via st.segmented_control's own docstring) - the headline sentence above already explains what this drives, so no help= here
        )
        # term_key already reflects this widget's live state (read above,
        # before the widget call) - no need to re-derive it from the
        # return value here. Still mirrored into "..._term_widget" for
        # _real_projected_savings() (Recommendations tab), which reads
        # "sp_compute_term_widget" on a later, separate render.
        st.session_state[f"{key_prefix}_term_widget"] = term_key
    else:
        # "(Azure policy)" used to be accurate since this branch only ever
        # fired for Azure's Savings Plan for Databases - now also fires for
        # AWS's Database Savings Plan (also 1-year-only, confirmed via
        # AWS's own FAQ - see db_available_terms's comment above), so the
        # wording needs to name whichever provider is actually active.
        st.caption(f"{pool_label} Savings Plans only support a {TERM_LABELS[term_key]} term ({selected_provider} policy).")

    # Total $ commitment over the full term, not just the monthly savings -
    # real gap flagged directly, 2026-08-28: the headline says how much
    # you'd SAVE, not what you'd actually be signing up to PAY. Computed
    # per available term (not just the active one) from data already on
    # this page: the recommended hourly amount at that term's real discount
    # rate (cmp_df, same source the headline's own savings figure uses),
    # times the real hours in that term (MONTH_HOURS x 12 or x 36).
    if has_real_pricing and recommended_hr > 0.001:
        # fmt() prefixes USD values with a literal "$" - st.markdown/
        # st.caption treat a PAIRED "$...$" as LaTeX math (confirmed real,
        # 2026-08-28). _md() escapes "$" to "\$" so it renders as a literal
        # currency symbol instead - still needed HERE (unlike the headline
        # above, which moved to raw HTML and dropped this same escape,
        # since HTML has no such trigger) because this caption below is
        # still plain st.caption markdown text.
        def _md(x: str) -> str:
            return x.replace("$", "\\$")
        term_cost_parts = []
        for t in available_terms:
            row = cmp_df[cmp_df["term_key"] == t].iloc[0]
            t_discount_pct = float(row["Discount %"])
            committed_rate = recommended_hr * (1 - t_discount_pct / 100.0)
            hours_in_term = MONTH_HOURS * (12 if t == "1yr" else 36)
            total_cost = committed_rate * hours_in_term
            term_cost_parts.append(f"{TERM_LABELS[t]} {_md(fmt(total_cost, 0))}")
        st.caption(f"Total commitment if purchased: {' · '.join(term_cost_parts)}")

    if not has_real_pricing:
        reason = (
            "real-time AWS Savings Plans pricing isn't wired up yet" if not is_azure else
            "no cached commitment pricing yet for this pool's SKUs - re-run a sync from the tenant's Manage dialog on the Home page"
        )
        st.caption(f":material/info: Estimate only — {reason}.")

    committed_pct = min(100.0, (existing_commitment_hr / baseline_hr * 100)) if baseline_hr > 0 else 0.0
    recommend_pct = max(0.0, min(100.0 - committed_pct, (recommended_hr / baseline_hr * 100) if baseline_hr > 0 else 0.0))
    leftover_hr = max(0.0, remaining_hr - recommended_hr)
    over_committed_html = (
        f'<div class="spflow-cardhead" style="margin-top:10px;color:#F87171;">'
        f'Over-committed by {fmt(leakage_hr)}/hr - committed more than is currently eligible.</div>'
        if leakage_hr > 0 else ""
    )
    st.markdown(
        '<div class="fl-previewcard" style="margin-top:14px;">'
        f'<div class="spflow-econrow"><span class="spflow-econlabel">Eligible Hourly Spend</span><span class="spflow-econval fl-mono">{fmt(baseline_hr)}/hr</span></div>'
        '<div class="spflow-bridgetrack">'
        f'<div class="spflow-bridge-committed" style="width:{committed_pct:.1f}%;"></div>'
        f'<div class="spflow-bridge-recommend" style="width:{recommend_pct:.1f}%;"></div>'
        "</div>"
        '<div class="spflow-legend">'
        f'<span><span class="spflow-dot" style="background:#60A5FA;"></span>Committed — {fmt(existing_commitment_hr)}/hr</span>'
        f'<span><span class="spflow-dot" style="background:rgba(52,211,153,.5);"></span>Recommended — {fmt(recommended_hr)}/hr</span>'
        f'<span><span class="spflow-dot" style="background:#1E2A3F;"></span>Left uncommitted (buffer) — {fmt(leftover_hr)}/hr</span>'
        "</div>"
        f"{over_committed_html}"
        "</div>",
        unsafe_allow_html=True,
    )

    # "Detailed pricing by term" table removed entirely, 2026-08-28 - real
    # feedback: explained twice (what it means, why it differs from the
    # headline number) and it still didn't land, plus its own "PAYG $/hr"
    # figure quietly disagreed with the bridge card's "Eligible Hourly
    # Spend" right above it (this table uses cached retail pricing via
    # savings_plan_term_comparison's _lookup_payg, the bridge uses actual
    # inventory PAYG rates - a second, slightly different version of a
    # number already on screen). The 1yr/3yr comparison this table existed
    # to support is still available: click each term pill above and read
    # the headline sentence for that term. cmp_df/has_real_pricing/
    # discount_pct/est_monthly_savings are still computed above - still
    # needed for the headline and the "Estimate only" caption, just no
    # longer rendered as their own table.

    commitment_hint = (
        f"{len(commitment_df)} active plan{'s' if len(commitment_df) != 1 else ''}, covers {(existing_commitment_hr / baseline_hr * 100) if baseline_hr > 0 else 0.0:.0f}%"
        if not commitment_df.empty else "none purchased yet"
    )
    with st.expander(f"What you already own — {commitment_hint}", icon=":material/receipt_long:", expanded=False):
        st.caption("A record of your active Savings Plans - not a recommendation.")
        if commitment_df.empty:
            st.caption(f"No {pool_label} Savings Plans purchased yet.")
        else:
            sp_show = commitment_df[["commitment_id", "scope_sku", "scope_region", "hourly_usd_commitment", "term", "expiry_date"]].copy()
            sp_show["hourly_usd_commitment"] = sp_show["hourly_usd_commitment"].apply(lambda x: fmt(x, 4) + "/hr")
            sp_show_disp = _with_mapping_caveat(commitment_df, sp_show)
            _csv_download_button(sp_show_disp, f"{selected_provider.lower()}_{pool_label.lower().replace(' ', '_')}_commitments", key=f"dl_sp_commit_{pool_label.lower().replace(' ', '_')}", right_aligned=True)
            st.dataframe(sp_show_disp, hide_index=True, width="stretch", column_config=_SP_COMMITMENT_COLUMN_CONFIG)

    by_type = (
        pool_df.groupby("Resource Type")
        .agg(count=("Resource Name", "size"), rate=("PAYG Hourly Cost USD", "sum"))
        .reset_index()
        .sort_values("rate", ascending=False)
    )
    elig_container_key = f"{key_prefix}_eligible"
    with st.container(key=elig_container_key), st.expander(f"What's eligible — {len(pool_df)} resource{'s' if len(pool_df) != 1 else ''}, {fmt(baseline_hr)}/hr", icon=":material/checklist:", expanded=False):
        elig_show = pool_df[["Resource Name", "Resource Type", "SKU", "PAYG Hourly Cost USD"]].copy()
        # "Why $0.00/hr" - added 2026-09-02, the real detail the headline's
        # "Can't price this by design" / "Can't price this yet" sub-line
        # now points to instead of spelling it out in the headline itself.
        # Reuses the same pricing/unpriced_by_design.py registry the
        # headline check above uses, so the two never disagree - a genuine
        # (not by-design) gap gets a plain, honest "No cached PAYG rate
        # yet" rather than pretending to know why.
        def _why_zero(r):
            if r["PAYG Hourly Cost USD"]:
                return ""
            reason = unpriced_by_design_reason(selected_provider, r["Resource Type"], r["SKU"])
            return reason if reason else "No cached PAYG rate yet"
        elig_show["Why $0.00/hr"] = pool_df.apply(_why_zero, axis=1)
        elig_show["PAYG Hourly Cost USD"] = elig_show["PAYG Hourly Cost USD"].apply(lambda x: fmt(x, 4))
        elig_show = elig_show.rename(columns={"PAYG Hourly Cost USD": "PAYG Cost/hr"})
        # Dynamic widths, not a fixed dict - real gap caught live,
        # 2026-08-30: a first attempt hardcoded widths to this app's
        # longest value ACROSS EVERY provider/pool (e.g. AWS SageMaker's
        # "Amazon SageMaker Notebook Instance"), so it never clipped
        # anywhere - but a pool with genuinely short values (this Azure
        # Compute pool's own "Compute"/"Azure Dedicated Host") sat in a
        # column sized for a value 3-5x longer than anything it will ever
        # show, which is exactly the empty-space look this was supposed to
        # fix. Computed instead from THIS POOL's own real data on every
        # render - the header label or its own longest real cell,
        # whichever is longer - so each pool's table is genuinely tight to
        # what it actually contains, not sized for the app-wide worst case.
        def _col_width(col: str, min_px: int = 90, max_px: int = 320) -> int:
            longest_chars = max([len(col)] + [len(str(v)) for v in elig_show[col]])
            return int(min(max_px, max(min_px, longest_chars * 7.5 + 32)))

        # "Why $0.00/hr" gets its own higher cap - real disclosed reasons
        # from pricing/unpriced_by_design.py run 60-90+ chars, well past
        # every other column's short, compact real values (a name, a
        # type, a SKU) this 320px default cap was actually sized for.
        col_widths = {
            col: (_col_width(col, max_px=650) if col == "Why $0.00/hr" else _col_width(col))
            for col in elig_show.columns
        }
        # The table's own content-driven width (sum of its column widths,
        # plus glide-data-grid's own border/cell chrome, ~2px/col) - reused
        # below as an explicit width on the "By resource type" list so the
        # two blocks share one width instead of each shrinking to ITS OWN
        # content independently. Two independently-content-tight blocks
        # looked *more* inconsistent side by side than the original full-
        # width stretch did (list ~375px, table ~750px, live-verified
        # 2026-08-30) - matching them to the table's width (the wider,
        # more information-dense block) is the one that reads as "one card
        # laid out together" rather than two unrelated ones stacked.
        table_width_px = sum(col_widths.values()) + 2 * len(col_widths)

        # The expander's own outer box defaults to full container width
        # regardless of how narrow its content is (a Streamlit structural
        # default, not something column_config touches) - left as-is, a
        # ~750-900px content block sat inside a ~1050px box with well over
        # 100px of dead space on the right, flagged directly ("the box
        # outer border is too big"). Constrained here to the content's own
        # width plus stExpanderDetails' real measured padding (14px each
        # side, confirmed via live getBoundingClientRect - not guessed),
        # scoped to this specific expander only via the container's
        # st-key-* class so every OTHER expander on this page (e.g. "What
        # you already own", intentionally full-width to match its own
        # always-stretch table) is untouched.
        # Targets `details[open]` specifically, not the whole stExpander -
        # a real regression caught live, 2026-08-30: constraining the
        # whole box shrank it in BOTH the collapsed and expanded states,
        # so the collapsed row sat noticeably narrower than its full-width
        # sibling "What you already own" right above it - two peer rows in
        # the same list reading as visually inconsistent, the opposite of
        # what this whole pass was trying to fix. Confirmed via live DOM
        # inspection this app's real Streamlit build renders the expander
        # as a native <details> element that gains an `open` attribute
        # only while expanded - `details[open]` matches that state
        # exactly, so the row stays full-width (matching its siblings)
        # until opened, then narrows to its own content.
        st.markdown(
            f"<style>.st-key-{elig_container_key} [data-testid='stExpander'] details[open] "
            f"{{ max-width: {table_width_px + 28}px; }}</style>",
            unsafe_allow_html=True,
        )

        type_rows_html = "".join(
            '<div class="spflow-row">'
            f'<span class="spflow-rowname">{html.escape(str(r["Resource Type"]))}</span>'
            f'<span class="spflow-rowcount">{int(r["count"])} resource{"s" if r["count"] != 1 else ""}</span>'
            f'<span class="spflow-rowrate fl-mono">{fmt(r["rate"], 4)}/hr</span>'
            "</div>"
            for _, r in by_type.iterrows()
        )
        st.markdown(
            f'<div class="spflow-cardhead">By resource type</div>'
            f'<div class="spflow-list" style="width:{table_width_px}px;">{type_rows_html}</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="spflow-cardhead" style="margin-top:16px;">Every resource</div>', unsafe_allow_html=True)
        # width="content", not "stretch" - the ACTUAL root cause of the
        # empty space, found by reading Streamlit's own installed source
        # (DataFrame.*.js) directly after the dynamic-width attempt above
        # still showed no visible change: "stretch" tells the underlying
        # grid (glide-data-grid) to redistribute any leftover space across
        # columns marked as growable whenever the summed column widths
        # fall short of the container's full width - confirmed in the
        # bundle's own column-sizing code (a `grow` mechanism triggered by
        # `n < r` - summed widths less than available width). That
        # redistribution was silently overriding every explicit
        # column_config width passed above, on every attempt, regardless
        # of what the values were - not a caching issue, a real,
        # previously-undiagnosed behavior. "content" sizes the table to
        # exactly match its own columns' widths instead, so there's never
        # leftover space for anything to grow into.
        _csv_download_button(elig_show, f"{selected_provider.lower()}_{pool_label.lower().replace(' ', '_')}_resources", key=f"dl_sp_resources_{pool_label.lower().replace(' ', '_')}", right_aligned=True)
        st.dataframe(
            elig_show, hide_index=True, width="content",
            column_config={
                col: st.column_config.TextColumn(width=w)
                for col, w in col_widths.items()
            },
        )


# Shared by every "existing commitments" table in the Savings Plan tab
# (Compute/Database/SageMaker pools) - these used to show raw snake_case
# column names (commitment_id, scope_sku, ...) verbatim, the only tables in
# this app that did, since no column_config was ever applied here (real gap
# caught while reviewing this tab, 2026-08-28). Widths explicit per this
# session's established finding that neither width="small" nor leaving
# width unset reliably avoids clipping - sized to this app's real data
# (commitment IDs, SKU/region strings, "$X.XXXX/hr" formatted values).
_SP_COMMITMENT_COLUMN_CONFIG = {
    "commitment_id":          st.column_config.TextColumn("Commitment ID", width=180),
    "scope_sku":              st.column_config.TextColumn("SKU", width=140),
    "scope_region":           st.column_config.TextColumn("Region", width=110),
    "hourly_usd_commitment":  st.column_config.TextColumn("Hourly Commitment", width=140),
    "term":                   st.column_config.TextColumn("Term", width=80),
    "expiry_date":            st.column_config.TextColumn("Expiry Date", width=120),
}


def _with_mapping_caveat(source_df: pd.DataFrame, display_df: pd.DataFrame) -> pd.DataFrame:
    """Appends a caveat column to a commitment display table when any row
    came from a live tenant's best-effort mapping (Azure genuinely can't
    disambiguate the purchase record - see pricing/commitment_mapping.py) -
    no-op (and no extra column) for demo data / unambiguous real rows, so
    this never clutters the common case."""
    if "is_inferred_mapping" not in source_df.columns or not source_df["is_inferred_mapping"].any():
        return display_df
    out = display_df.copy()
    out["⚠️ Note"] = source_df["mapping_note"].where(source_df["is_inferred_mapping"], "").fillna("")
    return out


def _render_savings_plan_tab():
    st.subheader(f"{selected_provider} Savings Plan Analysis")
    st.caption("Shows which resources run continuously, compares them to what you've already committed, and recommends how much more to commit.")
    _finops_tag("Optimize Usage & Cost", "Rate Optimization")

    with st.expander(f"{selected_provider} Savings Plan Coverage Policy", icon=":material/checklist:", expanded=False):
        # Trimmed across every field, 2026-08-30 (real feedback: "too much
        # information... no one will read long paragraphs"). Kept every
        # real fact (which services, why a gap exists) - cut restated
        # qualifiers, parenthetical justifications, and any explanation
        # that duplicates what the Tracked/Not Tracked fields below
        # already say (AWS's old "Not Covered" text used to re-explain
        # the SAME ephemeral-job reasoning those fields now own).
        if is_azure:
            sp_coverage_rows = [
                {
                    "Savings Plan Type": "Savings Plan for Compute (1-yr / 3-yr)",
                    "What Is Covered": "VMs, App Service, Functions Premium, Container Instances (ACI), Dedicated Host, Container Apps, Spring Apps",
                    "What Is NOT Covered": "Software licenses (Windows/SQL), networking bandwidth, OS/Data disks, non-compute services"
                },
                {
                    "Savings Plan Type": "Savings Plan for Databases (1-yr ONLY)",
                    "What Is Covered": "SQL Database, SQL Elastic Pool, SQL Managed Instance, PostgreSQL, MySQL, Cosmos DB (provisioned throughput), DMS, DocumentDB",
                    "What Is NOT Covered": "Software licenses (AHB), backup storage, networking, 3-year term (1-yr only)"
                }
            ]
        else:
            # "Tracked"/"Not Tracked" are deliberately separate from "What
            # Is Covered" (AWS's real product eligibility) - added
            # 2026-08-23 after a full service-by-service feasibility pass.
            # AWS eligibility and this app's ability to build a
            # per-resource baseline for it are two different questions;
            # conflating them had made it unclear which gaps were "AWS
            # doesn't offer this" vs "AWS offers it but this app can't
            # observe it." Split into their own two lines (was one merged
            # sentence) 2026-08-30 - real feedback that the icons buried
            # mid-sentence read as "a mess." "Not Tracked" is omitted
            # entirely for a plan type with no real gap.
            sp_coverage_rows = [
                {
                    "Savings Plan Type": "Compute Savings Plans (1-yr / 3-yr)",
                    "What Is Covered": "EC2, Fargate, Lambda - any region/family/OS/tenancy, up to 66% off",
                    "What Is NOT Covered": "EBS volumes, data transfer, licensing surcharges, non-compute services",
                    "Tracked": "EC2, Fargate.",
                    "Not Tracked": "Lambda - no per-instance data (per-invocation billing; AWS exposes only pool-level metrics).",
                },
                {
                    "Savings Plan Type": "EC2 Instance Savings Plans (1-yr / 3-yr)",
                    "What Is Covered": "EC2 within one family/region (e.g. m5 in us-east-1), up to 72% off",
                    "What Is NOT Covered": "RDS, ElastiCache, Redshift, S3, or instances outside the specified family/region",
                    "Tracked": "EC2 - same inventory as Compute Savings Plans above.",
                },
                {
                    "Savings Plan Type": "Database Savings Plans (1-yr ONLY)",
                    "What Is Covered": "Aurora, RDS, DynamoDB, ElastiCache for Valkey (not Redis/Memcached), DocumentDB, Timestream, Neptune, Keyspaces, DMS, OpenSearch - up to 35% off",
                    "What Is NOT Covered": "Redshift, MemoryDB, ElastiCache for Redis/Memcached (RI-eligible only), EC2/Fargate/Lambda, 3-year term (1-yr only)",
                    "Tracked": "Aurora, RDS, DynamoDB (provisioned-capacity only), ElastiCache for Valkey, DocumentDB, Neptune, Keyspaces, DMS, OpenSearch.",
                    "Not Tracked": "Timestream - fully usage-based, no instance/capacity concept to track.",
                },
                {
                    "Savings Plan Type": "SageMaker AI Savings Plans (1-yr / 3-yr)",
                    "What Is Covered": "SageMaker AI usage - any family/size/region/component, up to 64% off",
                    "What Is NOT Covered": "EC2/Fargate/Lambda/database compute.",
                    "Tracked": "Real-Time Inference Endpoints, Notebook Instances.",
                    "Not Tracked": "Training/Processing/Data Wrangler/Batch Transform - ephemeral jobs, nothing persistent to track.",
                },
            ]
        # Cards, not a dataframe - real bug caught live, 2026-08-28: these
        # cells are paragraph-length prose, and st.dataframe cells don't
        # wrap text regardless of column width (confirmed this session on
        # the Inventory/Rightsizing tables' clipping issues) - a table was
        # never going to show this content in full, only trade off which
        # part got cut. Real st.markdown text wraps naturally, so cards
        # fully solve it rather than just widening columns.
        #
        # Tabs instead of stacked cards, 2026-08-30 - real feedback (AWS's
        # own 4 cards, each with a 3rd "This App Tracks" bullet, was "too
        # much to scan at once" once the expander opened). Same fix this
        # exact file already used for the pool flows below (2026-08-28:
        # "stacking full 4-part flows for every pool... was too much
        # information", fixed with sub-tabs, one pool visible at a time) -
        # same problem shape, same fix. Applied to BOTH providers, not
        # just AWS's original 4-card case - real follow-up feedback:
        # wants the same UI on both clouds wherever possible, rather than
        # Azure's 2 shorter cards getting a different treatment just
        # because they'd have fit on screen at once too.
        def _render_sp_coverage_card(row):
            st.markdown(f"**{row['Savings Plan Type']}**")
            st.markdown(f":material/check_circle: **Covered:** {row['What Is Covered']}")
            st.markdown(f":material/block: **Not covered:** {row['What Is NOT Covered']}")
            if "Tracked" in row:
                st.markdown(f":material/visibility: **Tracked:** {row['Tracked']}")
            if "Not Tracked" in row:
                st.markdown(f":material/visibility_off: **Not tracked:** {row['Not Tracked']}")

        # Short tab labels - the "(1-yr / 3-yr)" term suffix is already
        # shown inside the card itself (row["Savings Plan Type"]'s full
        # string), so the tab only needs the plan name.
        coverage_tab_labels = [row["Savings Plan Type"].split(" (")[0] for row in sp_coverage_rows]
        coverage_tabs = st.tabs(coverage_tab_labels)
        for tab, row in zip(coverage_tabs, sp_coverage_rows):
            with tab:
                _render_sp_coverage_card(row)

    # Preset pills above the slider, 2026-08-30 (real feedback - asked for
    # an alternative to a bare slider). Reuses Rightsizing's own
    # Conservative/Balanced/Aggressive vocabulary for consistency, and the
    # same preset-drives-widget on_change pattern that tab already uses -
    # not a new interaction model, the same one already proven in this app.
    # "Balanced" = 80%, deliberately matching DEFAULT_SAFETY_BUFFER exactly
    # (analysis/engine.py) - confirmed via direct math (recommended_hr =
    # remaining_hr * safety_buffer_frac: a HIGHER % commits more of the
    # uncommitted baseline, leaving LESS headroom, so higher = more
    # aggressive, lower = more conservative) that using a different number
    # for "Balanced" than the app's real existing default would either
    # contradict the existing "conservative buffer" framing or silently
    # change behavior for anyone who never touches this control.
    _SP_BUFFER_PRESETS = {"Conservative": 60, "Balanced": 80, "Aggressive": 95}

    def _apply_sp_buffer_preset():
        choice = st.session_state["sp_safety_buffer_preset"]
        if choice != "Custom":
            st.session_state["sp_safety_buffer_widget"] = _SP_BUFFER_PRESETS[choice]

    def _sp_buffer_slider_changed():
        # Only fires on a genuine user drag (Streamlit on_change callbacks
        # don't fire when a DIFFERENT callback sets this same session_state
        # key programmatically) - so a preset click never spuriously flips
        # itself back to "Custom" here.
        current = st.session_state["sp_safety_buffer_widget"]
        st.session_state["sp_safety_buffer_preset"] = next(
            (name for name, pct in _SP_BUFFER_PRESETS.items() if pct == current), "Custom"
        )

    if "sp_safety_buffer_widget" not in st.session_state:
        st.session_state["sp_safety_buffer_widget"] = int(DEFAULT_SAFETY_BUFFER * 100)
    if "sp_safety_buffer_preset" not in st.session_state:
        _current_buffer = st.session_state["sp_safety_buffer_widget"]
        st.session_state["sp_safety_buffer_preset"] = next(
            (name for name, pct in _SP_BUFFER_PRESETS.items() if pct == _current_buffer), "Custom"
        )

    st.segmented_control(
        "Safety Buffer preset", options=list(_SP_BUFFER_PRESETS.keys()) + ["Custom"],
        key="sp_safety_buffer_preset", on_change=_apply_sp_buffer_preset,
        label_visibility="collapsed",
        help="Conservative keeps more headroom (commits less of the remaining eligible spend); "
             "Aggressive commits more, leaving less room for usage drops. Dragging the slider "
             "below to a non-preset value switches this to Custom.",
    )
    st.slider(
        "Safety Buffer % — how much of the steady-state footprint to commit",
        min_value=50, max_value=100,
        # No value= here - real error caught live, 2026-08-30: Streamlit
        # disallows passing BOTH value= and a key whose session_state entry
        # gets written via the Session State API (_apply_sp_buffer_preset
        # above does exactly that on a preset click) - "created with a
        # default value but also had its value set via the Session State
        # API." The st.session_state initialization above (before this
        # widget call) already guarantees the key exists on first load,
        # same pattern Rightsizing's own working presets already use
        # (pre-populate session_state once, never pass value= on the
        # widget itself).
        step=5, key="sp_safety_buffer_widget", on_change=_sp_buffer_slider_changed,
        help="The rest stays on PAYG as headroom, protecting against usage drops. Applies to both pools below.",
    )
    st.divider()

    # Sub-tabs per pool, not stacked sections (2026-08-28, real feedback:
    # stacking full 4-part flows for every pool on one page was "too much
    # information" regardless of the internal order) - only one pool's flow
    # is on screen at a time. Pool headers ("### A - Compute Savings Plan
    # Pool") dropped since the tab label already names the pool.
    pool_tab_labels = ["Compute", db_sp_title] + (["SageMaker AI"] if not is_azure else [])
    pool_tabs = st.tabs(pool_tab_labels)

    with pool_tabs[0]:
        # Matches the Database pool's caption verbatim, per direct request
        # 2026-08-28 - both pools now state the same plain "Running"
        # eligibility rule, consistent with the live-mode data gap
        # discussed this round (the 24x7-specific distinction isn't
        # meaningfully enforceable yet outside demo data).
        st.caption(
            "A Savings Plan is a fixed hourly commitment, so only resources currently Running are eligible - "
            "stopped resources stay on pay-as-you-go."
        )

        _render_sp_pool_economics("Compute", compute_24x7, compute_sp_commit, "sp_compute", safety_buffer, compute_sp_df, aws_sp_type="compute")

        if is_live_mode and is_live_configured:
            if compute_sp_pool_inventory.empty:
                st.info(
                    "No Savings Plan for Compute-eligible resource types found in this tenant "
                    "(VMs, App Service, Functions Premium, Container Instances, Dedicated Host, "
                    "Container Apps, Spring Apps) - nothing to baseline yet.", icon=":material/info:",
                )
            elif compute_24x7_candidates.empty:
                st.info(
                    f"{len(compute_sp_pool_inventory)} SP-eligible-type resource(s) found, but none are "
                    "currently **Running** - Savings Plans are only recommended against resources actually "
                    "running, to avoid over-committing.", icon=":material/info:",
                )
            elif compute_24x7.empty:
                st.warning(
                    f"{len(compute_24x7_candidates)} resource(s) are running, but none are actually "
                    "eligible for Savings Plan for Compute at their current SKU/tier - see the breakdown below.",
                    icon=":material/warning:",
                )

            if not compute_sp_excluded.empty:
                with st.expander(f"{len(compute_sp_excluded)} running resource(s) excluded from the Compute SP baseline", icon=":material/block:", expanded=False):
                    _compute_sp_excluded_show = compute_sp_excluded[["Resource Name", "Resource Type", "SKU", "SP Eligibility Note"]]
                    _csv_download_button(_compute_sp_excluded_show, f"{selected_provider.lower()}_compute_sp_excluded", key="dl_sp_compute_excluded", right_aligned=True)
                    st.dataframe(
                        _compute_sp_excluded_show,
                        hide_index=True, width="stretch",
                        column_config={"SP Eligibility Note": st.column_config.TextColumn("Why excluded", width="large")},
                    )

    with pool_tabs[1]:
        # Same real-accuracy fix as the Compute pool's caption above - being
        # currently Running doesn't guarantee it stays running for the
        # commitment term, so this states the eligibility rule, not a
        # safety claim the safety buffer already contradicts.
        st.caption("A Savings Plan is a fixed hourly commitment, so only resources currently Running are eligible - stopped resources stay on pay-as-you-go.")

        # Both providers' Database Savings Plan is 1-year ONLY - confirmed
        # via AWS's own FAQ for the AWS side (aws.amazon.com/savingsplans/
        # faqs), same restriction Azure's Savings Plan for Databases
        # already has. This used to be AWS-conditional ("EC2 Instance
        # Savings Plans genuinely do offer both terms") because the
        # database pool's AWS bucket used to be EC2 Instance Savings Plan
        # as a placeholder before Database Savings Plans were confirmed
        # real - corrected 2026-08-22 alongside db_sp_title below and
        # commitments/existing_commitments.py's bucketing.
        db_available_terms = ("1yr",)
        _render_sp_pool_economics(db_label, db_running, db_sp_commit, "sp_db", safety_buffer, db_sp_df, available_terms=db_available_terms, aws_sp_type=(None if is_azure else "database"))

        if is_live_mode and is_live_configured:
            if db_inventory.empty:
                st.info(f"No {db_sp_title}-eligible resource types found in this tenant - nothing to baseline yet.", icon=":material/info:")
            elif db_running_candidates.empty:
                st.info(
                    f"{len(db_inventory)} SP-eligible-type database resource(s) found, but none are "
                    "currently Running.", icon=":material/info:",
                )
            elif db_running.empty:
                st.warning(
                    f"{len(db_running_candidates)} database resource(s) are running, but none are actually "
                    "eligible for Savings Plan for Databases at their current tier - see the breakdown below.",
                    icon=":material/warning:",
                )

            if not db_sp_excluded.empty:
                with st.expander(f"{len(db_sp_excluded)} running resource(s) excluded from the {db_sp_title} baseline", icon=":material/block:", expanded=False):
                    _db_sp_excluded_show = db_sp_excluded[["Resource Name", "Resource Type", "SKU", "SP Eligibility Note"]]
                    _csv_download_button(_db_sp_excluded_show, f"{selected_provider.lower()}_database_sp_excluded", key="dl_sp_database_excluded", right_aligned=True)
                    st.dataframe(
                        _db_sp_excluded_show,
                        hide_index=True, width="stretch",
                        column_config={"SP Eligibility Note": st.column_config.TextColumn("Why excluded", width="large")},
                    )

    # AWS-only - SageMaker Savings Plans have no Azure equivalent product.
    if not is_azure:
        with pool_tabs[2]:
            st.caption(
                "Covers Real-Time Inference Endpoints and Notebook Instances only - Training, Processing, "
                "Data Wrangler, and Batch Transform jobs are one-shot ephemeral executions with no persistent "
                "running/stopped identity, so this app has no inventory row to baseline them against."
            )

            _render_sp_pool_economics("SageMaker AI", sagemaker_24x7, sagemaker_sp_commit, "sp_sagemaker", safety_buffer, sagemaker_sp_df, aws_sp_type="sagemaker")

            if is_live_mode and is_live_configured:
                if sagemaker_sp_pool_inventory.empty:
                    st.info(
                        "No SageMaker Endpoint/Notebook Instance resources found in this tenant - "
                        "nothing to baseline yet.", icon=":material/info:",
                    )
                elif sagemaker_24x7_candidates.empty:
                    st.info(
                        f"{len(sagemaker_sp_pool_inventory)} SageMaker resource(s) found, but none are "
                        "currently **Running** - Savings Plans are only recommended against resources actually "
                        "running, to avoid over-committing.", icon=":material/info:",
                    )


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYZE — RI COVERAGE
# ═══════════════════════════════════════════════════════════════════════════════
def _render_ri_coverage_tab():
    """One unified "Reservation Coverage" table (2026-08-29, real feedback:
    Per-Instance and Pooled resources used to be two separate tables - the
    primary table here (then titled "Per-Resource Coverage" - renamed the
    same day once that stopped being accurate, see below), plus a
    "pooled-capacity" table tucked inside "not shown above" - even though
    both answer the same question, "what's my coverage for this
    resource," just with different precision). Instance, Pooled, and
    Volume-Based rows (coverage_model "instance"/"capacity"/
    "unmeasurable") now all sit in one table, distinguished by a "Coverage
    Type" column and a per-row "Note" explaining anything non-obvious -
    matching how reference documentation typically handles this kind of
    exception (a flag/column, not a separate page section per exception).
    Volume-Based rows' Running/Reserved/Status are blanked to "—" rather
    than shown, since a resource-COUNT gap genuinely isn't meaningful for
    a service sold in DATA-VOLUME blocks. Only genuinely ineligible
    resources ("can this ever have a Reservation at all" - a different
    question from coverage type) stay in their own separate expander.

    Page order (2026-08-29 restructure, real feedback: "too cluttered, too
    much to understand" - was 9 stacked top-level sections, with the
    single most actionable item on the page, real $ actively wasted on a
    stopped resource with an active RI, buried dead last): headline+badges
    (the answer) -> orphaned-RI drain alert (the most urgent issue, if
    any) -> term toggle -> the unified Reservation Coverage table ->
    "N resource(s) not eligible for any Reservation" expander -> Reservation
    Coverage Rules and Active Reservation Contracts, each its own expander
    at the very bottom (previously 2 separate expanders sitting BEFORE the
    main table; briefly merged into one shared expander, then split back
    apart same day - they're different kinds of content, methodology
    guidance vs. a raw data table, and read better each with their own
    header)."""
    st.subheader(f"{selected_provider} Reserved Instance & Reserved Capacity Coverage")
    st.caption("Compares what's running against what you've already reserved, resource by resource, and flags real gaps to fix.")
    _finops_tag("Optimize Usage & Cost", "Rate Optimization")

    # cov/elig computed BEFORE the headline (2026-08-28 redesign) - the
    # headline sentence and badge row below need these counts, so they can't
    # wait until after the old metric-grid position the way this used to be
    # ordered.
    raw_cov = ri_result.coverage_table
    if prices_df is not None and not prices_df.empty and not raw_cov.empty:
        cov = ri_gap_pricing(raw_cov, inv_raw, prices_df).copy()
    else:
        cov = raw_cov.copy()

    if cov.empty:
        st.info("No reservation-eligible resources found yet.")
        return

    def _status(row):
        # "■" swatch, not an emoji, prepended uniformly (2026-08-30, table
        # restructure - mockup approved) - a canvas-rendered st.dataframe
        # cell can only take a plain colored TEXT string (confirmed: no
        # real background/border-radius support, same ceiling already hit
        # by Inventory's Power State and Rightsizing's Classification
        # columns), so a small colored block character is the honest
        # approximation of a "swatch" achievable here - the whole cell
        # string (glyph + text) gets one Styler color below, same
        # technique already proven working, not a new untested mechanism.
        if row["gap"] > 0:
            base = f"■ Short by {int(row['gap'])}"
            # partial_ri_credit_fraction (2026-08-29, analysis/engine.py's
            # size-flexibility reconciliation - both
            # _apply_aws_size_flexibility and, as of the Azure equivalent
            # added the same day, _apply_azure_vm_size_flexibility write
            # this column) - a real, verified mechanic on both clouds: a
            # smaller-size RI can already be giving one of these "short"
            # instances a genuine partial discount (AWS's own worked
            # example: a t2.medium RI gives a running t2.large a real
            # 50%-off credit; Azure's real ISF docs show the identical
            # shape for a smaller-size Reserved VM Instance), even though
            # it's not enough to resolve the gap to 0. NaN-safe via
            # pd.notna() - only the main tenant-wide merge layer sets this
            # column at all (Global/Single-subscription/Single-resource-
            # group/AWS Zonal-scope rows never pass through that
            # reconciliation, so they carry NaN here after the layers are
            # concatenated, not a real 0.0 - both must read as "no partial
            # credit").
            frac = row.get("partial_ri_credit_fraction")
            if pd.notna(frac) and frac > 0:
                base += f" · {frac * 100:.0f}% pre-covered"
            return base
        if row["excess"] > 0:
            return f"■ {int(row['excess'])} Idle"
        return "■ Fully Covered"

    cov["Status"] = cov.apply(_status, axis=1)

    elig = cov[cov["is_eligible"]]
    ineligible = cov[~cov["is_eligible"]]
    instance_cov = elig[elig["coverage_model"] == "instance"]
    capacity_cov = elig[elig["coverage_model"] == "capacity"]
    unmeasurable_cov = elig[elig["coverage_model"] == "unmeasurable"]

    fully_covered = int(((instance_cov["gap"] == 0) & (instance_cov["excess"] == 0)).sum())
    needs_more = int((instance_cov["gap"] > 0).sum())
    idle = int((instance_cov["excess"] > 0).sum())
    not_eligible = len(ineligible)

    # Term read directly from the WIDGET'S OWN session_state key
    # ("ri_term_display", set further down), not a separate manually-
    # mirrored key - real bug fixed 2026-08-28, caught by the user via
    # screenshot: clicking a term pill showed the OTHER term's headline/
    # rate, exactly one click behind. Streamlit updates a keyed widget's
    # session_state entry to the CURRENT (post-click) value before the
    # script body starts executing on every rerun, so reading it directly
    # here reflects a click immediately; the previous approach read a
    # separate "ri_term_widget" mirror key that was only written AFTER the
    # widget call far below, which was therefore always one full render
    # stale relative to what the user just clicked.
    ri_term_choice = st.session_state.get("ri_term_display", "1-Year")
    ri_term_key = "1yr" if ri_term_choice == "1-Year" else "3yr"
    rate_col = f"RI Rate {ri_term_choice} ($/hr)"
    savings_col = f"Monthly Savings if Purchased ({ri_term_choice})"
    has_pricing_cols = rate_col in instance_cov.columns

    # One consistent, fully-RESOLVED resource set for EVERY dollar figure
    # on this card (headline savings AND the Total commitment line below) -
    # real bug caught live, 2026-08-30: computing the headline's savings
    # from "priced for the currently-selected term" and the Total
    # commitment line from "priced for every term" silently drew on
    # different row counts (e.g. one real case: headline summed 4 rows,
    # commitment summed 3 - a resource with a cached 1-Year rate but no
    # 3-Year rate). A user reported "I didn't understand the calculation" -
    # traced to exactly this mismatch. Computed ONCE here, before the
    # headline, and reused below - no separate "N of M"/"excludes N"
    # disclosure text either (tried that, real feedback: "too much logic
    # to remind" for what should be a glance) - the $ figures just quietly
    # reflect whatever's actually priced, same "can't determine, don't
    # guess" approach this app already uses everywhere else.
    #
    # "Resolved", not just "has a real rate for every term" (2026-08-30,
    # a second real bug this same fix introduced) - Premium SSD P30 Disk
    # Reservations genuinely have NO 3-Year option at all (confirmed live,
    # not a data gap), so requiring a real rate for BOTH terms silently
    # dropped its real, known 1-Year savings from every $ total on this
    # card too. ri_gap_pricing() itself now applies this same "resolved"
    # standard when it nulls Monthly Savings (see its own comment) - this
    # mirrors that exact logic for the Total Commitment line's RATE-based
    # totals below, which read rate_col directly rather than the
    # already-correctly-nulled savings_col.
    gap_rows = instance_cov[instance_cov["gap"] > 0]

    def _gap_row_resolved(row) -> bool:
        for label in TERM_LABELS.values():
            rc = f"RI Rate {label} ($/hr)"
            if rc not in gap_rows.columns or pd.notna(row[rc]):
                continue
            redundancy = row.get("Redundancy", "N/A") or "N/A"
            if not term_row_exists(prices_df, "1yr" if label == "1-Year" else "3yr", row.get("Resource Type"), row.get("Region"), row.get("SKU"), row.get("OS"), redundancy):
                return False
        return True

    fully_priced = gap_rows[gap_rows.apply(_gap_row_resolved, axis=1)] if not gap_rows.empty else gap_rows

    # ── Headline: one answer, cards for the rest ─────────────────────────
    # Visual-only refresh, 2026-08-30 (real feedback: match Recommendations/
    # Savings Plan Analysis's card language) - plain st.markdown "####" +
    # 4 native st.badge() pills replaced with the same .rec-headline-card /
    # .rec-metric-card families used everywhere else. Approved via mockup
    # first. Raw HTML via unsafe_allow_html now (not markdown ":green[]"/
    # ":orange[]" spans) - same reason Savings Plan Analysis's headline
    # made this switch: no _md() "$" escaping needed once markdown's KaTeX
    # math-mode trigger is out of the picture entirely.
    plural = "s" if needs_more != 1 else ""
    is_warning = False
    sub = ""
    # Split what used to be one unconditional "needs_more == 0 -> well
    # covered" branch, 2026-08-30 - real bug caught live on a production
    # tenant: `needs_more`/`fully_covered` are BOTH computed only from
    # `instance_cov` (elig[elig["coverage_model"] == "instance"]) above, so
    # "needs_more == 0" is also true whenever there's simply nothing in that
    # bucket at all - e.g. every tracked resource is genuinely not
    # RI-eligible (not_eligible catches that) or only eligible under the
    # capacity/unmeasurable coverage models (Savings-Plan-only services,
    # Storage sold in 100TB+ blocks - excluded from instance_cov by
    # definition, see capacity_cov/unmeasurable_cov above). The user's own
    # real case: Fully Covered 0, Need More RI 0, Idle 0, Not RI-Eligible 3
    # - the old code still printed "You're already well covered", which
    # reads as an assessment that passed when there was really nothing here
    # TO assess. "Well covered" now requires fully_covered > 0 to actually
    # be true.
    if needs_more == 0 and fully_covered > 0:
        sentence = "<b>You're already well covered</b> — no additional Reserved Instance purchases recommended right now."
    elif needs_more == 0 and idle > 0:
        is_warning = True
        sentence = f"<b>{idle} Reservation{'s' if idle != 1 else ''} sitting idle</b> — nothing currently needs more coverage, but the unused capacity is worth exchanging."
    elif needs_more == 0:
        sentence = "<b>Nothing to measure Reserved Instance coverage against yet</b> — none of your tracked resources currently support per-instance RI coverage."
        sub = "Check the Not RI-Eligible card below for why."
    else:
        total_gap_savings = fully_priced[savings_col].dropna().sum() if has_pricing_cols and savings_col in fully_priced.columns else 0.0
        if total_gap_savings > 0:
            sentence = (
                f"Purchasing RIs for <b>{needs_more} resource profile{plural}</b> would save about "
                f"<b>{fmt(total_gap_savings, 2)}/month</b> at the {ri_term_choice} rate."
            )
        else:
            is_warning = True
            sentence = f"<b>{needs_more} resource profile{plural}</b> {'is' if needs_more == 1 else 'are'} running without a matching Reservation."
            sub = "Pricing unavailable for these SKUs right now — the coverage gap is still real."
    st.markdown(
        f'<div class="rec-headline-card{" warn" if is_warning else ""}">'
        '<div class="rec-headline-eyebrow">Reserved Instance Coverage</div>'
        f'<div class="rec-headline-sentence">{sentence}</div>'
        + (f'<div class="rec-headline-sub">{sub}</div>' if sub else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    # Term toggle moved here, directly under the headline it actually
    # drives (2026-08-30, real feedback: it used to sit below the drain
    # alert and coverage table - visually disconnected from the $ figure
    # in the headline above, which changes when this is clicked, with two
    # unrelated sections sandwiched in between). Same control, same
    # session_state key, same downstream reads (ri_term_choice/
    # ri_term_key/rate_col/savings_col already read live before the
    # headline was built above) - purely a position change.
    st.segmented_control(
        "Model new-purchase pricing at term",
        options=["1-Year", "3-Year"],
        default=ri_term_choice,
        key="ri_term_display",
        help="Drives the purchase-cost columns below and the Recommendations tab's combined savings projection.",
    )
    st.session_state["ri_term_widget"] = ri_term_key

    # Total $ commitment over the full term, not just the monthly savings -
    # same real gap Savings Plan Analysis's own headline already flagged
    # ("the headline says how much you'd SAVE, not what you'd actually be
    # signing up to PAY") and the same fix: one caption showing both
    # terms' real total cost side by side, not just the currently-selected
    # one. Computed per row (not from one blended rate, unlike Savings
    # Plan's single pool-wide rate) since every gap>0 row here can be a
    # different SKU/region at a different real reserved rate. Reuses the
    # SAME fully_priced set the headline above already computed - see that
    # block's comment for why (both dollar figures on this card now
    # describe the identical resource set, not independently-filtered
    # ones).
    if needs_more > 0 and has_pricing_cols:
        def _md(x: str) -> str:
            return x.replace("$", "\\$")
        term_cost_parts = []
        for t, label in TERM_LABELS.items():
            t_rate_col = f"RI Rate {label} ($/hr)"
            if t_rate_col not in fully_priced.columns:
                continue
            hours_in_term = MONTH_HOURS * (12 if t == "1yr" else 36)
            total_cost = (fully_priced["gap"] * fully_priced[t_rate_col] * hours_in_term).sum()
            term_cost_parts.append(f"{label} {_md(fmt(total_cost, 0))}")
        if term_cost_parts:
            st.caption(f"Total commitment if purchased: {' · '.join(term_cost_parts)}")

    st.divider()

    kpi_cols = st.columns(4)
    _ri_kpi_cards = [
        ("savings", "✅ Fully Covered", fully_covered, "no action needed"),
        ("under", "📈 Need More RI", needs_more, "real gap to close"),
        ("combined", "⏸️ Idle / Unused", idle, "consider exchanging"),
        ("neutral", "🚫 Not RI-Eligible", not_eligible, "no Reservation product exists"),
    ]
    for col, (tone, label, value, sub_label) in zip(kpi_cols, _ri_kpi_cards):
        with col:
            st.markdown(
                f'<div class="rec-metric-card {tone}">'
                f'<div class="lbl">{label}</div>'
                f'<div class="val fl-mono">{value}</div>'
                f'<div class="sub">{sub_label}</div>'
                "</div>",
                unsafe_allow_html=True,
            )
    st.divider()

    # ── Orphaned-RI drain alert - promoted here, right after the headline/
    # stats (2026-08-29, real feedback: "too cluttered, too much to
    # understand" - this was previously the LAST thing on the page, after
    # 8 other sections, even though it's the single most actionable item
    # here (real $ being wasted right now on a stopped resource with an
    # active RI) - promoted to where a user's eye lands right after the
    # headline, instead of requiring a full scroll to ever see it.
    # Wrapped in a keyed container (2026-08-29 follow-up feedback: the
    # default st.error() red box, even toned down app-wide, still read as
    # a generic/default alert next to this tab's own badge styling) - the
    # key scopes a custom re-skin (ui/styling.py's .st-key-fl_ri_drain_alert
    # rules) to just this alert, same "st-key-* scoping" pattern already
    # used for the login page's signin card, so no other st.error() in
    # the app is affected.
    if not ri_result.orphaned_ri_drain.empty:
        with st.container(key="fl_ri_drain_alert"):
            st.error(f"**{len(ri_result.orphaned_ri_drain)} stopped resource(s) draining active reservations**!", icon=":material/warning:")
            # Currency-aware display (2026-08-29, real feedback: this table
            # showed a redundant "RI Rate/hr" column - dropped at the
            # source, see analysis/engine.py's orphan_rows comment - and
            # ignored the Display Currency toggle entirely, always showing
            # raw USD even with INR selected. analysis/engine.py returns
            # raw numeric "Daily RI Drain"/"Monthly RI Drain" now (no
            # currency baked into the column name, same rule already
            # established for "Monthly Savings" elsewhere in this file) -
            # fmt() applied here, at display time, same as every other $
            # figure in this app.
            # No "Recommendation" column (2026-08-29, real feedback + a
            # screenshot proving this was a genuine rendering bug, not a
            # width-tuning problem: ui/styling.py's global
            # div[data-testid="stDataFrame"] { overflow: hidden } (needed
            # to round the wrapper's corners around glide-data-grid's
            # canvas) hard-clips any table whose canvas content is wider
            # than its rendered wrapper, with no scrollbar - confirmed
            # live by the user: scrolling revealed nothing, the content
            # was genuinely gone. No per-column width fixes that once the
            # total exceeds the real available width, and this column's
            # text was identical on every row anyway (see
            # analysis/engine.py's orphan_rows comment) - moved out of the
            # table into one caption instead of being the single widest,
            # fully-repeated column in it.
            st.caption("**Recommended action:** CANCEL / EXCHANGE each affected RI, or restart the resource to use it.")
            drain_disp = ri_result.orphaned_ri_drain.copy()
            for _dcol in ("Daily RI Drain", "Monthly RI Drain"):
                if _dcol in drain_disp.columns:
                    drain_disp[_dcol] = drain_disp[_dcol].apply(lambda x: fmt(x, 2))
            _csv_download_button(drain_disp, f"{selected_provider.lower()}_orphaned_ri_drain", key="dl_ri_drain", right_aligned=True)
            st.dataframe(
                drain_disp, hide_index=True, width="stretch",
                # Explicit widths for every column (2026-08-29, real
                # feedback - a full pass across every table on this tab).
                column_config={
                    "Resource ID":       st.column_config.TextColumn(width=160),
                    "Resource Name":     st.column_config.TextColumn(width=200),
                    "SKU":               st.column_config.TextColumn(width=140),
                    "Region":            st.column_config.TextColumn(width=110),
                    "OS":                st.column_config.TextColumn(width=80),
                    "Matching RI":       st.column_config.TextColumn(width=180),
                    "Daily RI Drain":    st.column_config.TextColumn(width=120),
                    "Monthly RI Drain":  st.column_config.TextColumn(width=140),
                },
            )

    # Header renamed from "Per-Resource Coverage" (2026-08-29, real
    # feedback: that title stopped being accurate the moment this table
    # gained a "Coverage Type" column that explicitly says some rows are
    # "Pooled"/"Volume-Based" - i.e. shared, not per-resource at all. This
    # section explains coverage for every resource, whichever way that
    # coverage actually applies.
    st.markdown("#### Reservation Coverage")
    # Unified table (2026-08-29, real feedback: Per-Instance and Pooled
    # resources were two separate tables - "Per-Resource Coverage" above,
    # "N pooled-capacity resource(s)" tucked in "not shown above" below -
    # even though they're the same underlying question ("what's my
    # coverage for this resource"), just answered with different
    # precision. Merged into one table with a "Coverage Type" column
    # (Per-Instance/Pooled/Volume-Based) instead, matching how reference
    # documentation typically handles this - one table, a column that
    # flags the exception, not a separate page section per exception.
    # Volume-Based rows (Storage/Files/etc., previously their own
    # "not tracked" section) are included here too, for the same reason -
    # their Running/Reserved/Status are blanked to "—" rather than shown
    # (see below), since a resource-COUNT gap is genuinely not meaningful
    # for a service sold in DATA-VOLUME blocks - showing a real-looking
    # number there would be worse than showing none.
    st.caption(
        "Each row is one resource profile (SKU + region + OS). \"Coverage Type\" shows how the Reservation "
        "applies - Per-Instance rows are a literal purchase recommendation; Pooled and Volume-Based rows apply "
        "automatically across your subscription, so treat their Status as a rough signal only (see Coverage Rules below)."
    )
    _shown_cov = pd.concat([instance_cov, capacity_cov, unmeasurable_cov], ignore_index=True) \
        if not (instance_cov.empty and capacity_cov.empty and unmeasurable_cov.empty) else pd.DataFrame()
    if not _shown_cov.empty:
        has_pricing_cols = rate_col in _shown_cov.columns
        show = _shown_cov.rename(columns={
            "Resource Type": "Service", "SKU": "SKU / Tier",
            "running_count": "Running", "reserved_qty": "Reserved",
        }).copy()
        _COVERAGE_TYPE_LABEL = {"instance": "Per-Instance", "capacity": "Pooled", "unmeasurable": "Volume-Based"}
        show["Coverage Type"] = show["coverage_model"].map(_COVERAGE_TYPE_LABEL)
        # Flexibility column (2026-08-30, real feedback: "0 running, 1
        # reserved, Fully Covered" looked like a contradiction until traced
        # live - the reservation's own capacity had been reallocated to
        # cover a DIFFERENT SKU in the same AWS/Azure size-flexibility
        # group, invisible anywhere in the table). Sourced directly from
        # analysis/engine.py's _apply_aws_size_flexibility /
        # _apply_azure_vm_size_flexibility, which already compute this
        # exact classification internally to decide what to group - this
        # just surfaces it instead of discarding it. .get() with a
        # fallback Series, not show["flexibility_status"] directly - the
        # column only exists at all when reservation_analysis() actually
        # ran a flexibility pass (never true for a coverage_table built
        # from an empty/pre-sync tenant).
        show["Flexibility"] = show.get("flexibility_status", pd.Series("N/A", index=show.index)).fillna("N/A")
        # "Flex Group" (2026-08-30, real follow-up: "how do I know WHICH
        # RI is covering this?") - "Flexible" alone says a row is linked
        # to something else, not what. Shows the real family/class-type
        # name (e.g. "Ddsv5 Series", "m5 family") for any row currently
        # marked Flexible, blank otherwise - match this text across rows
        # (same Region already shown as its own column) to find the rest
        # of the group. Deliberately a label, not a row-to-row link: the
        # real mechanic is a shared pool across every SKU in the group,
        # not necessarily one specific row covering another - a group of
        # 3+ SKUs can have several rows jointly contributing.
        show["Flex Group"] = show.get("flex_group_label", pd.Series("", index=show.index)).fillna("")
        show.loc[show["Flex Group"] == "", "Flex Group"] = "—"
        # Volume-Based rows' running_count/reserved_qty/gap ARE computed
        # internally (same merge every other row goes through), but
        # deliberately not shown - a service sold in 100 TB/1 PiB blocks
        # doesn't have a meaningful per-resource-COUNT gap (this app has
        # no way to know the actual data volume needed), so a real-looking
        # "Short by 1" here would be actively misleading, not just
        # imprecise. Same "can't determine, don't guess" discipline used
        # throughout this app - blanked, not computed-and-shown.
        # Running/Reserved cast to object BEFORE assignment - real bug
        # caught by a direct script (not the browser, per standing
        # instruction) before this ever reached app code: they come in as
        # int64 (running_count/reserved_qty are explicitly cast to int in
        # analysis/engine.py), and pandas raises a hard TypeError trying
        # to .loc-assign a string into an int64 column directly.
        #
        # That fix wasn't complete though - real error caught live in the
        # terminal, 2026-08-30: pyarrow.lib.ArrowInvalid ("Could not
        # convert '—' with type str: tried to convert to int64") every
        # time st.dataframe tried to serialize this table.
        # .astype(object) only changes the COLUMN's pandas dtype label -
        # the actual cells for non-volume-based rows are still real
        # Python int objects sitting in that object-dtype column right
        # next to "—" strings for volume-based rows. Arrow's own
        # from_pandas() inspects actual cell values (not just the pandas
        # dtype) to infer a single Arrow type per column, and a mix of
        # int and str cells is exactly what trips it - it committed to
        # int64 from the first rows it sampled, then choked on "—" later
        # in the same column. Streamlit silently recovers from this (logs
        # the traceback, then re-serializes with a fallback), so it never
        # surfaced as a visible crash - just a real error on every single
        # render of this table. Explicitly stringifying Running/Reserved's
        # real numeric values too (not just the "—" placeholder) makes
        # the column genuinely homogeneous - all str, nothing for Arrow to
        # misinfer - instead of relying on Streamlit's fallback path.
        _is_volume_based = show["coverage_model"] == "unmeasurable"
        for _col in ("Running", "Reserved"):
            show[_col] = show[_col].apply(lambda x: "—" if pd.isna(x) else str(int(x)))
            show.loc[_is_volume_based, _col] = "—"
        # Status is already an all-string column (built by _status()
        # above, every branch returns a str) - object-casting is a no-op
        # for dtype purposes, kept only so the "—" assignment below has a
        # column of the right dtype to write into.
        show["Status"] = show["Status"].astype(object)
        show.loc[_is_volume_based, "Status"] = "—"
        if has_pricing_cols:
            # Displayed as a monthly-equivalent (2026-08-29, real feedback:
            # a Reserved Instance isn't billed hour-by-hour the way a
            # Savings Plan genuinely is - RIs are bought for a fixed term
            # at a fixed price, upfront or in monthly installments, so a
            # bare "$/hr" figure here reads as if that's how the purchase
            # actually works). rate_col itself (the DataFrame column NAME)
            # stays "($/hr)" internally - that's the real hourly rate
            # analysis/commitment_economics.py::ri_gap_pricing() computes
            # and caches, needed as-is for the underlying $ math (gap *
            # (payg - rate) * 730) - only the DISPLAYED value is converted
            # (* 730) and re-labeled via column_config below; nothing about
            # the actual computation changes.
            #
            # Real inconsistency caught live, 2026-08-30 (user circled it
            # directly in a screenshot): ri_gap_pricing() only gates
            # Monthly Savings by coverage_model == "instance" - it prices
            # RI Rate for EVERY row regardless, since a raw rate lookup
            # doesn't care whether the row is a literal per-profile
            # purchase or not. That meant a Pooled row (e.g. "Azure SQL
            # Managed Instance Pool", coverage_model="capacity") could show
            # a real-looking "$687.33/mo" Rate with NO Monthly Savings next
            # to it to justify it - looked like a broken calculation, not
            # a deliberate choice, and directly contradicts the Coverage
            # Type column's own tooltip ("Pooled ... not a purchase
            # instruction"). A prior comment here claimed Pooled rows
            # "already come through as NaN" for rate too - confirmed false
            # via a direct script against real demo data (10 of 12 Pooled
            # rows had a real cached rate). Blanked explicitly here to
            # match Monthly Savings and the stated Coverage Type semantics:
            # only Per-Instance rows show either $ column now.
            #
            # SAME real inconsistency, second half - also caught live: a
            # Per-Instance row with gap == 0 (Fully Covered or Idle) could
            # STILL show a real RI Rate ("Azure Dedicated Host" -
            # $2,290.67/mo) with no Monthly Savings, for the identical
            # reason - ri_gap_pricing() only gates Savings by gap > 0, not
            # Rate. There's nothing to buy on a gap=0 row, so a purchase
            # rate has no purpose being shown either.
            _no_purchase_needed = show["coverage_model"] != "instance"
            if "gap" in show.columns:
                _no_purchase_needed = _no_purchase_needed | (show["gap"] == 0)
            show.loc[_no_purchase_needed, rate_col] = pd.NA
            show[rate_col] = show[rate_col].apply(lambda x: fmt(x * 730, 2) if pd.notna(x) else "—")
            show[savings_col] = show[savings_col].apply(lambda x: fmt(x, 2) if pd.notna(x) else "—")

        # Status Category (2026-08-30, table restructure - real feedback:
        # "structure it properly ... add filter feature like Inventory") -
        # a bucketed version of the free-text Status column, used ONLY for
        # filtering below. The real Status carries a per-row number
        # ("Short by 2", "Short by 5"), which would make a values-
        # checklist filter list dozens of near-duplicate entries instead
        # of a clean Fully Covered/Short/Idle/Not tracked bucket.
        def _status_category(val):
            if val == "—":
                return "Not tracked"
            if "Short by" in val:
                return "Short"
            if "Idle" in val:
                return "Idle"
            return "Fully Covered"
        show["Status Category"] = show["Status"].apply(_status_category)

        # ── Filter bar (real feedback: "add filter feature just like what
        # we have on Inventory") - reuses the same _value_checklist popover
        # + pill pattern and CSS scoping trick as Inventory's own filter
        # bar, scoped to this table's own 4 useful dimensions (Service,
        # Coverage Type, Status, Region) rather than porting Inventory's
        # whole FOCUS View/Columns-picker/URL-persistence machinery, which
        # solves problems specific to Inventory (a FOCUS spec view, a much
        # larger column set) that don't exist on this tab.
        _ri_filter_specs = [("Service", "Service"), ("Coverage Type", "Coverage Type"), ("Flexibility", "Flexibility"), ("Status Category", "Status"), ("Region", "Region")]
        _ri_label_to_col = {label: col for col, label in _ri_filter_specs}

        def _ri_filter_opts(label):
            col = _ri_label_to_col[label]
            normalized = show[col].astype(str)
            return normalized, sorted(normalized.unique().tolist())

        def _ri_bump(gen_key):
            st.session_state[gen_key] = st.session_state.get(gen_key, 0) + 1

        ri_filters_key = "ri_cov_active_filters"
        if ri_filters_key not in st.session_state:
            st.session_state[ri_filters_key] = []
        ri_active_filters = st.session_state[ri_filters_key]
        _ri_valid_labels = {l for _, l in _ri_filter_specs}
        if any(f["label"] not in _ri_valid_labels for f in ri_active_filters):
            ri_active_filters[:] = [f for f in ri_active_filters if f["label"] in _ri_valid_labels]

        with st.container(key="ri_cov_filter_bar"):
            st.markdown(
                """<style>
                div.st-key-ri_cov_filter_bar div[data-testid="stHorizontalBlock"] { gap: 0.5rem; }
                div.st-key-ri_cov_filter_bar div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
                    width: fit-content !important; flex: 0 0 auto !important; min-width: 0 !important;
                }
                div.st-key-ri_cov_filter_bar button[data-testid="stPopoverButton"] {
                    border-radius: 999px !important; border-color: #263349 !important;
                    transition: border-color 0.15s ease, background-color 0.15s ease, color 0.15s ease;
                }
                div.st-key-ri_cov_filter_bar button[data-testid="stPopoverButton"]:hover {
                    border-color: rgba(96,165,250,.5) !important; color: #60A5FA !important;
                }
                div.st-key-ri_cov_filter_row div[data-testid="stColumn"]:nth-of-type(1) button[data-testid="stPopoverButton"] {
                    border-style: dashed !important;
                }
                div.st-key-ri_cov_filter_row div[data-testid="stColumn"]:nth-of-type(n+2) button[data-testid="stPopoverButton"] {
                    background-color: rgba(96,165,250,.1) !important; border-color: rgba(96,165,250,.3) !important;
                    color: #BFDBFE !important;
                }
                div.st-key-ri_cov_filter_row div[data-testid="stColumn"]:nth-of-type(n+2) button[data-testid="stPopoverButton"]:hover {
                    background-color: rgba(96,165,250,.18) !important; border-color: rgba(96,165,250,.55) !important; color: #F1F5F9 !important;
                }
                /* Download CSV pushed to the far right of this row - see
                Inventory's identical rule for the full reasoning. */
                div.st-key-ri_cov_filter_row div[data-testid="stColumn"]:last-of-type {
                    margin-left: auto !important;
                }
                </style>""",
                unsafe_allow_html=True,
            )
            filter_row_container = st.container(key="ri_cov_filter_row")
            # +2, not +1 - see Inventory's identical pattern for the full
            # reasoning (dl_col is a deferred container reference, written
            # into once `show` is ready further down).
            filter_row = filter_row_container.columns(len(ri_active_filters) + 2)
            dl_col = filter_row[len(ri_active_filters) + 1]
            with filter_row[0]:
                add_gen_key = "ri_cov_addfilter_gen"
                add_gen = st.session_state.get(add_gen_key, 0)
                with st.popover("Add filter", icon=":material/add:", key=f"ri_cov_addfilter_popover_{add_gen}"):
                    available = [l for _, l in _ri_filter_specs if l not in [f["label"] for f in ri_active_filters]]
                    if not available:
                        st.caption("All filterable fields are already added.")
                    else:
                        st.markdown("**Filter results**")
                        pending_key = "ri_cov_pending_filter_field"
                        if st.session_state.get(pending_key) not in available:
                            st.session_state[pending_key] = available[0]
                        new_label = st.selectbox("Filter", available, key=pending_key)
                        _, opts = _ri_filter_opts(new_label)
                        new_values = _value_checklist(opts, key=f"ri_cov_addfilter_{new_label}_{add_gen}", defaults=[])
                        fc1, fc2 = st.columns(2)
                        if fc1.button("Apply", type="primary", width="stretch", key=f"ri_cov_addfilter_apply_{new_label}"):
                            ri_active_filters.append({"label": new_label, "values": new_values})
                            _ri_bump(add_gen_key)
                            st.rerun()
                        if fc2.button("Cancel", width="stretch", key=f"ri_cov_addfilter_cancel_{new_label}"):
                            _ri_bump(add_gen_key)
                            st.rerun()

            for i, f in enumerate(list(ri_active_filters)):
                with filter_row[i + 1]:
                    summary = "all" if not f["values"] else (f["values"][0] if len(f["values"]) == 1 else f"{len(f['values'])} selected")
                    edit_gen_key = f"ri_cov_editfilter_gen_{i}"
                    edit_gen = st.session_state.get(edit_gen_key, 0)
                    pill_col, x_col = st.columns(2)
                    with x_col:
                        if st.button("✕", key=f"ri_cov_editfilter_x_{i}", help=f"Remove {f['label']} filter"):
                            ri_active_filters.pop(i)
                            st.rerun()
                    with pill_col, st.popover(f"{f['label']} equals {summary}", key=f"ri_cov_editfilter_popover_{i}_{edit_gen}"):
                        _, opts = _ri_filter_opts(f["label"])
                        st.markdown("**Filter results**")
                        new_values = _value_checklist(opts, key=f"ri_cov_editfilter_{i}_{edit_gen}", defaults=f["values"])
                        fc1, fc2 = st.columns(2)
                        if fc1.button("Apply", type="primary", width="stretch", key=f"ri_cov_editfilter_apply_{i}"):
                            f["values"] = new_values
                            _ri_bump(edit_gen_key)
                            st.rerun()
                        if fc2.button("Remove filter", width="stretch", key=f"ri_cov_editfilter_remove_{i}"):
                            ri_active_filters.pop(i)
                            st.rerun()

        _ri_mask = pd.Series(True, index=show.index)
        for f in ri_active_filters:
            normalized, opts = _ri_filter_opts(f["label"])
            active_vals = f["values"] if f["values"] else opts
            _ri_mask &= normalized.isin(active_vals)
        show = show[_ri_mask]

        cols = ["Service", "SKU / Tier", "Region", "OS", "Coverage Type", "Flexibility", "Flex Group", "Running", "Reserved", "Status"]
        if has_pricing_cols:
            cols += [rate_col, savings_col]
        show = show[[c for c in cols if c in show.columns]]

        if show.empty:
            st.caption("No rows match the current filters.")
        else:
            # Same real pandas.Styler technique already used for the
            # Inventory tab's Power State and the Rightsizing tab's
            # Classification columns - color Status instead of adding a
            # badge column, since a canvas-rendered st.dataframe can't
            # render real badge widgets. Matches on substring, not a fixed
            # prefix (2026-08-30, table restructure) - Status now leads
            # with a plain "■" swatch glyph on every branch, not a
            # branch-specific emoji, so the text itself ("Short by"/
            # "Idle") is what distinguishes color now.
            def _status_color(val):
                if val == "—":
                    return "color: #64748B;"
                if "Short by" in val:
                    return "color: #FBBF24;"
                if "Idle" in val:
                    return "color: #60A5FA;"
                return "color: #34D399;"

            styled_show = show.style.map(_status_color, subset=["Status"])
            st.dataframe(
                styled_show, hide_index=True, width="stretch", row_height=42,
                column_config={
                    # Explicit widths for every column (2026-08-29, real
                    # feedback - a full pass across every table on this tab:
                    # sized to this app's own real longest values, same rule
                    # established this session for every other table here.
                    "Service":       st.column_config.TextColumn(pinned=True, width=230),
                    "SKU / Tier":    st.column_config.TextColumn(width=140),
                    "Region":        st.column_config.TextColumn(width=110),
                    "OS":            st.column_config.TextColumn(width=80),
                    # help= replaces the old per-row "Note" column
                    # (2026-08-30 restructure) - Note was empty for every
                    # Per-Instance row and only ever carried real content
                    # for Pooled/Volume-Based rows, duplicating exactly
                    # what Coverage Type already flags and what the
                    # Reservation Coverage Rules expander below explains in
                    # full - a mostly-blank 280px column wasn't earning its
                    # place. One header tooltip covers the same ground.
                    "Coverage Type": st.column_config.TextColumn(
                        width=110,
                        help="Per-Instance = literal 1-for-1 purchase, directly actionable. Pooled/Volume-Based "
                             "apply automatically across your subscription - treat their Status as a rough "
                             "signal, not a purchase instruction. See Reservation Coverage Rules below.",
                    ),
                    # Explains why a row's own Running/Reserved numbers can
                    # look surprising in isolation - "Flexible" means this
                    # row's reservation can cover (or be covered by) a
                    # DIFFERENT SKU in the same family, so a low/zero
                    # Running count next to a real Reserved count isn't
                    # necessarily a wasted purchase.
                    "Flexibility": st.column_config.TextColumn(
                        width=100,
                        help="Flexible = this reservation's capacity can move to/from other SKUs in the same "
                             "family (a real Azure/AWS mechanic). Not Flexible = excluded by a specific rule "
                             "(OS, family, an \"Off\" reservation, no peer SKU right now). N/A = this service "
                             "has no flexibility concept at all.",
                    ),
                    "Flex Group": st.column_config.TextColumn(
                        width=130,
                        help="For Flexible rows only: the real family/class-type name shared with the other "
                             "SKU(s) pooling capacity with this row. Match this text (within the same Region) "
                             "to find them.",
                    ),
                    "Running":       st.column_config.TextColumn(width=90),
                    "Reserved":      st.column_config.TextColumn(width=90),
                    # Widened 2026-08-29 (was 140) - Status can now carry a
                    # "· NN% pre-covered" partial-credit suffix (see _status()
                    # above), sized to fit that longest realistic real value
                    # rather than a generic guess - same "explicit width sized
                    # to the real longest value" rule established this session.
                    "Status":        st.column_config.TextColumn(width=260),
                    rate_col:        st.column_config.TextColumn(f"RI Rate ({ri_term_choice}) $/mo", width=140),
                    savings_col:     st.column_config.TextColumn("Monthly Savings", width=140),
                },
            )
            with dl_col:
                _csv_download_button(show, f"{selected_provider.lower()}_reservation_coverage", key="dl_ri_coverage")
            if not has_pricing_cols:
                st.caption(
                    ":material/info: Purchase-cost columns aren't shown - no cached pricing yet for these SKUs; "
                    "re-run a sync from the tenant's Manage dialog on the Home page."
                )
    else:
        st.caption("No reservation-trackable resources in inventory yet.")

    # ── Not-eligible resources (2026-08-29, real feedback) - the ONLY
    # thing left down here now that Pooled/Volume-Based rows moved into
    # the unified Per-Resource Coverage table above. Eligibility ("can
    # this resource type ever have a Reservation at all") is a genuinely
    # different question from coverage type ("how does an existing
    # Reservation apply to it"), so it stays its own section rather than
    # folding in too - a resource here has no coverage story to tell at
    # all, unlike every row in the table above.
    if not ineligible.empty:
        with st.expander(f"{len(ineligible)} resource(s) not eligible for any Reservation", icon=":material/block:", expanded=False):
            # Cards, not a dataframe (2026-08-30, real feedback - a
            # screenshot showed "Why not eligible" hard-clipped at the
            # table's right edge, mid-sentence, with no scrollbar). Same
            # root cause already fixed for Reservation Coverage Rules just
            # below this: st.dataframe cells don't wrap prose text
            # regardless of column width, and this app's global
            # overflow:hidden rule (needed to round the table wrapper's
            # corners) clips any content wider than the rendered column.
            #
            # Tabs by Service Category + real per-resource names inside
            # each card (2026-08-30 follow-up, real feedback: "same pattern
            # UI" as Reservation Coverage Rules below, and "mention
            # resource names as well or table like structure" - the
            # earlier SKU(s)/Region(s) summary caption wasn't enough to
            # identify which actual resource this is).
            #
            # `ineligible` (from `cov`) has no Resource Name at all - the
            # coverage table is grouped by PROFILE (Resource Type/SKU/
            # Region/OS/Redundancy), not by literal resource, so a profile
            # with running_count=3 is exactly one row here representing 3
            # real resources with no name captured at that level. Joins
            # back against inv_raw (the raw per-resource inventory, already
            # in scope for this whole tab) on the same profile key to
            # recover real names just for the ineligible profiles.
            _profile_keys = ["Resource Type", "SKU", "Region", "OS", "Redundancy"]
            _ineligible_keys = ineligible[_profile_keys].fillna("N/A").drop_duplicates()
            _inv_keyed = inv_raw.copy()
            _inv_keyed[_profile_keys] = _inv_keyed[_profile_keys].fillna("N/A")
            named = _inv_keyed.merge(_ineligible_keys, on=_profile_keys, how="inner")

            # CSV export (2026-08-30, real feedback: "if someone need to
            # export the list... how we can do it" - the cards-in-tabs
            # layout has no single scrollable table left to copy from, and
            # this is the first download_button anywhere in this app, so
            # there's no existing export pattern to match). One button for
            # the FULL list (every category, not just the open tab) -
            # exporting "the list" means the whole thing, and a user
            # working from a downloaded file shouldn't have to click
            # through every tab to reassemble it. Reuses `named` (already
            # has real per-resource names) joined back to `ineligible` for
            # the reason text, one row per real resource.
            _export_df = named.merge(
                ineligible[_profile_keys + ["eligibility_reason"]].fillna("N/A").drop_duplicates(),
                on=_profile_keys, how="left",
            )[["Resource Type", "Resource Name", "SKU", "Region", "eligibility_reason"]].rename(
                columns={"Resource Type": "Service", "eligibility_reason": "Why Not Eligible"}
            )
            _csv_download_button(_export_df, f"{selected_provider.lower()}_not_ri_eligible_resources", key="dl_ri_ineligible", right_aligned=True)

            # Grouped by (Service, reason), same reasoning as before - the
            # reason text is a function of the resource_type/SKU rule
            # (analysis/ri_eligibility.py), not the individual resource, so
            # a tenant with many resources of the same ineligible type
            # would otherwise repeat near-identical prose N times.
            svc_category = {svc: map_service_category(svc) for svc in ineligible["Resource Type"].unique()}
            _RI_CATEGORY_ORDER = ["Compute", "Databases", "Storage", "Analytics", "AI and Machine Learning", "Web and Mobile", "Other"]
            groups = ineligible.groupby(["Resource Type", "eligibility_reason"], dropna=False, sort=False)
            by_category: dict = {}
            for (svc, reason), grp in groups:
                by_category.setdefault(svc_category[svc], []).append((svc, reason, grp))
            present_categories = [c for c in _RI_CATEGORY_ORDER if c in by_category]

            cat_tabs = st.tabs(present_categories)
            for tab, cat in zip(cat_tabs, present_categories):
                with tab:
                    cat_groups = sorted(by_category[cat], key=lambda g: g[0])
                    grid_cols = st.columns(2)
                    for i, (svc, reason, grp) in enumerate(cat_groups):
                        with grid_cols[i % 2].container(border=True):
                            grp_keys = grp[_profile_keys].fillna("N/A").drop_duplicates()
                            grp_named = named.merge(grp_keys, on=_profile_keys, how="inner")
                            count = len(grp_named) if not grp_named.empty else int(grp["running_count"].sum())
                            plural = "s" if count != 1 else ""
                            st.markdown(f"**{svc}** · {count} resource{plural}")
                            st.markdown(f":material/block: {reason}")
                            if not grp_named.empty:
                                show_named = grp_named[["Resource Name", "SKU", "Region"]].rename(columns={"SKU": "SKU / Tier"})
                                st.dataframe(
                                    show_named, hide_index=True, width="stretch", row_height=32,
                                    column_config={
                                        "Resource Name": st.column_config.TextColumn(width=170),
                                        "SKU / Tier":     st.column_config.TextColumn(width=120),
                                        "Region":         st.column_config.TextColumn(width=100),
                                    },
                                )

    # ── Reference & Methodology - moved to the bottom (2026-08-29, real
    # feedback: this used to sit BEFORE the main table, meaning a user had
    # to scroll past "how this matching works" before ever reaching "am I
    # covered", the actual answer this tab exists to give). Kept as its
    # own expander, separate from Active Reservation Contracts just below
    # (2026-08-29 follow-up feedback: initially merged the two together,
    # but they're genuinely different kinds of content - this one is
    # methodology/interpretation guidance, that one is a raw data table -
    # and deserve their own section each rather than being stacked under
    # one shared header).
    with st.expander(f"{selected_provider} Reservation Coverage Rules", icon=":material/checklist:", expanded=False):
        if is_azure:
            from db.seed import RI_COVERAGE_NOTES
        else:
            from db.aws_seed import AWS_RI_COVERAGE_NOTES as RI_COVERAGE_NOTES
            # Standalone methodology caption removed entirely (2026-08-29,
            # real feedback: went through two rewrites trying to explain
            # AWS's size-flexibility reconciliation without sounding
            # alarming, and it still read as an unnecessary warning/source
            # of confusion) - the exceptions that matter are already
            # covered where they're actually relevant, in the individual
            # per-service cards below (e.g. the Oracle License Included
            # card explicitly says "NOT size-flexible"), so nothing
            # substantive is lost by dropping the summary banner.

        # Filtered to genuinely RI-eligible services (AWS only) + rendered
        # 2-up (2026-08-29, real feedback: this list was long, and ~6 of
        # the AWS entries were explicitly "NOT RI-eligible" in their own
        # text - duplicating the separate "Not RI-Eligible" section further
        # up this same tab verbatim, same conclusion shown twice). "AWS
        # Lambda (Managed Instances)" is a deliberate documentation-only
        # exception (see its own dict comment) - not a tracked
        # resource_type at all, so check_eligibility() would default it to
        # True for the wrong reason (no rule encoded, not "confirmed
        # eligible"); shown separately below instead of via this filter.
        #
        # AWS-only: Azure's RI_COVERAGE_NOTES has no such duplication (every
        # entry is a genuinely eligible service already) - applying the
        # same filter there was tried and reverted, since several Azure
        # eligibility rules pattern-match the real SKU (e.g. Synapse's
        # "DW<n>c" check, Data Explorer's Standard-vs-Basic check) and
        # return a defensive False for a placeholder "N/A" sku, which
        # would have wrongly hidden real, eligible Azure services that
        # simply don't special-case an unknown SKU the way VM/SQL DB/SQL
        # MI/App Service/Disk Storage's rules already do.
        _LAMBDA_NOTE_KEY = "AWS Lambda (Managed Instances)"
        if is_azure:
            eligible_notes = RI_COVERAGE_NOTES
        else:
            eligible_notes = {
                svc: note for svc, note in RI_COVERAGE_NOTES.items()
                if svc != _LAMBDA_NOTE_KEY and check_eligibility(svc, "N/A")[0]
            }

        # Tabs by Service Category (2026-08-30, real feedback: "can we have
        # a similar pattern to what we have for SP" - Savings Plan Coverage
        # Policy tabs by its own small set of official plan TYPES (Compute/
        # EC2 Instance/Database/SageMaker SP). Reserved Instances have no
        # equivalent small first-party taxonomy - they're just sold
        # per-service, not grouped into a handful of official "RI types"
        # the way Savings Plans genuinely are - so this reuses this app's
        # OWN existing FOCUS-based ServiceCategory grouping instead
        # (analysis/focus_mapping.py::map_service_category, already driving
        # Inventory's own Service Category filter) rather than inventing a
        # second, competing category scheme just for this tab.
        # _SERVICE_CATEGORY_MAP was extended with the handful of resource
        # types that only ever appear here (policy reference text, not live
        # inventory rows), so nothing lands in a junk-drawer "Other" tab.
        svc_category = {svc: map_service_category(svc) for svc in eligible_notes}
        if _LAMBDA_NOTE_KEY in RI_COVERAGE_NOTES:
            svc_category[_LAMBDA_NOTE_KEY] = "Compute"  # Lambda Managed Instances bills real EC2 compute - same bucket as EC2/Fargate.

        _RI_CATEGORY_ORDER = ["Compute", "Databases", "Storage", "Analytics", "AI and Machine Learning", "Web and Mobile", "Other"]
        by_category: dict = {}
        for svc, cat in svc_category.items():
            by_category.setdefault(cat, []).append(svc)
        present_categories = [c for c in _RI_CATEGORY_ORDER if c in by_category]

        # Cards, not a dataframe - same real bug already fixed on the
        # Savings Plan Coverage Policy card (2026-08-28): these Covers/
        # Excludes cells are paragraph-length prose, and st.dataframe cells
        # don't wrap text regardless of column width. 2-column grid within
        # each tab - each card is only 2-3 short lines, one-per-row wasted
        # half the width for no reason.
        def _render_ri_coverage_card(svc):
            if svc == _LAMBDA_NOTE_KEY:
                # Rendered as its own card (2026-08-29, real feedback: as a
                # plain st.caption() sitting below the grid, this real,
                # useful disclosure - Lambda genuinely IS RI-eligible, this
                # app just can't compute a gap for it - read as an
                # afterthought footnote easy to miss entirely). Same card
                # shell as its peers so it's discoverable in the same
                # reading flow. Distinguished from a real Covers/Excludes
                # card by using one "eye-off" line instead of two, so it
                # doesn't imply this app tracks it.
                st.markdown(f"**{_LAMBDA_NOTE_KEY}**")
                st.markdown(
                    ":material/visibility_off: Genuinely EC2 RI-eligible, but not shown as a gap/coverage row - "
                    "AWS exposes only pool-level configuration, never a per-instance count to compare against a reservation."
                )
                return
            covers, excludes = RI_COVERAGE_NOTES[svc]
            st.markdown(f"**{svc}**")
            st.markdown(f":material/check_circle: **Covers:** {covers}")
            st.markdown(f":material/block: **Excludes:** {excludes}")

        cat_tabs = st.tabs(present_categories)
        for tab, cat in zip(cat_tabs, present_categories):
            with tab:
                svcs = sorted(by_category[cat])
                grid_cols = st.columns(2)
                for i, svc in enumerate(svcs):
                    with grid_cols[i % 2].container(border=True):
                        _render_ri_coverage_card(svc)
        # No separate volume-based note here (2026-08-29, real follow-up
        # feedback: a standalone caption was added here first, then moved
        # again the same day) - Volume-Based rows now sit directly in the
        # unified Per-Resource Coverage table above, each with its own
        # "Note" column carrying this exact explanation per row. The
        # per-service Covers/Excludes cards above (Blob Storage, Files,
        # etc.) already cover what the reservation itself covers/excludes -
        # no third restatement needed here.

    with st.expander("Active Reservation Contracts", icon=":material/description:", expanded=False):
        if not ri_df.empty:
            ri_disp = ri_df[["commitment_id", "commitment_type", "scope_sku", "scope_region", "scope_os", "reserved_qty", "hourly_usd_commitment", "term", "expiry_date", "offering_class"]].copy()
            # Displayed as a monthly-equivalent (2026-08-29, real feedback -
            # same reasoning as the Per-Resource Coverage table's RI Rate
            # column above: an RI isn't billed hour-by-hour, so "/hr each"
            # here misrepresented how the purchase actually works. Only
            # this DISPLAY conversion changed - hourly_usd_commitment
            # itself stays hourly in the underlying ri_df/Commitment table,
            # same internal-hourly-basis reasoning as above. The column
            # header label is overridden to "Monthly Commitment" below,
            # since _SP_COMMITMENT_COLUMN_CONFIG's shared "Hourly
            # Commitment" label is correct for Savings Plans (a genuinely
            # native hourly commitment) and must stay that way there.
            ri_disp["hourly_usd_commitment"] = ri_disp["hourly_usd_commitment"].apply(lambda x: fmt(x * 730, 2) + "/mo each")
            # EC2-only (AWS): "standard"/"convertible" from AWS's own OfferingClass
            # field. N/A for Azure and for AWS RDS/ElastiCache/Redshift, which
            # have no such split - not a display gap, those services genuinely
            # don't have this concept per AWS's own docs.
            ri_disp["offering_class"] = ri_disp["offering_class"].fillna("N/A").apply(lambda v: v.title() if v != "N/A" else v)
            ri_disp = ri_disp.rename(columns={"offering_class": "Offering Class"})
            ri_disp_final = _with_mapping_caveat(ri_df, ri_disp)
            _csv_download_button(ri_disp_final, f"{selected_provider.lower()}_active_reservation_contracts", key="dl_ri_contracts", right_aligned=True)
            st.dataframe(
                ri_disp_final, hide_index=True, width="stretch",
                column_config={
                    **_SP_COMMITMENT_COLUMN_CONFIG,
                    "hourly_usd_commitment": st.column_config.TextColumn("Monthly Commitment", width=150),
                    "commitment_type": st.column_config.TextColumn("Type", width=170),
                    "scope_os":        st.column_config.TextColumn("OS", width=90),
                    "reserved_qty":    st.column_config.TextColumn("Reserved Qty", width=110),
                    "Offering Class":  st.column_config.TextColumn(width=120),
                },
            )
        else:
            st.info("No active Reserved Instance contracts found.")


def _real_projected_savings():
    """The commitment-pricing-based combined SP+RI savings projection - the
    MORE ACCURATE of this app's two savings figures (the other being
    generate_recommendations()'s safety-buffer heuristic, used for individual
    per-item $ impacts where real cached pricing isn't available). Returns
    None when real pricing genuinely isn't available so the caller can fall
    back to the heuristic total instead of showing a wrong/absent number.

    AWS branch added 2026-08-28: real Compute, SageMaker, and Database
    Savings Plan pricing is now available (aws_savings_plan_term_comparison,
    cached per-tenant - see _render_sp_pool_economics). RI pricing (added
    the same day) now uses the SAME ri_priced computation as Azure -
    ri_gap_pricing() and CommitmentPriceCache are provider-agnostic (see
    pricing/commitment_pricing.py's module docstring); only the SP side
    stays branched, since that mechanism genuinely differs by provider.
    Database's SP comparison DataFrame only has a real (non-$0) row for
    the "1yr" term_key (Database Savings Plans are 1-year-only) -
    combined_monthly_savings filters by the single shared sp_term, so if
    the Compute pool's term selector is set to "3yr" the Database
    contribution for that combined figure is correctly $0, not an error."""
    sp_term = st.session_state.get("sp_compute_term_widget", "1yr")
    ri_term = st.session_state.get("ri_term_widget", "1yr")
    ri_priced = (
        ri_gap_pricing(ri_result.coverage_table, inv_raw, prices_df)
        if (prices_df is not None and not prices_df.empty and not ri_result.coverage_table.empty)
        else pd.DataFrame()
    )
    if is_azure:
        if prices_df is None or prices_df.empty:
            return None
        sp_pool_cmps = []
        if not compute_24x7.empty:
            sp_pool_cmps.append(savings_plan_term_comparison(compute_24x7, prices_df))
        if not db_running.empty:
            sp_pool_cmps.append(savings_plan_term_comparison(db_running, prices_df))
        return combined_monthly_savings(sp_pool_cmps, ri_priced, sp_term, ri_term)
    else:
        sp_pool_cmps = []
        if not compute_24x7.empty:
            aws_cmp = aws_savings_plan_term_comparison(compute_24x7, active_tenant, "compute")
            if aws_cmp is not None:
                sp_pool_cmps.append(aws_cmp)
        if not db_running.empty:
            aws_db_cmp = aws_savings_plan_term_comparison(db_running, active_tenant, "database")
            if aws_db_cmp is not None:
                sp_pool_cmps.append(aws_db_cmp)
        if not sp_pool_cmps and ri_priced.empty:
            return None
        return combined_monthly_savings(sp_pool_cmps, ri_priced, sp_term, ri_term)


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYZE — RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════════════════
# Which tab actually owns the detail for each recommendation category -
# used by the pointer rows below instead of re-rendering that detail here.
_REC_CATEGORY_TAB = {
    "RI Leakage":                "RI Coverage",
    "RI Purchase Gap":           "RI Coverage",
    "RI Pooled Capacity Review": "RI Coverage",
    "RI Rebalance":              "RI Coverage",
    "Savings Plan Purchase":     "Savings Plan Analysis",
    "Savings Plan Leakage":      "Savings Plan Analysis",
}


def _render_recommendations_tab():
    st.subheader(f"{selected_provider} Savings Opportunity")
    st.caption(
        "What implementing the recommended Savings Plan and Reserved Instance purchases would actually "
        "get you - the one number neither the Savings Plan Analysis nor RI Coverage tab can answer alone."
    )
    _finops_tag("Optimize Usage & Cost", "Rate Optimization")

    # Redesigned 2026-08-30 - real user question ("isn't this already
    # covered under the Reservation section?") that turned out to be right,
    # traced precisely: every one of the 5 recommendation categories here
    # already has its own headline on RI Coverage or Savings Plan Analysis
    # (RI Coverage's own 2026-08-29 redesign already leads with a headline
    # + badges + the orphaned-RI drain alert as "the most urgent issue" -
    # the exact triage-first pattern this tab used to also claim as its own
    # value), and 3 of the 5 (RI Purchase Gap, RI Pooled Capacity Review,
    # RI Rebalance) carry a hardcoded financial_impact_hr of 0.0 in
    # generate_recommendations() - meaning this tab's old cards for those
    # were a WORSE copy (no $ figure) of what RI Coverage already shows
    # with real pricing via ri_gap_pricing(). The one thing that genuinely
    # doesn't exist anywhere else: a COMBINED number - RI Coverage only
    # knows about RI, Savings Plan Analysis only knows about SP, neither
    # can say what doing BOTH gets you. That combined figure
    # (_real_projected_savings(), already existed, just buried as one
    # metric among five redundant cards) is now this tab's entire focus.
    #
    # Special-cased below: "Live Connection" recs (no tenant connected / no
    # sync yet) are operational blockers, not optimization opportunities -
    # they can't be folded into a savings projection, so they're shown
    # as-is rather than forced into the layout below.
    if len(recs) == 1 and recs[0].get("category") == "Live Connection":
        r = recs[0]
        st.warning(f"**{r['title']}**\n\n{r['detail']}")
        st.info(f"**Next step:** {r.get('action', '')}")
        return

    # Orphaned Capacity dropped entirely, not just its old card - it's
    # already the FIRST thing RI Coverage shows (a styled st.error() alert,
    # "the most urgent issue" by that tab's own page-order design), so a
    # third restatement here (this tab used to have both a card AND count
    # it into the $ total) added noise, not coverage. real_savings below
    # never included its $ either (it only ever summed SP/RI PURCHASE
    # savings, not drain-avoidance) - unlike the old heuristic fallback,
    # which used to fold it in inconsistently.
    actionable = [r for r in recs if r.get("type") != "OPTIMAL" and r.get("category") != "Orphaned Capacity"]

    if not actionable:
        st.success(f"✅ Commitment portfolio is optimally configured for {selected_provider} - no additional Savings Plan or Reserved Instance purchases recommended right now.")
        return

    # Two savings figures exist in this app: this heuristic total (a rough,
    # always-available safety-buffer estimate) and the real commitment-
    # pricing-based projection (accurate, but needs cached pricing to
    # compute) - the real figure REPLACES the heuristic one whenever it's
    # available, one trustworthy number, not two competing ones.
    real_savings = _real_projected_savings()
    heuristic_savings_mo = sum(r.get("financial_impact_hr", 0.0) for r in actionable) * 730
    headline_savings_mo = real_savings["total_monthly_savings"] if real_savings else heuristic_savings_mo

    baseline_hr = float(
        inv_raw.loc[inv_raw["Resource State"] == "Running", "PAYG Hourly Cost USD"].sum()
    ) if not inv_raw.empty else 0.0
    baseline_mo = baseline_hr * 730
    pct_of_baseline = (headline_savings_mo / baseline_mo * 100) if baseline_mo > 0 else 0.0

    # No backslash-escaping of "$" here (an earlier version of this code
    # had one, ported from the Savings Plan Analysis tab's headline where
    # it's genuinely needed - real bug caught live, 2026-08-30: that
    # escape is a MARKDOWN-level convention (Streamlit's markdown parser
    # strips a leading "\" before rendering), but this whole block is raw
    # HTML via unsafe_allow_html=True - HTML has no such escape sequence,
    # so the literal backslash character was rendering on screen instead
    # of being stripped. Plain fmt() output is correct here.
    pct_note = f" — about {pct_of_baseline:.1f}% of your {fmt(baseline_mo, 2)}/mo on-demand baseline" if baseline_mo > 0 else ""
    st.markdown(
        '<div class="rec-headline-card">'
        '<div class="rec-headline-eyebrow">If implemented</div>'
        f'<div class="rec-headline-sentence">Implementing the recommended purchases would save '
        f'<b>{fmt(headline_savings_mo, 2)}/month</b>{pct_note}.</div>'
        '<div class="rec-headline-sub">'
        + ("Using real cached commitment pricing where available "
           f"({real_savings['sp_term']} Savings Plan, {real_savings['ri_term']} RI) - term choices made on "
           "the Savings Plan Analysis / RI Coverage tabs." if real_savings else
           "Safety-buffer estimate - real cached commitment pricing isn't available yet for this tenant's SKUs.")
        + '</div></div>',
        unsafe_allow_html=True,
    )

    if real_savings:
        m1, m2, m3 = st.columns(3)
        with m1:
            st.markdown(
                '<div class="rec-metric-card">'
                f'<div class="lbl">Savings Plan ({real_savings["sp_term"]})</div>'
                f'<div class="val fl-mono">{fmt(real_savings["sp_monthly_savings"], 2)}<span class="unit">/mo</span></div>'
                "</div>", unsafe_allow_html=True,
            )
        with m2:
            st.markdown(
                '<div class="rec-metric-card">'
                f'<div class="lbl">Reserved Instance ({real_savings["ri_term"]})</div>'
                f'<div class="val fl-mono">{fmt(real_savings["ri_monthly_savings"], 2)}<span class="unit">/mo</span></div>'
                "</div>", unsafe_allow_html=True,
            )
        with m3:
            st.markdown(
                '<div class="rec-metric-card combined">'
                '<div class="lbl">Combined Total</div>'
                f'<div class="val fl-mono">{fmt(real_savings["total_monthly_savings"], 2)}<span class="unit">/mo</span></div>'
                "</div>", unsafe_allow_html=True,
            )
        st.markdown("<div style='margin-bottom:22px;'></div>", unsafe_allow_html=True)

    # Grouped by destination tab, not one row per recommendation - real
    # feedback, 2026-08-30: with RI Coverage owning most categories, a
    # flat list repeated "→ RI Coverage" 3-4 times, most of each row's
    # width empty. One card per tab, items listed compactly underneath,
    # cuts the repetition and reads as "2 places to go," not "4 rows to
    # scan." Groups ordered by their own highest-severity item.
    severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    groups: dict[str, list] = {}
    for r in actionable:
        groups.setdefault(_REC_CATEGORY_TAB.get(r.get("category"), "Other"), []).append(r)

    st.caption("WHERE THIS COMES FROM")
    for tab_name, items in sorted(
        groups.items(),
        key=lambda kv: min(severity_rank.get(r.get("severity"), 3) for r in kv[1]),
    ):
        items_sorted = sorted(items, key=lambda r: severity_rank.get(r.get("severity"), 3))
        rows_html = "".join(
            '<div class="rec-group-item">'
            f'<span class="dot {"hi" if r.get("severity") == "HIGH" else "med"}"></span>'
            f'<span class="txt">{html.escape(r["title"])}</span>'
            "</div>"
            for r in items_sorted
        )
        st.markdown(
            '<div class="rec-group-card">'
            f'<div class="rec-group-head">→ {html.escape(tab_name)}</div>'
            f"{rows_html}"
            "</div>",
            unsafe_allow_html=True,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYZE — FINOPS MATURITY ASSESSMENT
# ═══════════════════════════════════════════════════════════════════════════════
# Plain-language pairing added 2026-08-30 - real feedback that "Crawl/Walk/
# Run" alone doesn't land without already knowing the term; every place
# this shows up now pairs it with a plain word, never bare.
_STAGE_STYLE = {
    "Run":               ("🟢", "Run", "Automated"),
    "Walk":               ("🔵", "Walk", "Has a process"),
    "Crawl":              ("🟡", "Crawl", "Manual, basic visibility"),
    "Below Crawl":        ("🔴", "Below Crawl", "Needs attention"),
    "Not Yet Measurable": ("⚪", "Not Yet Measurable", "Not tracked yet"),
}

# .rec-metric-card tone modifiers (ui/styling.py) already cover exactly
# these 5 semantic colors - green/blue/amber/red/gray - so this reuses the
# existing classes verbatim rather than adding new CSS.
_STAGE_TONE = {
    "Run": "savings", "Walk": "combined", "Crawl": "under",
    "Below Crawl": "over", "Not Yet Measurable": "neutral",
}

_CAPABILITY_LINKS = {
    "Rate Optimization": "the Savings Plan Analysis and RI Coverage tabs",
    "Usage Optimization": "the Inventory tab's orphaned-resource flags",
    "Anomaly Management": "the Recommendations tab",
    "Cost Allocation": "a roadmap item - needs resource tagging/ownership data not yet ingested",
    "Forecasting": "a roadmap item - needs a forecasting model not yet built",
}

# Usage Optimization/Anomaly Management now carry a real KPI badge (COIN,
# Anomaly Detection Rate) - reuses st.badge's color enum, same pattern
# already established on the RI Coverage tab's own badge row.
_KPI_BADGE_COLOR = {"good": "green", "warn": "orange", "bad": "red"}


def _render_maturity_tab():
    st.subheader(f"FinOps Maturity Assessment ({selected_provider})")
    st.caption(
        "Self-assessment against the FinOps Foundation's Crawl/Walk/Run Maturity Model "
        "(finops.org/framework/maturity-model), scored from this session's actual computed data."
    )
    _finops_tag("Manage the FinOps Practice", "FinOps Assessment")

    # Rewritten 2026-08-30 - real feedback ("I didn't understand this") on
    # the previous version, which led with the industry body ("The FinOps
    # Foundation... publishes a Maturity Model") before explaining what
    # the tab actually measures. Now leads with the plain question -
    # process maturity, not spend - then the stage names, then the
    # honesty policy, each its own short line rather than one dense
    # paragraph. Attribution line appended separately below (CC BY 4.0
    # per the FinOps Foundation's own brand guidance - the Framework's
    # CONTENT is openly licensed with attribution, distinct from their
    # separately-protected logo/trademark, which this app doesn't use).
    with st.expander("ℹ️ What is this, and why is it separate from Recommendations?", expanded=True):
        st.markdown(
            "Not how much you spend - **how systematic your process is.** Same spend, same coverage "
            "% - one tenant checks manually once a quarter, another's automated and continuous. "
            "Different maturity, same numbers.\n\n"
            "**Crawl → Walk → Run:** manual visibility → a repeatable process → fully automated.\n\n"
            "Real numbers only, checked against FinOps Foundation's published criteria. No data yet? "
            "Shown as **Not tracked yet** - never a fake score."
        )
        st.caption(
            "This tab implements the FinOps Foundation's Capability taxonomy and Maturity Model "
            "(Domains, Capabilities, Crawl/Walk/Run stages) - Framework content licensed **CC BY 4.0**, "
            "attributed to the [FinOps Foundation](https://www.finops.org/framework/). This app is an "
            "independent implementation, not a FinOps Foundation-certified product."
        )

    assessments = run_maturity_assessment(sp_result, ri_result, inv_raw, recs, currency=selected_currency, inr_rate=_inr_rate)

    stage_counts = {}
    for a in assessments:
        stage_counts[a.stage] = stage_counts.get(a.stage, 0) + 1
    # Gradient .rec-metric-card cards, not st.metric (2026-08-30, real
    # feedback: "bring it in line with the same KPI-card treatment" already
    # used on RI Coverage/Savings Plan Analysis/Recommendations/Rightsizing
    # - this was the one tab still on native st.metric). Same card markup,
    # same tone-class mechanism, just a 5-wide row instead of 4.
    for col, stage_key in zip(st.columns(5), _STAGE_STYLE.keys()):
        icon, label, plain = _STAGE_STYLE[stage_key]
        with col:
            st.markdown(
                f'<div class="rec-metric-card {_STAGE_TONE[stage_key]}">'
                f'<div class="lbl">{icon} {label}</div>'
                f'<div class="val fl-mono">{stage_counts.get(stage_key, 0)}</div>'
                f'<div class="sub">{plain}</div>'
                "</div>",
                unsafe_allow_html=True,
            )

    # Nudged up with a small negative margin, not a container-gap override
    # (2026-08-30, real feedback + screenshot: the first attempt at this,
    # wrapping both the card row and this box in one shared st.container
    # with a CSS `gap` rule, collapsed the space to near-zero instead of
    # tightening it - overrode more of Streamlit's own spacing behavior
    # than intended). This instead nudges JUST this element up by a small,
    # known amount relative to wherever it'd normally sit, an incremental
    # adjustment rather than a wholesale replacement of the block-spacing
    # mechanism - much lower risk of snapping to 0/overlapping.
    with st.container(key="fl_maturity_guidance_box"):
        st.markdown(
            '<style>div.st-key-fl_maturity_guidance_box { margin-top: -4px; }</style>',
            unsafe_allow_html=True,
        )
        st.info(
            "The Maturity Model's own guidance: *\"focus less on maturing each Capability to 'Run' "
            "for everything\"* - prioritize whichever capabilities deliver the highest business value "
            "for your organization next, rather than treating this as a checklist to max out."
        )

    st.divider()

    for a in assessments:
        icon, label, plain = _STAGE_STYLE.get(a.stage, ("⚪", a.stage, ""))
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            with c1:
                st.markdown(f"**{a.capability}** · _{a.domain}_")
                if a.kpi_label:
                    st.badge(a.kpi_label, color=_KPI_BADGE_COLOR.get(a.kpi_tone, "gray"))
                st.caption(a.headline)
            with c2:
                st.markdown(f"### {icon} {label}")
                st.caption(plain)
            with st.expander("Methodology & evidence", expanded=False):
                st.caption(a.detail)
                st.caption(f"**Evidence source:** {a.evidence}")
                st.caption(f"📍 **To improve this:** see {_CAPABILITY_LINKS.get(a.capability, 'the relevant tab above')}.")


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYZE — VM RIGHTSIZING (Azure only, v1 - see analysis/rightsizing.py)
# ═══════════════════════════════════════════════════════════════════════════════
def _render_rightsizing_tab():
    # "VM" is Azure-specific terminology - AWS's real compute resource is an
    # EC2 instance, not a "VM" (same distinction the Inventory tab's own
    # compute_label already makes: "Virtual Machines" vs "EC2 Instances").
    # Real bug caught by the user (2026-08-27): this tab used to hardcode
    # "VM Rightsizing" even while showing AWS EC2 rows.
    rightsizing_noun = "VM" if is_azure else "EC2"
    st.subheader(f"{selected_provider} {rightsizing_noun} Rightsizing")
    st.caption(
        f"Flags {compute_label.lower()} that are under- or over-provisioned relative to their real "
        "CPU/memory utilization, with configurable, industry-grounded thresholds."
    )
    _finops_tag("Optimize Usage & Cost", "Usage Optimization")

    key_prefix = f"{selected_provider}_{tenant_mode}_rightsizing"
    saved_settings = get_rightsizing_settings(active_tenant)

    for f in SETTINGS_FIELDS:
        wkey = f"{key_prefix}_{f}"
        if wkey not in st.session_state:
            st.session_state[wkey] = saved_settings[f]
    preset_key = f"{key_prefix}_preset"
    if preset_key not in st.session_state:
        st.session_state[preset_key] = next(
            (name for name, p in PRESETS.items()
             if all(saved_settings[f] == p[f] for f in SETTINGS_FIELDS)),
            "Custom",
        )

    def _apply_preset():
        choice = st.session_state[preset_key]
        if choice != "Custom":
            for f in SETTINGS_FIELDS:
                st.session_state[f"{key_prefix}_{f}"] = PRESETS[choice][f]

    # Both explanatory paragraphs that used to sit here (industry-practice
    # justification for the defaults, the Overutilized-OR/Underutilized-AND
    # classification rule) moved into a help tooltip - real feedback,
    # 2026-08-28: always-visible prose took up most of the popover before
    # you even reached the actual settings. First tried putting it on the
    # popover's own trigger button, which had a real bug: hovering the
    # trigger to open it leaves the tooltip stuck on screen since the
    # cursor never left that spot, overlapping the now-open panel. Moved
    # onto the Preset field instead, using the same (?) help-icon pattern
    # every other setting in this popover already uses - only shows once
    # the popover is already open, no overlap.
    rightsizing_help = (
        "Defaults follow real industry practice (Azure Advisor's resize thresholds, AWS Compute "
        "Optimizer's percentile/headroom/lookback model). A VM is Overutilized if CPU OR memory "
        "shows high pressure; it's only Underutilized if CPU AND memory (when memory data exists) "
        "are both low, so a VM idle on CPU but heavy on memory isn't wrongly flagged for a downsize."
    )
    # Grouped into labeled sections, 2026-08-30 (real feedback: match
    # Recommendations/Inventory's visual refresh) - same real widgets
    # throughout (st.selectbox/st.number_input/st.button), only grouping/
    # section labels/spacing change, approved via mockup first. Field
    # labels shortened where their group header now carries context a
    # longer label previously had to spell out alone (e.g. "CPU
    # Underutilized threshold (%)" -> "Underutilized (%)" under a "CPU
    # Thresholds" header) - same info, no longer repeated per field; each
    # field's own help= tooltip (unchanged) still carries the full
    # explanation regardless of the shorter visible label.
    def _rs_group_head(label: str):
        st.markdown(f'<div class="rs-group-head">{label}</div>', unsafe_allow_html=True)

    with st.popover("Rightsizing Settings", icon=":material/settings:"):
        st.selectbox("Preset", list(PRESETS.keys()) + ["Custom"], key=preset_key,
                     on_change=_apply_preset, help=rightsizing_help)

        _rs_group_head("⏱ Data Window")
        r1c1, r1c2 = st.columns(2)
        with r1c1:
            st.selectbox("Percentile", [90, 95, 99], key=f"{key_prefix}_percentile")
        with r1c2:
            st.number_input("Lookback (days)", min_value=1, max_value=93,
                             key=f"{key_prefix}_lookback_days")

        _rs_group_head("🖥 CPU Thresholds")
        r2c1, r2c2 = st.columns(2)
        with r2c1:
            st.number_input("Underutilized (%)", min_value=0.0, max_value=100.0,
                             step=5.0, key=f"{key_prefix}_cpu_under_pct",
                             help="Underutilized (CPU side) triggers when CPU usage drops below this.")
        with r2c2:
            st.number_input("Overutilized (%)", min_value=0.0, max_value=100.0,
                             step=5.0, key=f"{key_prefix}_cpu_over_pct",
                             help="Overutilized triggers when CPU usage rises above this.")

        _rs_group_head("🧠 Memory Thresholds")
        r3c1, r3c2 = st.columns(2)
        with r3c1:
            st.number_input("Underutilized (% used)", min_value=0.0, max_value=100.0,
                             step=5.0, key=f"{key_prefix}_mem_under_pct",
                             help="Underutilized (memory side) triggers when memory usage drops below this.")
        with r3c2:
            st.number_input("Overutilized (% avail)", min_value=0.0, max_value=100.0,
                             step=5.0, key=f"{key_prefix}_mem_available_pct",
                             help="Overutilized triggers when *available* (free) memory drops below this.")

        _rs_group_head("🛡 Safety & Minimum Data")
        r4c1, r4c2 = st.columns(2)
        with r4c1:
            st.number_input("Headroom (%)", min_value=0.0, max_value=50.0, step=5.0,
                             key=f"{key_prefix}_headroom_pct",
                             help="A suggested resize must leave at least this much headroom.")
        with r4c2:
            st.number_input("Min days of data", min_value=1, max_value=93,
                             key=f"{key_prefix}_min_days")

        if st.button("Save for this tenant", icon=":material/save:", key=f"{key_prefix}_save", width="stretch"):
            if active_tenant is None:
                st.warning("No active tenant to save to yet — connect or select one first.")
            else:
                update_rightsizing_settings(
                    selected_provider, tenant_mode, active_tenant.id,
                    {f: st.session_state[f"{key_prefix}_{f}"] for f in SETTINGS_FIELDS},
                )
                st.success("Saved for this tenant.")

    settings = {f: st.session_state[f"{key_prefix}_{f}"] for f in SETTINGS_FIELDS}

    st.divider()

    if vm_inventory.empty:
        st.caption("No VM inventory to analyze yet.")
        return

    # Percentile setting selects the aggregation the real per-provider metrics
    # service queries (Azure Monitor Metrics / Amazon CloudWatch, live for a
    # connected tenant - data/sync_pipeline.py, added 2026-09-01 after mentor
    # feedback that Rightsizing only ever worked in Demo; also still true for
    # demo/synced data - see data/inventory_loader.py); classification reads
    # the stored P95 columns regardless of the configured percentile - the
    # sync fetches one fixed Avg+P95 pair per sync run, not a live query
    # re-run for whatever percentile happens to be selected right now.
    # Disclosed simplification, not silently pretended to be arbitrary-
    # percentile-accurate. Threshold changes alone already drive real
    # classification differences without this, so nothing downstream is
    # faked.
    live_metrics_service = "Azure Monitor Metrics" if is_azure else "Amazon CloudWatch"
    st.caption(
        ":material/info: Classification reads the stored P95 CPU/Memory values (this app's synced "
        f"summary stats, refreshed each sync from live {live_metrics_service} data for a connected tenant). "
        "Full arbitrary-percentile aggregation on demand would require a live query per render — not built."
    )

    rows = []
    for _, vm in vm_inventory.iterrows():
        avg_cpu = vm.get("Avg CPU %")
        p95_cpu = vm.get("P95 CPU %")
        avg_mem = vm.get("Avg Memory Available %")
        p95_mem = vm.get("P95 Memory Available %")
        cpu_at_p = None if pd.isna(p95_cpu) else float(p95_cpu)
        mem_at_p = None if pd.isna(p95_mem) else float(p95_mem)
        days_of_data = settings["lookback_days"] if cpu_at_p is not None else None

        result = classify_vm_utilization(
            resource_state=vm["Resource State"],
            cpu_at_percentile=cpu_at_p,
            mem_available_at_percentile=mem_at_p,
            days_of_data=days_of_data,
            settings=settings,
        )

        suggested_sku, projected_cpu, projected_mem = None, None, None
        impact = None
        if result.label in ("Underutilized", "Overutilized") and cpu_at_p is not None:
            direction = "down" if result.label == "Underutilized" else "up"
            # Both metrics passed whenever available, regardless of which one
            # actually drove the verdict - a VM flagged Overutilized purely
            # by memory pressure still gets a real "after resize" memory
            # number (not just an irrelevant CPU one), and a downsize's
            # memory impact now gets safety-gated too, not only CPU's (see
            # suggest_target_instance_type's own docstring for the full
            # reasoning - extended 2026-08-26 after a direct user question
            # about this exact gap). suggest_target_instance_type dispatches
            # to the right SKU-naming parser for whichever provider is
            # active (Azure vCPU-in-name vs. AWS EC2 family.size ladder).
            suggestion = suggest_target_instance_type(
                selected_provider, vm["SKU"], direction, settings["headroom_pct"],
                cpu_utilization_pct=cpu_at_p, mem_available_pct=mem_at_p,
            )
            if suggestion:
                suggested_sku, projected_cpu, projected_mem = suggestion
                impact = estimate_resize_monthly_impact(
                    selected_provider, vm["SKU"], suggested_sku,
                    vm["PAYG Hourly Cost USD"], vm["Avg Daily Running Hours"],
                )

        rows.append({
            "VM Name": vm["Resource Name"],
            "SKU": vm["SKU"],
            # Raw values kept numeric (float | None) here - the display step
            # below turns a missing value into "Stopped"/"No agent" instead
            # of a bare None, but that's a presentation concern, not data;
            # "Resource State" rides along only to drive that formatting and
            # is dropped from the visible table, same as "Reason" isn't.
            "Resource State": vm["Resource State"],
            "Avg CPU %": None if pd.isna(avg_cpu) else round(float(avg_cpu), 1),
            "P95 CPU %": None if pd.isna(p95_cpu) else round(float(p95_cpu), 1),
            "Avg Memory Available %": None if pd.isna(avg_mem) else round(float(avg_mem), 1),
            "P95 Memory Available %": None if pd.isna(p95_mem) else round(float(p95_mem), 1),
            "Classification": result.label,
            "Why": result.reason,
            "Suggested SKU": suggested_sku or "—",
            # Kept numeric (float | None), not pre-stringified to "—" - a
            # column mixing float and str dtypes fails clean Arrow
            # serialization (Streamlit silently patches it, but it's sloppy
            # - a real warning caught in this tab's own server logs). Same
            # numeric-until-display pattern as Monthly Savings below.
            # Column names picked to be self-explanatory without requiring
            # the reader to already know this feature's jargon (renamed
            # 2026-08-26 from "Projected Utilization %"/"Est. Monthly
            # Impact" after a direct user question about what they meant).
            # "Monthly Savings" not "Monthly $ Impact" - a hardcoded "$" in
            # the header would mislead once selected_currency=INR (values
            # would render in ₹ via fmt() while the header still said "$",
            # a real bug caught by the user) - currency symbol belongs only
            # in the formatted cell value, never baked into a column name.
            "CPU % After Resize": projected_cpu,
            # *Available* %, matching the pre-resize "P95 Memory Available
            # %" column's own convention, so the two are directly
            # comparable side by side - None when no memory reading exists
            # for this VM (same "no agent" case, formatted the same way).
            "Memory % After Resize": projected_mem,
            "Monthly Savings": impact,
        })

    rs_df = pd.DataFrame(rows)

    under_count = int((rs_df["Classification"] == "Underutilized").sum())
    over_count = int((rs_df["Classification"] == "Overutilized").sum())
    total_savings = float(rs_df.loc[rs_df["Monthly Savings"] > 0, "Monthly Savings"].sum()) \
        if "Monthly Savings" in rs_df and rs_df["Monthly Savings"].notna().any() else 0.0

    # Visual-only refresh, 2026-08-30 (real feedback: match Recommendations/
    # Inventory's card language) - same .rec-metric-card family, colored to
    # this tab's own existing Classification palette (ui/styling.py).
    k1, k2, k3 = st.columns(3)
    with k1:
        st.markdown(
            '<div class="rec-metric-card under">'
            '<div class="lbl">📉 Underutilized</div>'
            f'<div class="val fl-mono">{under_count}</div>'
            "</div>", unsafe_allow_html=True,
        )
    with k2:
        st.markdown(
            '<div class="rec-metric-card over">'
            '<div class="lbl">📈 Overutilized</div>'
            f'<div class="val fl-mono">{over_count}</div>'
            "</div>", unsafe_allow_html=True,
        )
    with k3:
        st.markdown(
            '<div class="rec-metric-card savings">'
            '<div class="lbl">💳 Est. Monthly Savings</div>'
            f'<div class="val fl-mono">{fmt(total_savings, 2)}<span class="unit">/mo</span></div>'
            "</div>", unsafe_allow_html=True,
        )

    st.divider()

    class_opts = sorted(rs_df["Classification"].unique().tolist())
    with st.popover("Filter by Classification", icon=":material/filter_alt:"):
        picked_classes = _value_checklist(class_opts, key=f"{key_prefix}_class_filter", defaults=class_opts,
                                           search_label="Search classifications")
    if picked_classes:
        rs_df = rs_df[rs_df["Classification"].isin(picked_classes)]

    def _fmt_metric_cell(value, resource_state, is_memory):
        # A bare "None"/blank cell doesn't say WHY a metric is missing, and
        # the two real reasons look identical without this: a stopped VM
        # collects no telemetry at all (CPU and memory both blank), while a
        # running VM missing only memory is this app's disclosed inference
        # for a missing monitoring agent extension (Azure Monitor Agent /
        # CloudWatch Agent depending on provider) - CPU is host-level,
        # collected without any agent; memory needs the guest-level agent.
        # Not a confirmed diagnosis (v1 doesn't call either provider's real
        # Extensions/Agent-status API), so phrased as "No agent" rather
        # than a certain claim - full caveat lives in the "Why" column/
        # caption, this is just the short table-cell label.
        if pd.notna(value):
            return f"{value:.1f}"
        if resource_state != "Running":
            return "Stopped"
        return "No agent" if is_memory else "Not synced"

    show_df = rs_df.drop(columns=["Resource State"]).copy()
    for col, is_memory in [("Avg CPU %", False), ("P95 CPU %", False),
                            ("Avg Memory Available %", True), ("P95 Memory Available %", True)]:
        show_df[col] = rs_df.apply(lambda r, c=col, m=is_memory: _fmt_metric_cell(r[c], r["Resource State"], m), axis=1)
    show_df["CPU % After Resize"] = rs_df["CPU % After Resize"].apply(
        lambda x: f"{x:.1f}%" if pd.notna(x) else "—"
    )

    def _fmt_mem_after_resize(row):
        # Same "why is this blank" distinction as the pre-resize memory
        # columns, not a bare "—" for every empty cell: a VM with no
        # suggestion at all (Optimal/Unknown) genuinely has nothing to
        # project, but a VM that WAS resized-suggested with memory data
        # missing still gets the "No agent" wording, not a value-less dash
        # that looks identical to "not applicable."
        if pd.notna(row["Memory % After Resize"]):
            return f"{row['Memory % After Resize']:.1f}%"
        if row["Suggested SKU"] == "—":
            return "—"
        return _fmt_metric_cell(None, row["Resource State"], is_memory=True)

    show_df["Memory % After Resize"] = rs_df.apply(_fmt_mem_after_resize, axis=1)
    show_df["Monthly Savings"] = rs_df["Monthly Savings"].apply(
        lambda x: fmt(x, 2) if pd.notna(x) else "—"
    )
    st.caption(
        ":material/search: Use the table's own Search icon (toolbar) or the Classification filter above to narrow "
        "a large fleet — the **Why** column explains each VM's verdict inline instead of a separate list."
    )

    # Same real pandas.Styler technique already approved for the Inventory
    # tab's Power State column (2026-08-28) - color Classification instead
    # of adding a badge column, since a canvas-rendered st.dataframe can't
    # render badges anyway. Severity-matched to this app's established
    # palette: Overutilized=critical red (needs action now), Underutilized
    # =warning amber (wasting money, not urgent), Optimal=success green,
    # Unknown=muted gray (no data to classify from).
    def _class_color(val):
        return {
            "Optimal": "color: #34D399;",
            "Overutilized": "color: #F87171;",
            "Underutilized": "color: #FBBF24;",
        }.get(val, "color: #64748B;")

    styled_rs_df = show_df.style.map(_class_color, subset=["Classification"])

    _csv_download_button(show_df, f"{selected_provider.lower()}_rightsizing", key="dl_rightsizing", right_aligned=True)
    st.dataframe(
        styled_rs_df, hide_index=True, width="stretch",
        row_height=42,  # matches the Inventory tab's own row height, approved via mockup there
        column_config={
            # Pinned + width tuning (2026-08-28). Round 1 forced
            # width="small" (75px) onto columns with longer headers,
            # clipping the header itself. Round 2 tried leaving width unset
            # to let Streamlit auto-size instead - didn't reliably work
            # either (confirmed on the Inventory tab's table: its real
            # longest value still got clipped even past its header length),
            # so every column here gets an explicit width sized to whichever
            # is actually longer - this app's own real longest value
            # (_fmt_metric_cell's "Not synced"/"No agent"/"Stopped" labels,
            # Classification's "Underutilized"/"Overutilized") or the header
            # label - not a generic guess.
            "VM Name":       st.column_config.TextColumn(pinned=True, width=200),
            "SKU":           st.column_config.TextColumn(width=140),
            "Avg CPU %":     st.column_config.TextColumn(width=100),
            "P95 CPU %":     st.column_config.TextColumn(width=100),
            "Avg Memory Available %": st.column_config.TextColumn("Avg Mem Avail %", width=130),
            "P95 Memory Available %": st.column_config.TextColumn("P95 Mem Avail %", width=130),
            "Classification": st.column_config.TextColumn(width=130),
            "Why":           st.column_config.TextColumn(width=380),
            "Suggested SKU": st.column_config.TextColumn(width=140),
            "CPU % After Resize": st.column_config.TextColumn(
                "CPU After Resize", width=140,
                help="CPU usage this VM would have if you applied the Suggested SKU — lets you "
                     "sanity-check a resize before making it (e.g. a downsize projecting above the "
                     "safe range wouldn't have been suggested at all)."
            ),
            "Memory % After Resize": st.column_config.TextColumn(
                "Mem After Resize", width=140,
                help="Available (free) memory % this VM would have after the Suggested SKU — same "
                     "units as the P95 Memory Available % column, so you can compare before/after "
                     "directly. Confirms whether the resize actually relieves memory pressure when "
                     "that's what triggered Overutilized, not just CPU. \"No agent\" when this VM has "
                     "no memory reading to project from."
            ),
            "Monthly Savings": st.column_config.TextColumn(
                width=130,
                help="Estimated monthly cost change from applying the Suggested SKU, in your selected "
                     "display currency. Positive = savings (downsize). Negative = added cost (upsize "
                     "needed to relieve overutilization)."
            ),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# WORKSPACE — ANALYZE (single page, top tabs inside)
# ═══════════════════════════════════════════════════════════════════════════════
def page_analyze():
    _render_top_header()
    tabs = st.tabs([
        ":material/inventory_2: Inventory",
        f":material/target: {'VM' if is_azure else 'EC2'} Rightsizing",
        ":material/payments: Savings Plan Analysis",
        ":material/local_offer: RI Coverage",
        ":material/lightbulb: Recommendations",
        ":material/explore: Maturity Assessment",
    ])
    with tabs[0]:
        _render_inventory_tab()
    with tabs[1]:
        _render_rightsizing_tab()
    with tabs[2]:
        _render_savings_plan_tab()
    with tabs[3]:
        _render_ri_coverage_tab()
    with tabs[4]:
        _render_recommendations_tab()
    with tabs[5]:
        _render_maturity_tab()


# ─────────────────────────────────────────────────────────────────────────────
# NAVIGATION — flat sidebar (Home / Tenant Management / User Management /
# Help & Support), per approved sketch 2026-08 - no section headers, no
# separate "Workspace" entry. Help & Support added 2026-08-30 - real
# onboarding gap the user caught: setup guidance for a first tenant
# connection previously had no home before a tenant existed to Manage.
# Analyze is still a real registered page (needed for
# st.switch_page to work at all) but visibility="hidden" keeps it out of the
# sidebar - it's reached only via a tenant's own "Dashboard" button in
# Tenant Management, never browsed to directly, since analyzing data only
# makes sense once a specific tenant is active.
# ─────────────────────────────────────────────────────────────────────────────
home_page        = st.Page(page_home,              title="Home",              icon=":material/home:", default=True)
tenant_mgmt_page = st.Page(page_tenant_management,  title="Tenant Management", icon=":material/domain:")
users_page       = st.Page(page_users,              title="User Management",   icon=":material/group:")
help_page        = st.Page(page_help,               title="Help & Support",    icon=":material/help:")
analyze_page     = st.Page(page_analyze,            title="Analyze",           icon=":material/insights:", visibility="hidden")

pg = st.navigation([home_page, tenant_mgmt_page, users_page, help_page, analyze_page], position="sidebar")

# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR CONTROLS — rendered below the nav menu above. Only cross-cutting
# controls live here now (platform, data source, currency, account) -
# Commitment Term / Safety Buffer moved into the sections that actually use
# them (Savings Plan Analysis, RI Coverage). Analysis Window was here too
# until it was removed entirely, 2026-08-30 - see the note near
# DEFAULT_SIMULATE_DAYS below.
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    def reset_app_cache():
        st.cache_data.clear()

    # Icon column + content column per section (2026-08-30, "rail" redesign
    # round 2, real feedback: "some new visuals" for the bordered-box
    # version, which read as repetitive stacked boxes) - a colored icon
    # badge per section (Cloud=blue, Display=green, Account=violet) using
    # real st.columns(), not the mockup's absolute-positioned icon-on-a-
    # line technique (see ui/styling.py's own note above .fl-rail-node for
    # why that got simplified rather than guessed blind). No borders, no
    # dividers - generous margin-bottom on each section instead.
    _cloud_ic, _cloud_ct = st.columns([1, 8])
    with _cloud_ic:
        st.markdown(f'<div class="fl-rail-node cloud">{sidebar_icon("cloud")}</div>', unsafe_allow_html=True)
    with _cloud_ct:
        st.markdown('<div class="fl-rail-title">Cloud &amp; Data Source</div>', unsafe_allow_html=True)

        selected_provider = st.segmented_control(
            "Cloud Platform",
            options=["Azure", "AWS"],
            default=initial_provider,
            key="provider_selector_widget",
            on_change=reset_app_cache,
            help="Select cloud environment to optimize.",
        )
        if not selected_provider:
            selected_provider = initial_provider

        # Re-asserted on every rerun (not just written once) - same reasoning
        # as currency/the login token just below: Streamlit's sidebar nav
        # links don't reliably carry a query param forward unless it's
        # freshly rewritten on every single script run, not just at the
        # moment it was first set.
        st.query_params["provider"] = selected_provider

        # Deliberately NOT a free-switching widget - see
        # ui.auth_page.render_switch_mode_control's docstring for why: demo
        # and live are separate login accounts and separate data now, so
        # switching means signing out and back in, not flipping a toggle
        # mid-session.
        env_mode = render_switch_mode_control()

    st.markdown('<div style="height:26px;"></div>', unsafe_allow_html=True)

    _disp_ic, _disp_ct = st.columns([1, 8])
    with _disp_ic:
        st.markdown(f'<div class="fl-rail-node display">{sidebar_icon("card")}</div>', unsafe_allow_html=True)
    with _disp_ct:
        st.markdown('<div class="fl-rail-title">Display</div>', unsafe_allow_html=True)

        # format_func only changes the DISPLAYED label ("$ USD"/"₹ INR") -
        # the widget's real return value stays the bare "USD"/"INR" string
        # every downstream fmt_currency()/selected_currency == "INR" check
        # throughout this app already expects, so this is display-only,
        # zero behavior change.
        selected_currency = st.segmented_control(
            "Display Currency",
            options=["USD", "INR"],
            format_func=lambda v: {"USD": "$ USD", "INR": "₹ INR"}[v],
            default=initial_currency,
            key="currency_selector_widget",
            help="Toggle between US Dollar and Indian Rupee (live exchange rate).",
        )
        # Real bug reported 2026-08-29 (screenshot: sidebar caption showed
        # "Currency USD" and the whole Analyze page priced in USD, right after
        # clicking a sidebar nav link with INR selected, even though the
        # toggle widget ITSELF still visually showed INR highlighted). Root
        # cause: st.segmented_control can transiently return None for one
        # rerun right after a sidebar nav-link navigation (before the
        # frontend fully resyncs component state) - the old fallback then
        # read `initial_currency`, which comes from st.query_params, and
        # that URL can be stale after a nav-link click (same "sidebar nav
        # links don't reliably carry query params forward" limitation this
        # file's own comments already document for the login session token
        # just below - never fully closed for currency specifically). The
        # widget's OWN session_state entry stays correct the whole time
        # (proven by the toggle's own correct visual state in that exact
        # screenshot) - preferred first, before falling back to the
        # URL-derived value, which is only reliable on a genuinely fresh
        # page load (no prior selection to fall back to at all).
        if not selected_currency:
            selected_currency = st.session_state.get("currency_selector_widget") or initial_currency

        # Update query parameters to persist selection across page refreshes
        st.query_params["currency"] = selected_currency

        # Same reasoning, same fix pattern - the login session token (?s=...,
        # see ui/auth_page.py) needs this exact same "re-set on every rerun"
        # treatment. Real bug caught on video 2026-08-20: Streamlit's sidebar
        # multi-page nav links (Home/Tenant Management/User Management) do NOT
        # reliably carry a query param forward into their href when it was only
        # ever set once (at login) - one click on a nav link and it's silently
        # gone from the address bar, so the next ordinary refresh (now on a URL
        # with no token) correctly but unhelpfully bounces back to login.
        # "currency" survives the identical navigation only because it's
        # unconditionally re-set on every single rerun, right here - mirroring
        # that pattern (and this exact location, not require_login()'s early
        # fast path - that placement broke sidebar rendering entirely when
        # tried) is what actually fixes it.
        _session_token = st.session_state.get("_session_token")
        if _session_token:
            st.query_params["s"] = _session_token

    st.markdown('<div style="height:26px;"></div>', unsafe_allow_html=True)

    _acct_ic, _acct_ct = st.columns([1, 8])
    with _acct_ic:
        st.markdown(f'<div class="fl-rail-node account">{sidebar_icon("lock")}</div>', unsafe_allow_html=True)
    with _acct_ct:
        st.markdown('<div class="fl-rail-title">Account</div>', unsafe_allow_html=True)
        render_logout_control()
        render_change_password_control()

# ─────────────────────────────────────────────────────────────────────────────
# CURRENCY — Live exchange rate & fmt() helper
# ─────────────────────────────────────────────────────────────────────────────
_inr_rate = get_inr_rate() if selected_currency == "INR" else 84.0

def fmt(x: float, decimals: int = 2) -> str:
    """Format x using the active display currency (USD or INR)."""
    return fmt_currency(x, decimals=decimals, currency=selected_currency, inr_rate=_inr_rate)

# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING & ENVIRONMENT SELECTION
# ─────────────────────────────────────────────────────────────────────────────
is_azure = (selected_provider == "Azure")
provider_icon = "☁️ Azure" if is_azure else "🟧 AWS"
compute_label = "Virtual Machines" if is_azure else "EC2 Instances"
# Renamed 2026-08-23: AWS's primary compute resource_type used to be the
# same literal "Compute" string as Azure's - both providers shared one
# flat resource_type namespace, so that one string was the sole accidental
# collision point (every other resource type already has a distinct
# "Amazon .../AWS ..." name). AWS now uses "Amazon EC2" - see
# aws/connector.py's live-fetch comment for the full reasoning.
compute_type = "Compute" if is_azure else "Amazon EC2"
db_label = "Database Services" if is_azure else "RDS Databases"
db_sp_title = "Savings Plan for Databases" if is_azure else "Database Savings Plan"
db_eligible_types = DATABASE_SP_ELIGIBLE_TYPES if is_azure else AWS_DATABASE_SP_TYPES
compute_sp_eligible_types = COMPUTE_SP_ELIGIBLE_TYPES if is_azure else AWS_COMPUTE_SP_TYPES
# SageMaker Savings Plans are an AWS-only product - no Azure equivalent, so
# this stays an empty set for Azure (matching how the rest of this section
# already keys everything off is_azure rather than a provider-name check).
sagemaker_sp_eligible_types = set() if is_azure else AWS_SAGEMAKER_SP_TYPES

# Was a user-facing "Analysis Window (days)" control on the Cost Analysis
# tab (that whole tab has since been removed too, same day - see
# page_analyze()'s tab list) - the control itself was pulled first (real
# user question + investigation): it never read real historical data (no
# date dimension exists anywhere in this app's schema - CloudInventory is
# a point-in-time snapshot, overwritten on every sync), and for a real
# Production tenant the per-resource signal it replayed (Avg Daily Running
# Hours) is hardcoded to 24 in both live connectors anyway - the control's
# 7/14/30 choice couldn't actually change what it modeled. Fixed at the
# engine's own existing default instead of exposing a decorative knob.
simulate_days = DEFAULT_SIMULATE_DAYS
# Safety Buffer now lives inside the Savings Plan Analysis tab.
safety_buffer_pct = st.session_state.get("sp_safety_buffer_widget", int(DEFAULT_SAFETY_BUFFER * 100))
safety_buffer = safety_buffer_pct / 100.0


# Tenants now exist in both scopes (2026-08) - Demo's Home page gets a real
# seeded tenant entry too, not just Production's real connections. Every
# db.tenants call needs to know which one explicitly, same discipline as the
# rest of the demo/live split.
tenant_mode = "live" if env_mode == "Production" else "demo"

# Check Live Credentials — the connected-tenant registry in SQL DB is the single
# source of truth for "is live configured", not the .env file (which only exists
# to pre-fill the connection form / support the cron function outside Streamlit).
active_tenant = get_active_tenant(selected_provider, tenant_mode)
is_live_configured = active_tenant is not None
is_live_mode = (env_mode == "Production")

@st.cache_data(show_spinner="Loading Demo data...")
def load_benchmark_data(days: int, buffer: float, provider: str, sp_eligible_types: tuple, currency: str, inr_rate: float):
    inv_raw          = get_compute_inventory(provider=provider, mode="demo")
    sp_df            = get_existing_savings_plans(provider=provider, mode="demo")
    compute_sp_df    = get_compute_savings_plans(provider=provider, mode="demo")
    db_sp_df         = get_database_savings_plans(provider=provider, mode="demo")
    sagemaker_sp_df  = get_sagemaker_savings_plans(provider=provider, mode="demo")
    ri_df            = get_existing_reservations(provider=provider, mode="demo")
    # Overwrites whatever "Is Orphaned" the DB row carried with the real,
    # computed value - see compute_orphaned_status's docstring for why this
    # can't stay a static stored flag. Must happen before run_waterfall/
    # reservation_analysis below, since both already read this column for
    # their own orphan-drain calculations.
    if not inv_raw.empty:
        inv_raw["Is Orphaned"] = compute_orphaned_status(inv_raw, ri_df)
    wf            = run_waterfall(inv_raw, ri_df, sp_df, simulate_days=days)
    sp_res        = savings_plan_analysis(inv_raw, sp_df, safety_buffer=buffer, eligible_types=list(sp_eligible_types))
    flex_groups_df = get_vm_flexibility_groups(get_engine(provider, "demo")) if provider == "Azure" else None
    # Region-aware eligibility (2026-08-30) - a live per-region signal from
    # this tenant's own synced pricing cache overrides the static family
    # rules in analysis/ri_eligibility.py wherever it exists (see that
    # module's check_eligibility() for why - a static list is a real
    # maintenance liability; two real errors, NP-series and HC-series VMs,
    # were caught precisely because they'd been marked globally ineligible
    # from a single-region scan). Fetched here rather than reused from the
    # module-level `prices_df` (app.py, further down) because that variable
    # doesn't exist yet at the point this cached function is called -
    # same DB read either way, just done locally instead of threaded in.
    ri_prices_df = get_commitment_prices(get_engine(provider, "demo"), provider=provider)
    ri_res        = reservation_analysis(inv_raw, ri_df, flex_groups_df, ri_prices_df)
    # currency/inr_rate (2026-08-29, real feedback) - explicit params on
    # THIS cached function too, not just generate_recommendations() - a
    # @st.cache_data function's return value is cached by its OWN
    # argument set, so if currency weren't part of THIS signature,
    # switching Display Currency wouldn't invalidate this cache and would
    # keep returning recs with stale-currency text baked in.
    recs          = generate_recommendations(sp_res, ri_res, wf, safety_buffer=buffer, currency=currency, inr_rate=inr_rate)
    return inv_raw, sp_df, compute_sp_df, db_sp_df, sagemaker_sp_df, ri_df, sp_res, ri_res, recs

@st.cache_data(show_spinner="Loading your live tenant data...")
def load_live_data(provider: str, tenant_id: int, days: int, buffer: float, sp_eligible_types: tuple, currency: str, inr_rate: float):
    """Same shape as load_benchmark_data, but reads ONLY the given tenant's
    live-ingested rows (tenant_id FK) from SQL DB - never demo/seed rows, and
    never another tenant's rows. The app never calls cloud APIs directly here;
    everything was already fetched and cached in SQL DB by the ingestion pipeline."""
    inv_raw          = get_compute_inventory(provider=provider, mode="live", tenant_id=tenant_id)
    sp_df            = get_existing_savings_plans(provider=provider, mode="live", tenant_id=tenant_id)
    compute_sp_df    = get_compute_savings_plans(provider=provider, mode="live", tenant_id=tenant_id)
    db_sp_df         = get_database_savings_plans(provider=provider, mode="live", tenant_id=tenant_id)
    sagemaker_sp_df  = get_sagemaker_savings_plans(provider=provider, mode="live", tenant_id=tenant_id)
    ri_df            = get_existing_reservations(provider=provider, mode="live", tenant_id=tenant_id)
    # Same real, computed "Is Orphaned" as load_benchmark_data - this is the
    # actual fix for live tenants: every live connector previously hardcoded
    # this column to False, so a genuinely orphaned resource never surfaced
    # anywhere in the app. Must happen before run_waterfall/
    # reservation_analysis below, since both already read this column for
    # their own orphan-drain calculations.
    if not inv_raw.empty:
        inv_raw["Is Orphaned"] = compute_orphaned_status(inv_raw, ri_df)
    wf            = run_waterfall(inv_raw, ri_df, sp_df, simulate_days=days)
    sp_res        = savings_plan_analysis(inv_raw, sp_df, safety_buffer=buffer, eligible_types=list(sp_eligible_types))
    flex_groups_df = get_vm_flexibility_groups(get_engine(provider, "live")) if provider == "Azure" else None
    # Region-aware eligibility (2026-08-30) - see load_benchmark_data's own
    # comment above for the full rationale. Matters even more here than in
    # demo mode: a real production tenant's actual region is exactly the
    # kind of live fact a static, single-region-researched list can't see.
    ri_prices_df = get_commitment_prices(get_engine(provider, "live"), provider=provider)
    ri_res        = reservation_analysis(inv_raw, ri_df, flex_groups_df, ri_prices_df)
    # currency/inr_rate - see load_benchmark_data's own comment on why
    # these must be explicit params of THIS cached function, not just
    # generate_recommendations()'s.
    recs          = generate_recommendations(sp_res, ri_res, wf, safety_buffer=buffer, currency=currency, inr_rate=inr_rate)
    return inv_raw, sp_df, compute_sp_df, db_sp_df, sagemaker_sp_df, ri_df, sp_res, ri_res, recs

if is_live_mode and not is_live_configured:
    # Strict Live Mode with NO Connection: return empty data state
    cols = ["Resource ID", "Resource Name", "Resource Type", "Resource State", "Region", "OS", "SKU", "PAYG Hourly Cost USD", "Avg Daily Running Hours", "Subscription", "Resource Group", "Availability Zone", "Provider", "Is Orphaned"]
    inv_raw = pd.DataFrame(columns=cols)
    sp_df = pd.DataFrame(columns=["commitment_id", "commitment_type", "scope_sku", "scope_resource_type", "scope_region", "scope_os", "scope_redundancy", "scope_subscription_id", "scope_resource_group_id", "scope_availability_zone", "hourly_usd_commitment", "reserved_qty", "term", "expiry_date", "provider"])
    compute_sp_df = sp_df.copy()
    db_sp_df = sp_df.copy()
    sagemaker_sp_df = sp_df.copy()
    ri_df = sp_df.copy()
    from analysis.engine import SPAnalysisResult, RIAnalysisResult, WaterfallResult
    sp_result = SPAnalysisResult(0.0, 0.0, 0.0, 0.0, 0.0, safety_buffer, [])
    ri_result = RIAnalysisResult(pd.DataFrame(), pd.DataFrame())
    recs = [{
        "type": "ACTION_REQUIRED", "severity": "HIGH", "icon": "⚠️", "category": "Live Connection",
        "title": f"No Live {selected_provider} Connection Configured",
        "detail": f"You are in **Production** mode, but no tenant is connected for {selected_provider}. Please configure your API access on the **🏠 Home** page.",
        "action": "Connect a tenant on the Home page.",
        "financial_impact_hr": 0.0, "items": [],
    }]
elif is_live_mode and is_live_configured:
    inv_raw, sp_df, compute_sp_df, db_sp_df, sagemaker_sp_df, ri_df, sp_result, ri_result, recs = load_live_data(
        selected_provider, active_tenant.id, simulate_days, safety_buffer,
        tuple(compute_sp_eligible_types | db_eligible_types | sagemaker_sp_eligible_types),
        selected_currency, _inr_rate,
    )
    if inv_raw.empty:
        recs = [{
            "type": "ACTION_REQUIRED", "severity": "MEDIUM", "icon": "ℹ️", "category": "Live Connection",
            "title": f"No Resources Synced Yet for '{active_tenant.tenant_name}'",
            "detail": "This tenant is connected, but no live inventory has been ingested yet. Go to the **🏠 Home** page and run a sync from its Manage dialog.",
            "action": "Run a manual sync from the tenant's Manage dialog on the Home page.",
            "financial_impact_hr": 0.0, "items": [],
        }]
else:
    inv_raw, sp_df, compute_sp_df, db_sp_df, sagemaker_sp_df, ri_df, sp_result, ri_result, recs = load_benchmark_data(
        simulate_days, safety_buffer, selected_provider,
        tuple(compute_sp_eligible_types | db_eligible_types | sagemaker_sp_eligible_types),
        selected_currency, _inr_rate,
    )

# Real Savings Plan / Reserved Instance commitment pricing cache (Phase A) -
# a lightweight local DB read (the API calls already happened during
# sync/seed), so it's not wrapped in @st.cache_data.
prices_df = get_commitment_prices(get_engine(selected_provider, "live" if is_live_mode else "demo"), provider=selected_provider)

# ─────────────────────────────────────────────────────────────────────────────
# DERIVED SLICES & METRICS
# ─────────────────────────────────────────────────────────────────────────────
if not inv_raw.empty:
    vm_inventory = inv_raw[inv_raw["Resource Type"] == compute_type].copy()
    db_inventory = inv_raw[inv_raw["Resource Type"].isin(db_eligible_types)].copy()
    # Everything else (Storage, Redis, Synapse, Databricks, ...) - previously
    # silently invisible in the Inventory tab since it matched neither bucket above.
    other_inventory = inv_raw[
        (inv_raw["Resource Type"] != compute_type) & ~inv_raw["Resource Type"].isin(db_eligible_types)
    ].copy()
    # Broader than vm_inventory: everything the Compute Savings Plan pool
    # actually covers per Azure policy (VMs, App Service, Functions Premium,
    # Container Instances, Dedicated Host, ...), not just literal VMs.
    compute_sp_pool_inventory = inv_raw[inv_raw["Resource Type"].isin(compute_sp_eligible_types)].copy()
    # SageMaker Endpoints/Notebook Instances - AWS-only, a genuinely separate
    # SP pool from Compute above (see sagemaker_sp_eligible_types).
    sagemaker_sp_pool_inventory = inv_raw[inv_raw["Resource Type"].isin(sagemaker_sp_eligible_types)].copy()
else:
    vm_inventory = pd.DataFrame(columns=["Resource ID", "Resource Name", "Resource Type", "Resource State", "Region", "OS", "SKU", "PAYG Hourly Cost USD", "Avg Daily Running Hours", "Subscription", "Provider", "Is Orphaned"])
    db_inventory = vm_inventory.copy()
    other_inventory = vm_inventory.copy()
    compute_sp_pool_inventory = vm_inventory.copy()
    sagemaker_sp_pool_inventory = vm_inventory.copy()

compute_sp_commit   = float(compute_sp_df["hourly_usd_commitment"].sum())   if not compute_sp_df.empty else 0.0
db_sp_commit        = float(db_sp_df["hourly_usd_commitment"].sum())        if not db_sp_df.empty else 0.0
sagemaker_sp_commit = float(sagemaker_sp_df["hourly_usd_commitment"].sum()) if not sagemaker_sp_df.empty else 0.0
orphaned_count    = int(inv_raw["Is Orphaned"].sum()) if not inv_raw.empty else 0
high_recs         = sum(1 for r in recs if r["severity"] == "HIGH")

# Steady-state (24x7) SP-eligible pools - computed once, shared by the
# Savings Plan Analysis tab and the Recommendations tab's real-pricing
# projection (_real_projected_savings()) so both work from the exact same
# slice instead of recomputing the eligibility split twice.
compute_24x7_candidates = compute_sp_pool_inventory[
    (compute_sp_pool_inventory["Resource State"] == "Running") &
    (compute_sp_pool_inventory["Avg Daily Running Hours"] == 24)
]
compute_24x7, compute_sp_excluded = _split_sp_eligible(compute_24x7_candidates, is_azure)
db_running_candidates = db_inventory[db_inventory["Resource State"] == "Running"]
db_running, db_sp_excluded = _split_sp_eligible(db_running_candidates, is_azure)
sagemaker_24x7_candidates = sagemaker_sp_pool_inventory[
    (sagemaker_sp_pool_inventory["Resource State"] == "Running") &
    (sagemaker_sp_pool_inventory["Avg Daily Running Hours"] == 24)
]
sagemaker_24x7, sagemaker_sp_excluded = _split_sp_eligible(sagemaker_24x7_candidates, is_azure)

# ─────────────────────────────────────────────────────────────────────────────
# RUN THE SELECTED PAGE
# ─────────────────────────────────────────────────────────────────────────────
pg.run()

# Refreshed 2026-08-30, real feedback: read as too prominent for fine print
# (bold "Legal & Financial Notice:" lead-in at the same visual weight as
# body text, wide centered paragraph) and sat awkwardly far from the page
# bottom on short pages like Home, with a fixed 100px margin-top compounding
# whatever empty space the page's own short content already left above it.
# Restyled as genuine fine print (small-caps muted label instead of a bold
# phrase, smaller explicit px size, left-aligned instead of centered,
# tighter margins) matching how mature financial apps present this kind of
# notice - de-emphasized, not a featured block.
#
# True viewport-bottom pinning added 2026-08-30, second round - real
# feedback that the first pass still hugged short pages' own content
# (User Management screenshot showed it right under the user list). The
# .fl-page-footer class is the hook ui/styling.py's CSS targets (via
# :has()) to push this footer's real flex-item wrapper to the bottom of
# the page - see that CSS rule's own comment for the full mechanism
# (confirmed against the installed Streamlit build's own compiled JS
# bundle, not guessed). The 56px margin-top here still applies underneath
# that on a LONG page, where the flex auto-margin has no leftover space to
# use - keeps real breathing room above the footer either way.
st.markdown("""
<div class="fl-page-footer" style="background-color: transparent; border-top: 1px solid rgba(255,255,255,0.08); padding: 24px 0px 20px 0px; margin-top: 56px;">
    <div style="max-width: 1200px; margin: 0 auto;">
        <p style="font-size: 11px; color: #64748b; line-height: 1.6; margin: 0 0 6px;">
            <span style="text-transform: uppercase; letter-spacing: .05em; font-weight: 600; color: #94a3b8; font-size: 10px;">Legal &amp; Financial Notice</span>
            &nbsp;·&nbsp; Multi-Cloud FinOps Optimization System is an independent cloud financial decision support platform. Cost calculations, projected savings, and commitment recommendations are provided for analytical modeling purposes. Official billing figures must be verified in Microsoft Azure Cost Management or AWS Cost Explorer prior to purchasing commitment contracts. Microsoft Azure and Amazon Web Services (AWS) are registered trademarks of their respective owners.
        </p>
        <div style="font-size: 10.5px; color: #475569;">
            © 2026 Multi-Cloud FinOps Optimization System. All rights reserved.
        </div>
    </div>
</div>
""", unsafe_allow_html=True)
