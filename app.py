"""
app.py — Multi-Cloud FinOps Optimization System (Azure & AWS)
Enterprise Cloud Cost & Commitment Engine

Left sidebar (Manage): Tenants, User Management.
Left sidebar (Workspace): Analyze — a single page whose top tabs are
  Inventory | Rightsizing | Savings Plan Analysis | RI Coverage |
  Recommendations | Maturity Assessment.
"""

import sys, os, glob, html
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

@st.cache_resource
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
from ui.auth_page import require_login, render_logout_control, render_switch_mode_control
current_user = require_login()

# require_login() only injects CSS on the pre-auth screens (it returns early
# once already authenticated) - apply it here too so the setup gate and main
# dashboard get the same card/button polish.
from ui.styling import inject_global_css
inject_global_css()

# Imports
from data.inventory_loader import get_compute_inventory
from data.sync_pipeline import run_ingestion_pipeline
from pricing.retail_pricing import usd, fmt_currency, get_inr_rate
from pricing.commitment_pricing import get_commitment_prices, MONTH_HOURS
from pricing.azure_vm_flexibility import get_vm_flexibility_groups
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
)
from db.tenants import (
    list_tenants, get_active_tenant, get_tenant_credentials, upsert_tenant,
    update_tenant_name, touch_last_synced, set_active_tenant, delete_tenant,
    resource_count, list_subscriptions, upsert_subscription,
    update_tenant_permission_status, record_sync_result, update_sync_interval,
    update_aws_account_id, update_rightsizing_settings,
)
from analysis.rightsizing import (
    classify_vm_utilization, get_rightsizing_settings, suggest_target_instance_type,
    estimate_resize_monthly_impact, PRESETS, SETTINGS_FIELDS,
)
from azure_conn.connector import (
    AzureCredentials, load_credentials_from_env, save_credentials_to_env_file,
    test_connection, check_role_assignments, check_tenant_role_assignments,
    list_accessible_subscriptions, status_from_role_check,
    REQUIRED_ROLES, REQUIRED_SUBSCRIPTION_ROLES, REQUIRED_TENANT_ROLES,
    HAS_AZURE_IDENTITY,
)
from aws.connector import (
    AWSCredentials, load_aws_credentials_from_env, save_aws_credentials_to_env_file,
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
def _render_azure_connect_form(key_prefix: str, mode: str = "live"):
    """Renders the credential form + Test/Connect buttons and handles both
    actions. Returns True if a connect just succeeded (caller may want to
    st.rerun() immediately rather than wait for the natural rerun)."""
    env_azure = load_credentials_from_env()

    with st.form(f"{key_prefix}_azure_form"):
        col1, col2 = st.columns(2)
        with col1:
            az_name   = st.text_input("Tenant / Subscription Name", value="Prod Azure Tenant", placeholder="e.g. Main Production Azure", key=f"{key_prefix}_az_name")
            az_tenant = st.text_input("Tenant ID", value=env_azure.tenant_id if env_azure else "", placeholder="f946c54c-e759-4985-bbe0-3e166cef8fa0", key=f"{key_prefix}_az_tenant")
            az_client = st.text_input("Client ID (Application ID)", value=env_azure.client_id if env_azure else "", placeholder="84644394-8136-4625-9d36-564fc9c0b5e7", key=f"{key_prefix}_az_client")
            az_domain = st.text_input("Domain (optional)", value="", placeholder="contoso.onmicrosoft.com", key=f"{key_prefix}_az_domain")
        with col2:
            az_sub = st.text_input("Subscription ID", value=env_azure.subscription_id if env_azure else "", placeholder="e0b96fd6-b891-4a5f-80db-6cfed14e62ab", key=f"{key_prefix}_az_sub")
            az_sec = st.text_input("Client Secret", value=env_azure.client_secret if env_azure else "", type="password", key=f"{key_prefix}_az_sec")

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
                if test_res["success"]:
                    st.success(f"✅ **Permission Audit Passed!** {test_res['message']}")
                    st.info(f"📋 **Accessible Subscriptions in Tenant ({len(test_res['subscriptions'])}):** {', '.join(test_res['subscriptions'])}")
                    st.caption(f"Verified Roles: {', '.join(test_res['roles_verified'])}")
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
                if res["status"] == "SUCCESS":
                    st.success(f"🎉 **Live Tenant Ingestion Complete!** {res['message']}")
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
            "or switch to **Demo / Benchmark Mode** in the sidebar to view sample data.",
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
def _render_aws_connect_form(key_prefix: str, mode: str = "live"):
    """AWS's equivalent of _render_azure_connect_form - simpler, since AWS
    live ingestion (EC2/RDS via boto3) isn't built yet; this only registers
    the account in the tenant registry."""
    with st.expander("📖 Instructions: How to obtain AWS IAM Access Keys", expanded=False):
        st.markdown("""
1. Sign in to the **AWS Management Console** and open the **IAM Console**.
2. In the navigation pane, choose **Users**, then select your user or create a dedicated `FinOpsOptimizerUser`.
3. Choose the **Security credentials** tab.
4. Under **Access keys**, click **Create access key**.
5. Select **Command Line Interface (CLI)** and copy the generated credentials:
   - **Access Key ID**
   - **Secret Access Key**
6. Attach these AWS managed policies (verified against AWS's own policy reference pages, not guessed - use **🧪 Test Access Permissions** below to check for real, and it'll report these exact same policy names):
""")
        # Generated FROM REQUIRED_AWS_POLICIES (aws/connector.py), not
        # hand-typed a second time here - keeps this list and the live Test
        # checklist saying the exact same managed-policy name for the exact
        # same action, permanently, instead of the two silently drifting out
        # of sync the way they had before (real gap the user caught live).
        for policy in REQUIRED_AWS_POLICIES:
            action = policy["Policy / Action"]
            managed_policy = policy["AWS Managed Policy"]
            if managed_policy.startswith("("):
                st.markdown(f"   - `{action}` — {managed_policy}")
            else:
                st.markdown(f"   - `{action}` → attach **`{managed_policy}`**")
        st.caption(
            "Only 9 managed policies to attach in total, despite 13 actions above - EC2 and RDS "
            "each cover both the inventory scan and the Reserved Instances read (`Describe*` "
            "family), so nothing extra is needed for RI data beyond what's already required for "
            "inventory. `AWSPriceListServiceFullAccess` is safe despite the name - the Pricing "
            "service has no mutating actions at all, so \"full access\" just means every "
            "read-only pricing action. DocumentDB and Neptune inventory need no extra policy "
            "either - both are authorized via plain `rds:DescribeDBInstances`, already required "
            "above. DMS and Fargate genuinely have no dedicated AWS-managed read-only policy at "
            "all (confirmed against AWS's own docs) - attach a small custom inline policy for "
            "those two actions, or the broad `ReadOnlyAccess` policy."
        )
    env_aws = load_aws_credentials_from_env()
    with st.form(f"{key_prefix}_aws_form"):
        col1, col2 = st.columns(2)
        with col1:
            aws_key = st.text_input("AWS Access Key ID", value=env_aws.access_key_id if env_aws else "", placeholder="AKIAXXXXXXXXXXXXXXXX")
            aws_reg = st.text_input("Default AWS Region", value=env_aws.region if env_aws else "us-east-1", placeholder="us-east-1")
        with col2:
            aws_sec = st.text_input("AWS Secret Access Key", value=env_aws.secret_access_key if env_aws else "", type="password")

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
        aws_sync_icon = {"SUCCESS": "✅", "FAILED": "❌", "PARTIAL": "⚠️"}.get(t.last_sync_status, "")
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
                st.success("Tenant name updated.")
                st.rerun()

        if active_aws_section == "Credentials":
            if is_demo:
                st.caption("Not applicable - a demo tenant has no real AWS credentials behind it.")
            else:
                with st.form(f"mgmt_aws_creds_{t.id}"):
                    aws_key_edit = st.text_input("AWS Access Key ID", value=t.client_id)
                    aws_reg_edit = st.text_input("Default AWS Region", value=t.tenant_id)
                    aws_sec_edit = st.text_input("AWS Secret Access Key", value="", type="password",
                                                  placeholder="Leave blank to keep the current secret")
                    if st.form_submit_button("Save credentials", type="primary"):
                        secret_to_save = aws_sec_edit if aws_sec_edit else get_tenant_credentials(t)
                        upsert_tenant(
                            provider="AWS", mode=mode, tenant_name=new_name or t.tenant_name,
                            tenant_id=aws_reg_edit, subscription_id=aws_reg_edit,
                            client_id=aws_key_edit, client_secret=secret_to_save,
                        )
                        st.success("Credentials updated.")
                        st.rerun()
        elif active_aws_section == "Permissions":
            if is_demo:
                st.caption("Not applicable - a demo tenant has no real AWS credentials behind it.")
            else:
                st.caption("Checked live against AWS each time - not persisted, same as the Add a new tenant form's test.")
                if st.button("🔁 Check permissions", disabled=is_demo, key=f"mgmt_aws_check_{t.id}"):
                    creds = AWSCredentials(t.client_id, get_tenant_credentials(t), t.tenant_id)
                    with st.spinner("Checking IAM permissions..."):
                        check = check_aws_permissions(creds)
                        conn_check = test_aws_connection(creds)
                    if conn_check["success"]:
                        update_aws_account_id(selected_provider, mode, t.id, conn_check["account_id"])
                    if check["checked"]:
                        _render_aws_permission_checklist(check["results"])
                    else:
                        st.error(f"❌ Could not check permissions: {check['error']}")
        elif active_aws_section == "Sync":
            if t.last_sync_status == "SUCCESS":
                st.success(t.last_sync_message or "Last sync succeeded.", icon="✅")
            elif t.last_sync_status == "PARTIAL":
                st.warning(t.last_sync_message or "Last sync partially completed.", icon="⚠️")
            elif t.last_sync_status == "FAILED":
                st.error(t.last_sync_message or "Last sync failed.", icon="❌")
            else:
                st.caption("No sync attempted yet.")

            current_interval = t.sync_interval_hours if t.sync_interval_hours in _SYNC_INTERVAL_LABELS else 24
            i1, i2 = st.columns([3, 2])
            new_interval = i1.selectbox(
                "Automated sync interval", options=list(_SYNC_INTERVAL_LABELS.keys()),
                format_func=lambda h: _SYNC_INTERVAL_LABELS[h],
                index=list(_SYNC_INTERVAL_LABELS.keys()).index(current_interval),
                key=f"mgmt_aws_interval_{t.id}", disabled=is_demo,
                help="A single hourly cron checks every tenant and only re-syncs the ones due, based on this setting.",
            )
            i2.caption("")
            if i2.button("Save schedule", disabled=is_demo, key=f"mgmt_aws_save_interval_{t.id}", width="stretch"):
                update_sync_interval(selected_provider, mode, t.id, new_interval)
                st.success("Sync schedule updated.")
                st.rerun()

            st.caption(f"Last synced: {t.last_synced_at[:16] if t.last_synced_at else 'Never'}")

            if st.button("⚡ Run sync now", disabled=is_demo, key=f"mgmt_aws_run_sync_{t.id}", type="primary",
                         help="Only available for Production tenants." if is_demo else "Fetches live EC2/RDS inventory from this tenant right now."):
                with st.spinner(f"Running ingestion for '{t.tenant_name}'..."):
                    sync_creds = AWSCredentials(t.client_id, get_tenant_credentials(t), t.tenant_id)
                    res = run_ingestion_pipeline(selected_provider, creds=sync_creds, tenant_db_id=t.id)
                record_sync_result(selected_provider, mode, t.id, res["status"], res["message"])
                st.cache_data.clear()
                st.rerun()

        st.divider()
        if st.button("🗑️ Delete tenant", disabled=is_demo, key=f"mgmt_delete_{t.id}"):
            delete_tenant(selected_provider, mode, t.id)
            st.session_state["_manage_tenant_id"] = None
            st.rerun()
        return

    subs = list_subscriptions(selected_provider, mode, t.id)
    sub_ready = bool(subs) and all(s.permission_status == "ready" for s in subs)
    sub_missing = any(s.permission_status == "missing_role" for s in subs)
    sub_error = any(s.permission_status == "error" for s in subs)
    sub_icon = "✅" if sub_ready else "❌" if sub_error else "⚠️" if sub_missing else ""
    tenant_icon = {"ready": "✅", "error": "❌", "missing_role": "⚠️"}.get(t.tenant_permission_status, "")
    sync_icon = {"SUCCESS": "✅", "FAILED": "❌", "PARTIAL": "⚠️"}.get(t.last_sync_status, "")

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
            st.success("Tenant name updated.")
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
                                                placeholder="Leave blank to keep the current secret")
                creds_submitted = st.form_submit_button("Save credentials", type="primary")
            if creds_submitted:
                secret_to_save = new_secret if new_secret else get_tenant_credentials(t)
                upsert_tenant(
                    provider=selected_provider, mode=mode, tenant_name=new_name or t.tenant_name,
                    tenant_id=new_tenant_id, subscription_id=t.subscription_id,
                    client_id=new_client_id, client_secret=secret_to_save, domain=new_domain or None,
                )
                st.success("Credentials updated.")
                st.rerun()

        with st.expander("📖 How to set up this Service Principal", expanded=False):
            st.markdown("""
**1. Create the Service Principal:**
```bash
az ad sp create-for-rbac --name "finops-optimizer-sp" --role "Reader" --scopes /subscriptions/<SUBSCRIPTION_ID> --output json
```

**2. Assign subscription-level roles** (Reader + Cost Management Reader) for each subscription this tenant should cover:
```bash
az role assignment create --assignee <CLIENT_ID> --role "Reader" --scope /subscriptions/<SUB_ID>
az role assignment create --assignee <CLIENT_ID> --role "Cost Management Reader" --scope /subscriptions/<SUB_ID>
```
To cover every subscription in the tenant at once instead of one at a time:
```bash
for sub in $(az account list --query "[].id" -o tsv); do
  az role assignment create --assignee <CLIENT_ID> --role "Reader" --scope "/subscriptions/$sub"
  az role assignment create --assignee <CLIENT_ID> --role "Cost Management Reader" --scope "/subscriptions/$sub"
done
```

**3. Assign tenant-level roles** for Reservations and Savings Plans - a separate permission system, not subscription-scoped, since neither is a subscription resource. This step needs **User Access Administrator** rights at the tenant level - a materially higher bar than step 2, and many accounts (e.g. student/trial subscriptions) genuinely can't get it:
```bash
az role assignment create --assignee <CLIENT_ID> --role "Reservations Reader" --scope "/providers/Microsoft.Capacity"
az role assignment create --assignee <CLIENT_ID> --role "Savings Plan Reader" --scope "/providers/Microsoft.BillingBenefits"
```
Missing this step is **not fatal** - Resource inventory and cost data (step 2) sync independently of Reservations/Savings Plan data (step 3); a sync will report which parts succeeded.
""")

    # ── Subscriptions ─────────────────────────────────────────────────────
    elif active_section == "Subscriptions":
        if st.button("🔁 Sync subscriptions", disabled=is_demo, key=f"mgmt_sync_subs_{t.id}",
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
                    st.error(s.missing_role or "Could not check", icon="❌")
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

    # ── Tenant-wide permissions (Reservations / Savings Plans) ──────────
    elif active_section == "Tenant-wide permissions":
        st.caption("Reservations and Savings Plans are tenant-wide resources with their own separate permission system, not covered by the subscription-level roles above.")
        if t.tenant_permission_status in ("ready", "missing_role"):
            _render_role_checklist(REQUIRED_TENANT_ROLES, t.tenant_permission_status, t.tenant_assigned_roles)
            if t.tenant_permission_status == "missing_role":
                st.caption("Needs User Access Administrator at the tenant level - often unavailable on student/trial accounts. Inventory and cost data are unaffected; only Reservations/Savings Plan data needs this.")
        elif t.tenant_permission_status == "error":
            st.error(t.tenant_missing_roles or "Could not check", icon="❌")
        else:
            st.caption("Not checked yet.")
        if st.button("🔁 Check tenant permissions", disabled=is_demo, key=f"mgmt_tenant_check_{t.id}",
                      help="Only available for Production tenants." if is_demo else None):
            with st.spinner("Checking tenant-level permissions..."):
                _run_tenant_permission_check(t, mode)
            st.rerun()

        with st.expander("📋 Required Azure RBAC roles", expanded=False):
            st.markdown("**Subscription-scoped**")
            st.dataframe(pd.DataFrame(REQUIRED_SUBSCRIPTION_ROLES)[["Role Name", "Scope", "Purpose"]], hide_index=True, width="stretch")
            st.markdown("**Tenant-scoped**")
            st.dataframe(pd.DataFrame(REQUIRED_TENANT_ROLES)[["Role Name", "Scope", "Purpose"]], hide_index=True, width="stretch")

    # ── Sync ───────────────────────────────────────────────────────────
    elif active_section == "Sync":
        if t.last_sync_status == "SUCCESS":
            st.success(t.last_sync_message or "Last sync succeeded.", icon="✅")
        elif t.last_sync_status == "PARTIAL":
            st.warning(t.last_sync_message or "Last sync partially completed.", icon="⚠️")
        elif t.last_sync_status == "FAILED":
            st.error(t.last_sync_message or "Last sync failed.", icon="❌")
        else:
            st.caption("No sync attempted yet.")

        current_interval = t.sync_interval_hours if t.sync_interval_hours in _SYNC_INTERVAL_LABELS else 24
        i1, i2 = st.columns([3, 2])
        new_interval = i1.selectbox(
            "Automated sync interval", options=list(_SYNC_INTERVAL_LABELS.keys()),
            format_func=lambda h: _SYNC_INTERVAL_LABELS[h],
            index=list(_SYNC_INTERVAL_LABELS.keys()).index(current_interval),
            key=f"mgmt_interval_{t.id}", disabled=is_demo,
            help="A single hourly cron checks every tenant and only re-syncs the ones due, based on this setting.",
        )
        i2.caption("")
        if i2.button("Save schedule", disabled=is_demo, key=f"mgmt_save_interval_{t.id}", width="stretch"):
            update_sync_interval(selected_provider, mode, t.id, new_interval)
            st.success("Sync schedule updated.")
            st.rerun()

        st.caption(f"Last synced: {t.last_synced_at[:16] if t.last_synced_at else 'Never'}")

        if st.button("⚡ Run sync now", disabled=is_demo, key=f"mgmt_run_sync_{t.id}", type="primary",
                     help="Only available for Production tenants." if is_demo else "Fetches live inventory, reservations, and savings plans from this tenant right now."):
            with st.spinner(f"Running ingestion for '{t.tenant_name}'..."):
                sync_creds = AzureCredentials(t.tenant_id, t.subscription_id, t.client_id, get_tenant_credentials(t))
                res = run_ingestion_pipeline(selected_provider, creds=sync_creds, tenant_db_id=t.id)
            record_sync_result(selected_provider, mode, t.id, res["status"], res["message"])
            st.cache_data.clear()
            st.rerun()

    # ── Features (placeholder) ────────────────────────────────────────
    elif active_section == "Features":
        st.caption("Nothing here yet - reserved for upcoming tenant-level features.")

    st.divider()
    if st.button("🗑️ Delete tenant", disabled=is_demo, key=f"mgmt_delete_prod_{t.id}",
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
            total_payg_hr += float(inv["PAYG Hourly Cost USD"].sum()) if not inv.empty else 0.0

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
        with st.container(key="fl_home_kpis"):
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Tenants Connected", kpis["tenants"])
            c2.metric("Resources Tracked", kpis["resources"])
            c3.metric("Total PAYG Rate", fmt(kpis["payg_hr"], 2) + "/hr")
            c4.metric("Total Committed", fmt(kpis["committed_hr"], 2) + "/hr")
            # "Critical Recommendations" (2026-08-29, real feedback) -
            # matches this app's own established label for this exact
            # metric (the count of HIGH-severity
            # generate_recommendations() items), used before it was
            # dropped from the Analyze top header's own 6-metric grid
            # (see _render_top_header()'s docstring above) - this Home
            # page reintroduced the same count under a different,
            # inconsistent name ("Critical Alerts") when it was rebuilt
            # the next day.
            c5.metric(
                "Critical Recommendations",
                f"{kpis['critical']} items" if kpis["critical"] > 0 else "0 items",
                delta="Action Required" if kpis["critical"] > 0 else "Optimal",
                delta_color="inverse" if kpis["critical"] > 0 else "off",
            )

        if kpis["stale"]:
            names = ", ".join(kpis["stale"])
            plural = "s" if len(kpis["stale"]) > 1 else ""
            st.warning(f"⚠️ {len(kpis['stale'])} tenant{plural} hasn't synced in over a week: **{names}**.", icon="⚠️")

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
        if st.button("Add a new tenant", icon=":material/add:", use_container_width=True, type="primary",
                     disabled=add_disabled,
                     help="Only available in Production Mode." if add_disabled else None):
            st.session_state["_show_add_tenant_form"] = not st.session_state.get("_show_add_tenant_form", False)

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
def page_users():
    st.subheader(":material/group: Dashboard User Accounts")
    st.caption("Accounts that can sign in to this dashboard. Shared across everyone - not tied to a cloud tenant.")
    _finops_tag("Manage the FinOps Practice", "FinOps Practice Operations & Automation, Tools & Services")

    from db.users import list_users, create_user as _create_user

    # Demo and live accounts are separate scopes now (db/schema.py) - this
    # page always reflects whichever scope the current session is in, same
    # as every other data read in the app. In Demo Mode this correctly shows
    # only the single fixed demo account with no "add a user" ability to
    # abuse - real account management only makes sense in Production mode.
    users_mode = "live" if is_live_mode else "demo"

    with st.container(border=True):
        for u in list_users(mode=users_mode):
            ucols = st.columns([3, 3, 2])
            ucols[0].markdown(f"**{u.display_name or u.username}** (`{u.username}`)")
            ucols[1].caption(f"Added {u.created_at[:10]} · Last login: {u.last_login_at[:10] if u.last_login_at else 'never'}")
            with ucols[2]:
                if u.is_active:
                    st.badge("Active", icon=":material/check_circle:", color="green")
                else:
                    st.badge("Inactive", icon=":material/radio_button_unchecked:", color="gray")

    if not is_live_mode:
        st.info("Switch to **Production** mode to add or manage real user accounts - the demo account is fixed.", icon=":material/info:")
        return

    with st.expander("Add a new user", icon=":material/person_add:", expanded=False):
        with st.form("add_user_form"):
            nu_username = st.text_input("Username")
            nu_display = st.text_input("Display name (optional)")
            nu_pw1 = st.text_input("Password", type="password")
            nu_pw2 = st.text_input("Confirm password", type="password")
            nu_submit = st.form_submit_button("Add User", icon=":material/person_add:", type="primary")
        if nu_submit:
            if not nu_username or not nu_pw1:
                st.error("Username and password are both required.")
            elif nu_pw1 != nu_pw2:
                st.error("Passwords don't match.")
            elif len(nu_pw1) < 8:
                st.error("Use at least 8 characters for the password.")
            else:
                try:
                    _create_user(nu_username, nu_pw1, nu_display, mode="live")
                    st.success(f"User '{nu_username}' added.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


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


def _payg_blank_reason(resource_type: str, sku: str) -> str:
    """A blank PAYG cell can mean genuinely different things - conflating
    them into one generic 'Not RI/SP-metered' label (the old behavior) reads
    as a bug to anyone who doesn't already know why a specific resource has
    no hourly rate. Distinguishes: (1) genuinely free/ineligible tiers (Free
    App Service, Consumption-plan Functions) via the same real eligibility
    reasons already computed for the RI Coverage/Savings Plan tabs, (2)
    resources this app can't price per-instance at all (e.g. Storage, sold
    in 100TB+ blocks) via sku_mapping.py's own documented reason, (3) a
    genuine, currently-unresolved pricing gap - kept distinct from the first
    two so it doesn't get mistaken for 'this is fine, it's just free'."""
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
    def _payg_cell(r):
        if r["Resource State"] != "Running":
            return fmt(0, 4) + " (stopped)"
        if r["PAYG Hourly Cost USD"]:
            return fmt(r["PAYG Hourly Cost USD"], 4)
        reason = _payg_blank_reason(r["Resource Type"], r["SKU"])
        return (reason[:87] + "...") if len(reason) > 90 else reason

    disp["PAYG Cost/hr"] = disp.apply(_payg_cell, axis=1)
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

    # Live mode: show which tenant a subscription ID belongs to, not just the raw GUID.
    if is_live_mode and is_live_configured and active_tenant is not None:
        disp["Subscription"] = disp["Subscription"].apply(
            lambda sid: f"{active_tenant.tenant_name} ({sid})" if sid else active_tenant.tenant_name
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
                "Est. Monthly PAYG Cost", "PAYG Cost/hr"]
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
            }}
            /* Push "Columns" to the right edge of its row, away from FOCUS
            View (real feedback, 2026-08-28) - CSS-only (margin-left: auto
            on a flex item pushes it to the far edge regardless of the
            flex:0 0 auto rule above), deliberately NOT reordering the
            underlying st.toggle()/popover() calls: FOCUS View must stay
            the first widget instantiated in this function or its state
            gets silently reset by any filter button's st.rerun() (real bug,
            documented below at settings_row). This only targets the FIRST
            stHorizontalBlock in this container (settings_row) so it can't
            affect the Add filter/pills row underneath. */
            div.st-key-{key_prefix}_filter_bar div[data-testid="stHorizontalBlock"]:nth-of-type(1) > div[data-testid="stColumn"]:nth-of-type(2) {{
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
        settings_row = st.columns(2)
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
        filter_row = st.columns(n_pills + 1)
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
            st.dataframe(
                pd.DataFrame(FOCUS_COLUMN_DEFINITIONS, columns=["FOCUS Column", "Spec Definition", "How it's populated here"]),
                hide_index=True, width="stretch", key=f"{key_prefix}_focus_reference_table",
            )
            st.caption(
                "**Note on BilledCost/EffectiveCost:** BilledCost is shown equal to ListCost at this per-resource "
                "snapshot. This app *does* compute real discount economics from active RI/Savings-Plan commitments, "
                "but that math applies against pooled commitments across many resources - see the **RI Coverage** "
                "and **Savings Plan Analysis** tabs for the actual $ savings, rather than a guessed per-resource split."
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
    # real feedback, 2026-08-28: a numeric column right-aligns by default
    # in this Streamlit build's grid (glide-data-grid), while PAYG Cost/hr
    # next to it stays a TextColumn and left-aligns, since that one still
    # needs to show a blank-pricing REASON string sometimes, not just a
    # number - column_config has no per-column alignment override to force
    # them to match the other way, so matching the two cost columns means
    # formatting this one back to text. Values are already currency-
    # converted (disp["Est. Monthly PAYG Cost"] above), so this must NOT
    # call fmt() again - that would double-convert for INR.
    _currency_symbol = "₹" if selected_currency == "INR" else "$"
    if "Est. Monthly PAYG Cost" in show_df.columns:
        show_df["Est. Monthly PAYG Cost"] = show_df["Est. Monthly PAYG Cost"].apply(
            lambda v: f"{_currency_symbol}{v:,.2f}" if pd.notna(v) else "—"
        )

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
            "Subscription":           st.column_config.TextColumn(width=140),
            "Resource Type":          st.column_config.TextColumn("Service", width=220),
            "Status":                 st.column_config.TextColumn("Power State", width=170),
            "Region":                 st.column_config.TextColumn(width=110),
            "Resource Group":         st.column_config.TextColumn(width=120),
            "Availability Zone":      st.column_config.TextColumn(width=120),
            "OS":                     st.column_config.TextColumn(width=90),
            "SKU":                    st.column_config.TextColumn(width=140),
            "Est. Monthly PAYG Cost": st.column_config.TextColumn("Est. Monthly Cost", width=140),
            "PAYG Cost/hr":           st.column_config.TextColumn("PAYG Cost/hr", width=140),
        },
    )


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
    # fmt() prefixes USD values with a literal "$" - st.markdown treats a
    # PAIRED "$...$" as LaTeX math (confirmed real, 2026-08-28: two fmt()
    # values in the same headline rendered as italic serif text with
    # spaces stripped and the bold ** markers swallowed, exactly KaTeX's
    # math-mode behavior). _md() escapes "$" to "\$" so it renders as a
    # literal currency symbol instead - safe for INR values too, which
    # never contain "$" and pass through unchanged.
    def _md(x: str) -> str:
        return x.replace("$", "\\$")

    # Semantic color on the key figure, not just bold - real polish pass,
    # 2026-08-28: a warning (over-committed) and good news (savings) read
    # identically at a glance before this, both just bold black text.
    # :color[...] is a real Streamlit markdown directive (confirmed via
    # st.markdown's own docstring), matching the red/green language already
    # used for Power State and Classification elsewhere in this app.
    if leakage_hr > 0:
        headline = f"You've committed :red[**{_md(fmt(leakage_hr))}/hr**] more than is currently eligible — worth reviewing this plan."
    elif recommended_hr <= 0.001:
        headline = ":green[You're already well covered] — no additional commitment recommended right now."
    elif has_real_pricing:
        headline = (
            f"Committing **{_md(fmt(recommended_hr))}/hr** more would save about "
            f":green[**{_md(fmt(est_monthly_savings, 2))}/month**] at the {TERM_LABELS[term_key]} rate."
        )
    else:
        headline = f"We recommend committing **{_md(fmt(recommended_hr))}/hr** more, based on your safety buffer setting."
    st.markdown(f"#### {headline}")

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
            st.dataframe(_with_mapping_caveat(commitment_df, sp_show), hide_index=True, width="stretch", column_config=_SP_COMMITMENT_COLUMN_CONFIG)

    by_type = (
        pool_df.groupby("Resource Type")
        .agg(count=("Resource Name", "size"), rate=("PAYG Hourly Cost USD", "sum"))
        .reset_index()
        .sort_values("rate", ascending=False)
    )
    with st.expander(f"What's eligible — {len(pool_df)} resource{'s' if len(pool_df) != 1 else ''}, {fmt(baseline_hr)}/hr", icon=":material/checklist:", expanded=False):
        type_rows_html = "".join(
            '<div class="spflow-row">'
            f'<span class="spflow-rowname">{html.escape(str(r["Resource Type"]))}</span>'
            f'<span class="spflow-rowcount">{int(r["count"])} resource{"s" if r["count"] != 1 else ""}</span>'
            f'<span class="spflow-rowrate fl-mono">{fmt(r["rate"], 4)}/hr</span>'
            "</div>"
            for _, r in by_type.iterrows()
        )
        st.markdown(f'<div class="spflow-cardhead">By resource type</div>{type_rows_html}', unsafe_allow_html=True)
        st.markdown('<div class="spflow-cardhead" style="margin-top:16px;">Every resource</div>', unsafe_allow_html=True)
        elig_show = pool_df[["Resource Name", "Resource Type", "SKU", "PAYG Hourly Cost USD"]].copy()
        elig_show["PAYG Hourly Cost USD"] = elig_show["PAYG Hourly Cost USD"].apply(lambda x: fmt(x, 4))
        elig_show = elig_show.rename(columns={"PAYG Hourly Cost USD": "PAYG Cost/hr"})
        st.dataframe(elig_show, hide_index=True, width="stretch")


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
        if is_azure:
            sp_coverage_rows = [
                {
                    "Savings Plan Type": "Savings Plan for Compute (1-yr / 3-yr)",
                    "What Is Covered": "Azure Virtual Machines, App Service, Functions Premium, Container Instances (ACI), Dedicated Host, Container Apps, Spring Apps",
                    "What Is NOT Covered": "Software licenses (Windows/SQL), networking bandwidth, OS/Data disks, non-compute services"
                },
                {
                    "Savings Plan Type": "Savings Plan for Databases (1-yr ONLY)",
                    "What Is Covered": "Azure SQL Database, SQL Elastic Pool, SQL Managed Instance, Database for PostgreSQL, Database for MySQL, Cosmos DB provisioned throughput, Database Migration Service, Azure DocumentDB",
                    "What Is NOT Covered": "Software licenses (AHB), database backup storage, networking, 3-year term (1-yr only per Azure policy)"
                }
            ]
        else:
            # "This App Tracks" is deliberately a SEPARATE column from "What
            # Is Covered" (AWS's real product eligibility) - added 2026-08-23
            # after a full service-by-service feasibility pass this session.
            # AWS eligibility and this app's ability to build a per-resource
            # baseline for it are two different questions; conflating them
            # into one cell had made it unclear which gaps were "AWS doesn't
            # offer this" vs "AWS offers it but this app can't observe it."
            sp_coverage_rows = [
                {
                    "Savings Plan Type": "Compute Savings Plans (1-yr / 3-yr)",
                    "What Is Covered": "Amazon EC2, AWS Fargate, and AWS Lambda usage across any region, instance family, OS, or tenancy (up to 66% discount)",
                    "What Is NOT Covered": "EBS storage volumes, data transfer/bandwidth, software licensing surcharges, non-compute services",
                    "This App Tracks": "EC2, Fargate ✅. Lambda ❌ NOT tracked - classic Lambda has no persistent running/stopped resource to enumerate (pure per-invocation billing); Lambda Managed Instances is real and RI/SP-eligible but AWS exposes only pool-level CloudWatch aggregates, never per-instance-type counts.",
                },
                {
                    "Savings Plan Type": "EC2 Instance Savings Plans (1-yr / 3-yr)",
                    "What Is Covered": "EC2 instance usage within a specific family in a designated Region (e.g., m5 in us-east-1, up to 72% discount)",
                    "What Is NOT Covered": "Amazon RDS databases, ElastiCache, Redshift, S3 storage, or instances outside the specified family/region",
                    "This App Tracks": "EC2 ✅ - same inventory as Compute Savings Plans above (no separate resource type needed).",
                },
                {
                    "Savings Plan Type": "Database Savings Plans (1-yr ONLY)",
                    "What Is Covered": "Aurora, RDS, DynamoDB, ElastiCache for Valkey ONLY (not Redis or Memcached - confirmed via the actual Database Savings Plans pricing table), DocumentDB (+ Serverless), Timestream, Neptune (+ Serverless + Analytics), Keyspaces, DMS (+ Serverless), and Amazon OpenSearch Service - up to 35% off",
                    "What Is NOT Covered": "Amazon Redshift, Amazon MemoryDB, ElastiCache for Redis/Memcached (Reserved Instance-eligible only, not Database SP), EC2/Fargate/Lambda compute, 3-year term (1-yr only per AWS policy - same restriction Azure's Savings Plan for Databases has)",
                    "This App Tracks": "Aurora, RDS, DynamoDB (provisioned-capacity tables only), ElastiCache for Valkey, DocumentDB + Serverless, Neptune + Serverless + Analytics, Keyspaces, DMS + Serverless, OpenSearch - all ✅. Timestream ❌ NOT tracked - fully usage-based per byte ingested/stored/scanned, no instance class or capacity-unit concept to represent as an inventory resource at all.",
                },
                {
                    "Savings Plan Type": "SageMaker AI Savings Plans (1-yr / 3-yr)",
                    "What Is Covered": "Amazon SageMaker AI instance usage regardless of instance family, size, Region, or component (Notebook, Training, Inference, etc.) - up to 64% discount",
                    "What Is NOT Covered": "EC2/Fargate/Lambda/database compute. Baseline below (Pool C) only covers Real-Time Inference Endpoints and Notebook Instances - Training/Processing/Data Wrangler/Batch Transform are one-shot ephemeral jobs with no persistent running/stopped identity, so they aren't modeled as inventory at all (same reasoning already applied to Lambda invocations).",
                    "This App Tracks": "Real-Time Inference Endpoints, Notebook Instances ✅. Training/Processing/Data Wrangler/Batch Transform ❌ NOT tracked - ephemeral one-shot jobs, no persistent running/stopped identity to enumerate.",
                },
            ]
        # Cards, not a dataframe - real bug caught live, 2026-08-28: these
        # cells are paragraph-length prose, and st.dataframe cells don't
        # wrap text regardless of column width (confirmed this session on
        # the Inventory/Rightsizing tables' clipping issues) - a table was
        # never going to show this content in full, only trade off which
        # part got cut. Real st.markdown text wraps naturally, so cards
        # fully solve it rather than just widening columns.
        for row in sp_coverage_rows:
            with st.container(border=True):
                st.markdown(f"**{row['Savings Plan Type']}**")
                st.markdown(f":material/check_circle: **Covered:** {row['What Is Covered']}")
                st.markdown(f":material/block: **Not covered:** {row['What Is NOT Covered']}")
                if "This App Tracks" in row:
                    st.markdown(f":material/info: **This app tracks:** {row['This App Tracks']}")

    safety_buffer_pct_local = st.slider(
        "Safety Buffer % — how much of the steady-state footprint to commit",
        min_value=50, max_value=100,
        value=st.session_state.get("sp_safety_buffer_widget", int(DEFAULT_SAFETY_BUFFER * 100)),
        step=5, key="sp_safety_buffer_widget",
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
                    st.dataframe(
                        compute_sp_excluded[["Resource Name", "Resource Type", "SKU", "SP Eligibility Note"]],
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
                    st.dataframe(
                        db_sp_excluded[["Resource Name", "Resource Type", "SKU", "SP Eligibility Note"]],
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
        if row["gap"] > 0:
            base = f"⚠️ Short by {int(row['gap'])}"
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
            return f"ℹ️ {int(row['excess'])} Idle"
        return "✅ Fully Covered"

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

    # ── Headline: one answer, badges for the rest ───────────────────────
    def _md(x: str) -> str:
        return x.replace("$", "\\$")

    plural = "s" if needs_more != 1 else ""
    if needs_more == 0:
        headline = ":green[You're already well covered] — no additional Reserved Instance purchases recommended right now."
    else:
        total_gap_savings = instance_cov.loc[instance_cov["gap"] > 0, savings_col].dropna().sum() if has_pricing_cols else 0.0
        if total_gap_savings > 0:
            headline = (
                f"Purchasing RIs for **{needs_more} resource profile{plural}** would save about "
                f":green[**{_md(fmt(total_gap_savings, 2))}/month**] at the {ri_term_choice} rate."
            )
        else:
            headline = f":orange[**{needs_more} resource profile{plural}**] {'is' if needs_more == 1 else 'are'} running without a matching Reservation."
    st.markdown(f"#### {headline}")

    badge_cols = st.columns(4)
    badge_cols[0].badge(f"{fully_covered} Fully Covered", icon=":material/check_circle:", color="green")
    badge_cols[1].badge(f"{needs_more} Need More RI", icon=":material/trending_up:", color="orange")
    badge_cols[2].badge(f"{idle} Idle / Unused", icon=":material/pause_circle:", color="blue")
    badge_cols[3].badge(f"{not_eligible} Not RI-Eligible", icon=":material/block:", color="gray")

    # ── Orphaned-RI drain alert - moved here, right after the headline/
    # badges (2026-08-29, real feedback: "too cluttered, too much to
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

    st.segmented_control(
        "Model new-purchase pricing at term",
        options=["1-Year", "3-Year"],
        default=ri_term_choice,
        key="ri_term_display",
        help="Drives the purchase-cost columns below and the Recommendations tab's combined savings projection.",
    )
    # ri_term_choice/ri_term_key/rate_col/savings_col were already read
    # live above (before this widget call) and are guaranteed identical to
    # this widget's value - no need to re-derive them from its return.
    # Still mirrored into "ri_term_widget" for _real_projected_savings()
    # (Recommendations tab), which reads it on a later, separate render.
    st.session_state["ri_term_widget"] = ri_term_key

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
        "automatically across your subscription, so treat their Status as a rough signal only (see Note)."
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
        # Short phrases, not sentences (2026-08-29, real feedback + a
        # screenshot showing a different long-text column on this same
        # table clipped with no closing border when given width="large" -
        # this table already carries 10 other explicit-width columns, and
        # a "large" 11th column competing for the remainder isn't reliable
        # here, same lesson already applied to that other column below.
        # Sized to the real longest value below (~40 chars) rather than a
        # relative keyword; the FULL explanation still lives in the
        # Reservation Coverage Rules expander's per-service cards - this
        # is a pointer, not a restatement.
        _COVERAGE_TYPE_NOTE = {
            "instance": "",
            "capacity": "Rough signal only, not a purchase instruction",
            "unmeasurable": "No per-resource gap possible - see Coverage Rules",
        }
        show["Coverage Type"] = show["coverage_model"].map(_COVERAGE_TYPE_LABEL)
        show["Note"] = show["coverage_model"].map(_COVERAGE_TYPE_NOTE)
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
        _is_volume_based = show["coverage_model"] == "unmeasurable"
        for _col in ("Running", "Reserved", "Status"):
            show[_col] = show[_col].astype(object)
            show.loc[_is_volume_based, _col] = "—"
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
            # the actual computation changes. Pooled/Volume-Based rows
            # already come through as NaN here (ri_gap_pricing() only ever
            # prices coverage_model == "instance" rows) - "—" for both,
            # same as a genuinely un-priced Per-Instance row.
            show[rate_col] = show[rate_col].apply(lambda x: fmt(x * 730, 2) if pd.notna(x) else "—")
            show[savings_col] = show[savings_col].apply(lambda x: fmt(x, 2) if pd.notna(x) else "—")
        cols = ["Service", "SKU / Tier", "Region", "OS", "Coverage Type", "Running", "Reserved", "Status"]
        if has_pricing_cols:
            cols += [rate_col, savings_col]
        cols += ["Note"]
        show = show[[c for c in cols if c in show.columns]]

        # Same real pandas.Styler technique already used for the Inventory
        # tab's Power State and the Rightsizing tab's Classification columns
        # - color Status instead of adding a badge column, since a
        # canvas-rendered st.dataframe can't render real badge widgets.
        def _status_color(val):
            if val == "—":
                return "color: #64748B;"
            if val.startswith("⚠️"):
                return "color: #FBBF24;"
            if val.startswith("ℹ️"):
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
                "Coverage Type": st.column_config.TextColumn(width=110),
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
                "Note":          st.column_config.TextColumn(width=280),
            },
        )
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
            show_i = ineligible.rename(columns={"Resource Type": "Service", "SKU": "SKU / Tier", "eligibility_reason": "Why not eligible"})
            st.dataframe(
                show_i[["Service", "SKU / Tier", "Region", "Why not eligible"]], hide_index=True, width="stretch",
                column_config={
                    "Service":    st.column_config.TextColumn(width=230),
                    "SKU / Tier": st.column_config.TextColumn(width=140),
                    "Region":     st.column_config.TextColumn(width=110),
                    # "large" keyword width (not a pixel value) - same fix
                    # already used for the Savings Plan tab's own "why
                    # excluded" long-prose column.
                    "Why not eligible": st.column_config.TextColumn(width="large"),
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

        # Cards, not a dataframe - same real bug already fixed on the
        # Savings Plan Coverage Policy card (2026-08-28): these Covers/
        # Excludes cells are paragraph-length prose, and st.dataframe cells
        # don't wrap text regardless of column width. 2-column grid - each
        # card is only 2-3 short lines, one-per-row wasted half the width
        # for no reason once the list was trimmed to eligible services only.
        grid_cols = st.columns(2)
        for i, (svc, (covers, excludes)) in enumerate(eligible_notes.items()):
            with grid_cols[i % 2].container(border=True):
                st.markdown(f"**{svc}**")
                st.markdown(f":material/check_circle: **Covers:** {covers}")
                st.markdown(f":material/block: **Excludes:** {excludes}")

        if _LAMBDA_NOTE_KEY in RI_COVERAGE_NOTES:
            # Rendered as its own card IN the grid (2026-08-29, real
            # feedback: as a plain st.caption() sitting below the grid,
            # this real, useful disclosure - Lambda genuinely IS RI-
            # eligible, this app just can't compute a gap for it - read as
            # an afterthought footnote easy to miss entirely). Same card
            # shell as its peers so it's discoverable in the same reading
            # flow, not promoted above the fold (still inside this
            # collapsed expander - it's a minor caveat about one service,
            # not urgent like the drain alert at the top of the page) and
            # not demoted to a floating caption either. Distinguished from
            # a real Covers/Excludes card by using one "eye-off" line
            # instead of two, so it doesn't imply this app tracks it.
            with grid_cols[len(eligible_notes) % 2].container(border=True):
                st.markdown(f"**{_LAMBDA_NOTE_KEY}**")
                st.markdown(
                    ":material/visibility_off: Genuinely EC2 RI-eligible, but not shown as a gap/coverage row - "
                    "AWS exposes only pool-level configuration, never a per-instance count to compare against a reservation."
                )
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
            st.dataframe(
                _with_mapping_caveat(ri_df, ri_disp), hide_index=True, width="stretch",
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
_STAGE_STYLE = {
    "Run":               ("🟢", "Run"),
    "Walk":               ("🔵", "Walk"),
    "Crawl":              ("🟡", "Crawl"),
    "Below Crawl":        ("🔴", "Below Crawl"),
    "Not Yet Measurable": ("⚪", "Not Yet Measurable"),
}

_CAPABILITY_LINKS = {
    "Rate Optimization": "the Savings Plan Analysis and RI Coverage tabs",
    "Usage Optimization": "the Inventory tab's orphaned-resource flags",
    "Anomaly Management": "the Recommendations tab",
    "Cost Allocation": "a roadmap item - needs resource tagging/ownership data not yet ingested",
    "Forecasting": "a roadmap item - needs a forecasting model not yet built",
}


def _render_maturity_tab():
    st.subheader(f"FinOps Maturity Assessment ({selected_provider})")
    st.caption(
        "Self-assessment against the FinOps Foundation's Crawl/Walk/Run Maturity Model "
        "(finops.org/framework/maturity-model), scored from this session's actual computed data."
    )
    _finops_tag("Manage the FinOps Practice", "FinOps Assessment")

    with st.expander("ℹ️ What is this, and why is it separate from Recommendations?", expanded=True):
        st.markdown(
            "The **FinOps Foundation** - the industry body behind FinOps, comparable to PMI for project "
            "management - publishes a **Maturity Model**: a standard rubric with three stages, "
            "**Crawl → Walk → Run**, and published numeric thresholds for some capabilities "
            "(e.g. *Commitment Discounts*: Crawl ≥60%, Walk ≥75%, Run ≥80% of eligible spend covered).\n\n"
            "This is a **different question** than the Recommendations tab. Recommendations answers "
            "*\"what should I do right now\"*. This tab answers *\"how mature is my cost-optimization "
            "practice overall, and what capability is worth investing in next.\"* Every score below comes "
            "from this session's real computed numbers - actual RI/SP coverage %, actual orphaned-resource "
            "detection - checked against the official published thresholds, never an invented number. "
            "Where the app doesn't have the underlying data yet (tagging, forecasting), that's shown "
            "honestly as **Not Yet Measurable** instead of a fake score."
        )

    assessments = run_maturity_assessment(sp_result, ri_result, inv_raw, recs)

    stage_counts = {}
    for a in assessments:
        stage_counts[a.stage] = stage_counts.get(a.stage, 0) + 1
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("🟢 Run", stage_counts.get("Run", 0))
    k2.metric("🔵 Walk", stage_counts.get("Walk", 0))
    k3.metric("🟡 Crawl", stage_counts.get("Crawl", 0))
    k4.metric("🔴 Below Crawl", stage_counts.get("Below Crawl", 0))
    k5.metric("⚪ Not Measurable", stage_counts.get("Not Yet Measurable", 0))

    st.info(
        "The Maturity Model's own guidance: *\"focus less on maturing each Capability to 'Run' "
        "for everything\"* - prioritize whichever capabilities deliver the highest business value "
        "for your organization next, rather than treating this as a checklist to max out."
    )

    st.divider()

    for a in assessments:
        icon, label = _STAGE_STYLE.get(a.stage, ("⚪", a.stage))
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            with c1:
                st.markdown(f"**{a.capability}** · _{a.domain}_")
                st.caption(a.headline)
            with c2:
                st.markdown(f"### {icon} {label}")
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
    with st.popover("Rightsizing Settings", icon=":material/settings:"):
        st.selectbox("Preset", list(PRESETS.keys()) + ["Custom"], key=preset_key,
                     on_change=_apply_preset, help=rightsizing_help)

        r1c1, r1c2 = st.columns(2)
        with r1c1:
            st.selectbox("Percentile", [90, 95, 99], key=f"{key_prefix}_percentile")
        with r1c2:
            st.number_input("Lookback window (days)", min_value=1, max_value=93,
                             key=f"{key_prefix}_lookback_days")

        r2c1, r2c2 = st.columns(2)
        with r2c1:
            st.number_input("CPU Underutilized threshold (%)", min_value=0.0, max_value=100.0,
                             step=5.0, key=f"{key_prefix}_cpu_under_pct",
                             help="Underutilized (CPU side) triggers when CPU usage drops below this.")
        with r2c2:
            st.number_input("CPU Overutilized threshold (%)", min_value=0.0, max_value=100.0,
                             step=5.0, key=f"{key_prefix}_cpu_over_pct",
                             help="Overutilized triggers when CPU usage rises above this.")

        r3c1, r3c2 = st.columns(2)
        with r3c1:
            st.number_input("Memory Underutilized threshold (% used)", min_value=0.0, max_value=100.0,
                             step=5.0, key=f"{key_prefix}_mem_under_pct",
                             help="Underutilized (memory side) triggers when memory usage drops below this.")
        with r3c2:
            st.number_input("Memory Overutilized threshold (% available)", min_value=0.0, max_value=100.0,
                             step=5.0, key=f"{key_prefix}_mem_available_pct",
                             help="Overutilized triggers when *available* (free) memory drops below this.")

        r4c1, r4c2 = st.columns(2)
        with r4c1:
            st.number_input("Headroom / safety margin (%)", min_value=0.0, max_value=50.0, step=5.0,
                             key=f"{key_prefix}_headroom_pct",
                             help="A suggested resize must leave at least this much headroom.")
        with r4c2:
            st.number_input("Minimum days of data", min_value=1, max_value=93,
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
    # service would query in production (Azure Monitor Metrics / Amazon
    # CloudWatch); today's demo/synced data only ever captures Avg and P95
    # (see data/inventory_loader.py), so classification reads the stored P95
    # columns regardless of the configured percentile - disclosed
    # simplification, not silently pretended to be arbitrary-percentile-
    # accurate. Threshold changes alone already drive real classification
    # differences without this, so nothing downstream is faked.
    live_metrics_service = "Azure Monitor Metrics" if is_azure else "Amazon CloudWatch"
    st.caption(
        ":material/info: Classification currently reads the stored P95 CPU/Memory values (this app's synced "
        f"summary stats). Full arbitrary-percentile aggregation requires a live production-phase "
        f"{live_metrics_service} query — not built yet."
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

    k1, k2, k3 = st.columns(3)
    k1.metric(":material/trending_down: Underutilized", under_count)
    k2.metric(":material/trending_up: Overutilized", over_count)
    k3.metric(":material/payments: Est. Monthly Savings (downsizes)", fmt(total_savings, 2))

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
# NAVIGATION — flat 3-item sidebar (Home / Tenant Management / User
# Management), per approved sketch 2026-08 - no section headers, no separate
# "Workspace" entry. Analyze is still a real registered page (needed for
# st.switch_page to work at all) but visibility="hidden" keeps it out of the
# sidebar - it's reached only via a tenant's own "Dashboard" button in
# Tenant Management, never browsed to directly, since analyzing data only
# makes sense once a specific tenant is active.
# ─────────────────────────────────────────────────────────────────────────────
home_page        = st.Page(page_home,              title="Home",              icon=":material/home:", default=True)
tenant_mgmt_page = st.Page(page_tenant_management,  title="Tenant Management", icon=":material/domain:")
users_page       = st.Page(page_users,              title="User Management",   icon=":material/group:")
analyze_page     = st.Page(page_analyze,            title="Analyze",           icon=":material/insights:", visibility="hidden")

pg = st.navigation([home_page, tenant_mgmt_page, users_page, analyze_page], position="sidebar")

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

    # ── ☁️ Cloud & Data Source ──────────────────────────────────────────────
    st.markdown("#### ☁️ Cloud & Data Source")

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

    # Re-asserted on every rerun (not just written once) - same reasoning as
    # currency/the login token just below: Streamlit's sidebar nav links
    # don't reliably carry a query param forward unless it's freshly
    # rewritten on every single script run, not just at the moment it was
    # first set.
    st.query_params["provider"] = selected_provider

    # Deliberately NOT a free-switching widget - see
    # ui.auth_page.render_switch_mode_control's docstring for why: demo and
    # live are separate login accounts and separate data now, so switching
    # means signing out and back in, not flipping a toggle mid-session.
    env_mode = render_switch_mode_control()

    st.divider()

    # ── 💱 Display ───────────────────────────────────────────────────────────
    st.markdown("#### 💱 Display")

    selected_currency = st.segmented_control(
        "Display Currency",
        options=["USD", "INR"],
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

    st.caption(f"Platform `{selected_provider}` · Mode `{env_mode}` · Currency `{selected_currency}`")

    st.divider()

    # ── 🔒 Account ───────────────────────────────────────────────────────────
    st.markdown("#### 🔒 Account")
    render_logout_control()
    st.caption("Manage user accounts under 👥 User Management.")

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

@st.cache_data(show_spinner=False)
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
    ri_res        = reservation_analysis(inv_raw, ri_df, flex_groups_df)
    # currency/inr_rate (2026-08-29, real feedback) - explicit params on
    # THIS cached function too, not just generate_recommendations() - a
    # @st.cache_data function's return value is cached by its OWN
    # argument set, so if currency weren't part of THIS signature,
    # switching Display Currency wouldn't invalidate this cache and would
    # keep returning recs with stale-currency text baked in.
    recs          = generate_recommendations(sp_res, ri_res, wf, safety_buffer=buffer, currency=currency, inr_rate=inr_rate)
    return inv_raw, sp_df, compute_sp_df, db_sp_df, sagemaker_sp_df, ri_df, sp_res, ri_res, recs

@st.cache_data(show_spinner=False)
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
    ri_res        = reservation_analysis(inv_raw, ri_df, flex_groups_df)
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

st.markdown("""
<div style="background-color: transparent; border-top: 1px solid rgba(255,255,255,0.08); padding: 48px 0px 40px 0px; margin-top: 100px;">
    <div style="max-width: 1200px; margin: 0 auto; text-align: center;">
        <p style="font-size: 0.8rem; color: #94a3b8; line-height: 1.6; margin-bottom: 16px;">
            <strong style="color: #cbd5e1;">Legal & Financial Notice:</strong> Multi-Cloud FinOps Optimization System is an independent cloud financial decision support platform. Cost calculations, projected savings, and commitment recommendations are provided for analytical modeling purposes. Official billing figures must be verified in Microsoft Azure Cost Management or AWS Cost Explorer prior to purchasing commitment contracts. Microsoft Azure and Amazon Web Services (AWS) are registered trademarks of their respective owners.
        </p>
        <div style="font-size: 0.8rem; color: #64748b; padding-top: 8px;">
            © 2026 Multi-Cloud FinOps Optimization System. All rights reserved.
        </div>
    </div>
</div>
""", unsafe_allow_html=True)
