"""
app.py — Multi-Cloud FinOps Optimization System (Azure & AWS)
Enterprise Cloud Cost & Commitment Engine

Left sidebar (Manage): Tenants, User Management.
Left sidebar (Workspace): Analyze — a single page whose top tabs are
  Inventory | Savings Plan Analysis | RI Coverage | Cost Analysis |
  Recommendations | Maturity Assessment.
"""

import sys, os, glob
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

import streamlit as st
import pandas as pd

# Detect initial currency from query parameters to persist across browser refresh
if "currency" in st.query_params:
    initial_currency = st.query_params["currency"]
    if initial_currency not in ["USD", "INR"]:
        initial_currency = "USD"
else:
    initial_currency = "USD"

st.set_page_config(
    page_title="FinOps Optimization System",
    page_icon=":material/bolt:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Detect active theme for chart adaptation ────────────────────────────────
_theme_type = st.context.theme.type  # "dark" or "light"
is_dark_theme = (_theme_type == "dark")

# Initialize Databases (Cached for instant reloads)
from db.schema import init_db, get_engine
from db.seed import seed_if_empty, COMPUTE_SP_ELIGIBLE_TYPES, DATABASE_SP_ELIGIBLE_TYPES
from db.aws_seed import seed_aws_if_empty, AWS_COMPUTE_SP_TYPES, AWS_DATABASE_SP_TYPES

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
from data.sync_pipeline import run_ingestion_pipeline, get_latest_sync_log
from pricing.retail_pricing import price_inventory, usd, fmt_currency, get_inr_rate
from pricing.commitment_pricing import get_commitment_prices
from ui.charts import get_cost_distribution_chart, get_waterfall_savings_chart, get_recommendation_opportunity_chart
from commitments.existing_commitments import (
    get_existing_savings_plans,
    get_existing_reservations,
    get_compute_savings_plans,
    get_database_savings_plans,
)
from analysis.engine import (
    run_waterfall,
    savings_plan_analysis,
    reservation_analysis,
    generate_recommendations,
    DEFAULT_SAFETY_BUFFER,
)
from analysis.sp_eligibility import check_sp_eligibility
from analysis.ri_eligibility import check_eligibility
from pricing.sku_mapping import resolve_sku_query
from analysis.focus_mapping import to_focus_view, FOCUS_COLUMN_DEFINITIONS, FOCUS_SPEC_VERSION, FOCUS_SPEC_URL
from analysis.maturity import run_maturity_assessment
from analysis.commitment_economics import (
    savings_plan_term_comparison, ri_gap_pricing, combined_monthly_savings, TERM_LABELS,
)
from db.tenants import (
    list_tenants, get_active_tenant, upsert_tenant, set_active_tenant,
    delete_tenant, resource_count,
)
from azure_conn.connector import (
    AzureCredentials, load_credentials_from_env, save_credentials_to_env_file,
    test_connection, REQUIRED_ROLES, HAS_AZURE_IDENTITY,
)
from aws.connector import (
    AWSCredentials, load_aws_credentials_from_env, save_aws_credentials_to_env_file,
    test_aws_connection, REQUIRED_AWS_POLICIES, HAS_BOTO3,
)

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"), override=False)
except ImportError:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# SHARED AZURE TENANT CONNECT/LIST WIDGETS
# Used by both the post-login Setup gate and the Tenants page - extracted once
# here instead of duplicated, since both need the exact same "enter Service
# Principal creds -> test/connect -> ingest" flow.
# ─────────────────────────────────────────────────────────────────────────────
def _render_azure_connect_form(key_prefix: str):
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
                    provider="Azure",
                    tenant_name=az_name or "Azure Tenant",
                    tenant_id=az_tenant,
                    subscription_id=az_sub,
                    client_id=az_client,
                    client_secret=az_sec,
                )
                res = run_ingestion_pipeline("Azure", creds=new_az, tenant_db_id=tenant_db_id)
                if res["status"] == "SUCCESS":
                    st.success(f"🎉 **Live Tenant Ingestion Complete!** {res['message']}")
                    return True
                else:
                    st.error(f"❌ Connection or Ingestion Error: {res['message']}")
        else:
            st.error("Please fill in all Azure credential fields.")
    return False


def _render_azure_tenant_list(key_prefix: str):
    """Renders the connected-tenants list with Activate/Delete buttons.
    Returns True if the active tenant just changed (Activate clicked)."""
    azure_tenants = list_tenants("Azure")
    if not azure_tenants:
        st.caption("No tenants connected yet. Fill in the form above and click **Connect, Save & Ingest**.")
        return False

    changed = False
    for t in azure_tenants:
        t_count = resource_count("Azure", t.id)
        cols = st.columns([3, 3, 2, 2, 2])
        cols[0].markdown(f"{'🟢' if t.is_active else '⚪'} **{t.tenant_name}**")
        cols[1].caption(f"Tenant: `{t.tenant_id[:8]}…` · Sub: `{t.subscription_id[:8]}…`")
        cols[2].caption(f"{t_count} resource{'s' if t_count != 1 else ''}")
        cols[3].caption(f"Added {t.created_at[:10]}")
        with cols[4]:
            b1, b2 = st.columns(2)
            if not t.is_active:
                if b1.button("Activate", key=f"{key_prefix}_activate_az_{t.id}", use_container_width=True):
                    set_active_tenant("Azure", t.id)
                    changed = True
            if b2.button("🗑️", key=f"{key_prefix}_delete_az_{t.id}", use_container_width=True, help="Remove this tenant and its synced data"):
                delete_tenant("Azure", t.id)
                changed = True
    st.caption("🟢 = active tenant shown in Live Cloud API mode. Only one tenant is active at a time.")
    return changed


# ─────────────────────────────────────────────────────────────────────────────
# POST-LOGIN SETUP GATE — tenant selection for Production Mode sessions
# ─────────────────────────────────────────────────────────────────────────────
def _render_setup_gate():
    """Tenant-connection gate for Production Mode sessions - the Demo-vs-
    Production choice itself now happens on the login screen (ui/auth_page.py)
    before this ever runs; a Demo Mode login sets setup_complete=True at
    login time and never reaches this gate at all."""
    st.markdown(
        '<div class="finops-hero-badge">☁️</div>'
        f"<h2 style=\"margin-bottom:0\">Connect a Cloud Tenant</h2>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"Welcome, {current_user['display_name'] or current_user['username']}. "
        "Connect an Azure tenant to analyze real cloud spend, or skip for now to explore with Demo data."
    )

    active = get_active_tenant("Azure")
    if list_tenants("Azure"):
        st.caption("Select which connected tenant to use for this session, or add another below.")
        if _render_azure_tenant_list("setup"):
            st.rerun()
        st.divider()

    with st.expander("➕ Add a new tenant", expanded=(active is None)):
        if _render_azure_connect_form("setup"):
            st.rerun()

    b1, b2 = st.columns([1, 3])
    if b1.button("Skip for now", key="setup_skip"):
        st.session_state["env_mode_widget"] = "Demo / Benchmark Mode"
        st.session_state["setup_complete"] = True
        st.rerun()
    active_now = get_active_tenant("Azure")
    if b2.button(
        "Continue to Dashboard →", key="setup_go_live", type="primary", use_container_width=True,
        disabled=active_now is None,
        help=None if active_now else "Connect or activate a tenant above first.",
    ):
        st.session_state["env_mode_widget"] = "Live Cloud API"
        st.session_state["setup_complete"] = True
        st.rerun()
    st.stop()


if not st.session_state.get("setup_complete"):
    _render_setup_gate()


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
    st.caption(f"🧭 **FinOps Framework:** {domain} → *{capability}*")


def _render_top_header():
    """Persistent KPI/status header shown once above the Analyze tabs."""
    st.markdown("## ☁️ Multi-Cloud FinOps Optimization System")
    st.caption(f"Enterprise Cost & Commitment Optimization Platform  ·  Provider: **{selected_provider}**  ·  Mode: **{env_mode}**")

    if is_live_mode and not is_live_configured:
        st.warning(
            f"⚠️ **Live Mode Active — No Connection Configured for {selected_provider}:** "
            f"Please go to the **🏢 Tenants** page to configure your {selected_provider} credentials, "
            "or switch to **Demo / Benchmark Mode** in the sidebar to view sample data.",
            icon="⚠️"
        )
    elif is_live_mode and is_live_configured:
        st.success(
            f"✅ **Connected to Live {selected_provider} API** — Tenant: **{active_tenant.tenant_name}** "
            f"({len(inv_raw)} resource{'s' if len(inv_raw) != 1 else ''} synced)",
            icon="✅",
        )

    with st.container(border=False):
        r1_col1, r1_col2, r1_col3 = st.columns(3)
        r1_col1.metric(f"Running {compute_label}", len(running_vms))
        r1_col2.metric(f"Running {db_label}",      len(running_dbs))
        r1_col3.metric("Compute PAYG Rate",        fmt(total_vm_payg_hr, 4) + "/hr")

        st.write("") # small spacing
        r2_col1, r2_col2, r2_col3 = st.columns(3)
        r2_col1.metric("Database PAYG Rate",       fmt(total_db_payg_hr, 4) + "/hr")
        r2_col2.metric("Total SP Committed",       fmt(total_sp_commit, 4) + "/hr")
        r2_col3.metric("Critical Alerts",
                      f"{high_recs} items" if high_recs > 0 else "0 items",
                      delta="Action Required" if high_recs > 0 else "Optimal",
                      delta_color="inverse" if high_recs > 0 else "off")

    st.divider()


# ═══════════════════════════════════════════════════════════════════════════════
# MANAGE — TENANTS
# ═══════════════════════════════════════════════════════════════════════════════
def page_tenants():
    st.subheader(f"🏢 {selected_provider} Tenant Management")
    st.caption(f"Connect, switch, or remove {selected_provider} accounts feeding this dashboard's live data.")
    _finops_tag("Manage the FinOps Practice", "FinOps Practice Operations & Automation, Tools & Services")

    if is_azure:
        # ── AZURE CONNECTION SETTINGS ──────────────────────────────────────────
        hdr_l, hdr_r = st.columns([4, 1])
        with hdr_l:
            st.markdown("#### 🗂️ Connected Tenants")
        with hdr_r:
            if st.button("➕ Add New Tenant", use_container_width=True, type="primary"):
                st.session_state["_show_add_tenant_form"] = not st.session_state.get("_show_add_tenant_form", False)

        if st.session_state.get("_show_add_tenant_form"):
            with st.container(border=True):
                st.markdown("### ☁️ Azure Service Principal Integration")
                with st.expander("📖 Instructions: How to obtain & assign required Azure RBAC roles", expanded=False):
                    st.markdown("""
1. Open **Azure Cloud Shell** or your local Azure CLI.
2. Create the Service Principal with **Reader** role on your primary subscription:
   ```bash
   az ad sp create-for-rbac --name "finops-optimizer-sp" --role "Reader" --scopes /subscriptions/<YOUR_SUBSCRIPTION_ID> --output json
   ```
3. Assign **Cost Management Reader** role to allow reading cost, reservation utilization, and savings plan data:
   ```bash
   az role assignment create --assignee <CLIENT_ID> --role "Cost Management Reader" --scope /subscriptions/<YOUR_SUBSCRIPTION_ID>
   ```
4. Copy the credentials into the form below:
   - `appId` $\\rightarrow$ **Client ID**
   - `password` $\\rightarrow$ **Client Secret**
   - `tenant` $\\rightarrow$ **Tenant ID**
   - `<YOUR_SUBSCRIPTION_ID>` $\\rightarrow$ **Subscription ID**
""")
                if _render_azure_connect_form("tenants_page"):
                    st.session_state["_show_add_tenant_form"] = False
                    st.rerun()

        azure_tenants = list_tenants("Azure")
        if not azure_tenants:
            st.info("No tenants connected yet. Click **➕ Add New Tenant** above to connect your first Azure tenant.")
        else:
            with st.container(border=True):
                if _render_azure_tenant_list("tenants_page"):
                    st.rerun()

            active_now = get_active_tenant("Azure")
            if active_now is not None:
                st.write("")
                if st.button(f"→ View Dashboard for '{active_now.tenant_name}'", type="primary"):
                    st.switch_page(analyze_page)

        st.markdown("#### Required Azure RBAC Roles")
        st.dataframe(pd.DataFrame(REQUIRED_ROLES)[["Role Name", "Scope", "Purpose"]], hide_index=True, width="stretch")

    else:
        # ── AWS CONNECTION SETTINGS ───────────────────────────────────────────
        hdr_l, hdr_r = st.columns([4, 1])
        with hdr_l:
            st.markdown("#### 🗂️ Connected AWS Accounts")
        with hdr_r:
            if st.button("➕ Add New Account", use_container_width=True, type="primary"):
                st.session_state["_show_add_tenant_form"] = not st.session_state.get("_show_add_tenant_form", False)

        if st.session_state.get("_show_add_tenant_form"):
            with st.container(border=True):
                st.markdown("### 🟧 AWS IAM Credentials Integration")
                with st.expander("📖 Instructions: How to obtain AWS IAM Access Keys", expanded=False):
                    st.markdown("""
1. Sign in to the **AWS Management Console** and open the **IAM Console**.
2. In the navigation pane, choose **Users**, then select your user or create a dedicated `FinOpsOptimizerUser`.
3. Choose the **Security credentials** tab.
4. Under **Access keys**, click **Create access key**.
5. Select **Command Line Interface (CLI)** and copy the generated credentials:
   - **Access Key ID**
   - **Secret Access Key**
6. Ensure the IAM user has `ec2:DescribeInstances`, `rds:DescribeDBInstances`, and `ce:GetCostAndUsage` policies attached.
""")
                env_aws = load_aws_credentials_from_env()

                with st.form("aws_form"):
                    col1, col2 = st.columns(2)
                    with col1:
                        aws_key = st.text_input("AWS Access Key ID", value=env_aws.access_key_id if env_aws else "", placeholder="AKIAXXXXXXXXXXXXXXXX")
                        aws_reg = st.text_input("Default AWS Region", value=env_aws.region if env_aws else "us-east-1", placeholder="us-east-1")
                    with col2:
                        aws_sec = st.text_input("AWS Secret Access Key", value=env_aws.secret_access_key if env_aws else "", type="password")

                    aws_sub_btn = st.form_submit_button("💾 Connect & Save AWS Credentials", type="primary")

                if aws_sub_btn:
                    new_aws = AWSCredentials(aws_key, aws_sec, aws_reg)
                    if new_aws.is_complete:
                        save_aws_credentials_to_env_file(new_aws)
                        upsert_tenant(
                            provider="AWS",
                            tenant_name=f"AWS ({aws_reg})",
                            tenant_id=aws_reg,
                            subscription_id=aws_reg,
                            client_id=aws_key,
                            client_secret=aws_sec,
                        )
                        st.success("✅ AWS credentials saved to the tenant registry.")
                        st.info("ℹ️ Live AWS inventory fetch (EC2/RDS via boto3) isn't implemented yet — this account is registered, but Live mode will show no resources until that's built.")
                        st.session_state["_show_add_tenant_form"] = False
                        st.rerun()
                    else:
                        st.error("Please fill in Access Key ID and Secret Access Key.")

        aws_tenants = list_tenants("AWS")
        if not aws_tenants:
            st.info("No AWS accounts connected yet. Click **➕ Add New Account** above to connect one.")
        else:
            with st.container(border=True):
                for t in aws_tenants:
                    cols = st.columns([4, 3, 2])
                    cols[0].markdown(f"{'🟢' if t.is_active else '⚪'} **{t.tenant_name}**")
                    cols[1].caption(f"Key: `{t.client_id[:6]}…` · Added {t.created_at[:10]}")
                    with cols[2]:
                        if not t.is_active and st.button("Activate", key=f"activate_aws_{t.id}", use_container_width=True):
                            set_active_tenant("AWS", t.id)
                            st.rerun()

            active_now = get_active_tenant("AWS")
            if active_now is not None:
                st.write("")
                if st.button(f"→ View Dashboard for '{active_now.tenant_name}'", type="primary"):
                    st.switch_page(analyze_page)

        st.markdown("#### Required AWS IAM Permissions")
        st.dataframe(pd.DataFrame(REQUIRED_AWS_POLICIES), hide_index=True, width="stretch")

    # ── 24-HOUR EXTRACTION & INGESTION PIPELINE ────────────────────────────
    st.divider()
    st.markdown("### 🔄 24-Hour Automated Extraction & Ingestion Pipeline")
    st.caption("Architecture Data Flow: Cloud APIs ──► Azure Function Cron Sync ──► Star Schema DB ──► Streamlit UI")

    sync_info = get_latest_sync_log(selected_provider)

    with st.container(border=True):
        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("Latest Cron Sync Time", sync_info["synced_at"])
        sc2.metric("Execution Source", sync_info["source"])
        sc3.metric("Pipeline Status", sync_info["status"])

        st.caption(
            f"Per Capstone Architecture, an **Azure Function (Timer Trigger)** executes automatically every 24 hours (00:00 UTC) "
            f"to extract resource metadata from cloud APIs, normalize it into FOCUS format, and ingest it into the Star Schema database."
        )

        manual_sync_disabled = active_tenant is None
        if st.button(
            f"⚡ Run Manual Data Ingestion Sync Now ({selected_provider})",
            type="secondary", disabled=manual_sync_disabled,
            help=None if active_tenant else "Connect a tenant above first.",
        ):
            with st.spinner(f"Executing ingestion pipeline for tenant '{active_tenant.tenant_name}'..."):
                sync_creds = (
                    AzureCredentials(active_tenant.tenant_id, active_tenant.subscription_id,
                                      active_tenant.client_id, active_tenant.client_secret)
                    if is_azure else None
                )
                res = run_ingestion_pipeline(selected_provider, creds=sync_creds, tenant_db_id=active_tenant.id)
            if res["status"] == "SUCCESS":
                st.cache_data.clear()
                st.success(f"✅ {res['message']}")
                st.rerun()
            else:
                st.error(f"❌ {res['message']}")


# ═══════════════════════════════════════════════════════════════════════════════
# MANAGE — USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
def page_users():
    st.subheader("👥 Dashboard User Accounts")
    st.caption("Accounts that can sign in to this dashboard. Shared across everyone - not tied to a cloud tenant.")
    _finops_tag("Manage the FinOps Practice", "FinOps Practice Operations & Automation, Tools & Services")

    from db.users import list_users, create_user as _create_user

    # Demo and live accounts are separate scopes now (db/schema.py) - this
    # page always reflects whichever scope the current session is in, same
    # as every other data read in the app. In Demo Mode this correctly shows
    # only the single fixed demo account with no "add a user" ability to
    # abuse - real account management only makes sense in Live Cloud API mode.
    users_mode = "live" if is_live_mode else "demo"

    with st.container(border=True):
        for u in list_users(mode=users_mode):
            ucols = st.columns([3, 3, 2])
            ucols[0].markdown(f"**{u.display_name or u.username}** (`{u.username}`)")
            ucols[1].caption(f"Added {u.created_at[:10]} · Last login: {u.last_login_at[:10] if u.last_login_at else 'never'}")
            ucols[2].caption("🟢 Active" if u.is_active else "⚪ Inactive")

    if not is_live_mode:
        st.info("Switch to **Live Cloud API** mode to add or manage real user accounts - the demo account is fixed.", icon="ℹ️")
        return

    with st.expander("➕ Add a new user", expanded=False):
        with st.form("add_user_form"):
            nu_username = st.text_input("Username")
            nu_display = st.text_input("Display name (optional)")
            nu_pw1 = st.text_input("Password", type="password")
            nu_pw2 = st.text_input("Confirm password", type="password")
            nu_submit = st.form_submit_button("Add User", type="primary")
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


_TYPE_ICONS = {
    "Compute": "🖥️", "Azure SQL Database": "🗄️", "Azure SQL Managed Instance": "🗄️",
    "Azure Database for MySQL": "🐬", "Azure Database for PostgreSQL": "🐘",
    "Azure Cosmos DB": "🌐", "Azure Blob Storage": "📦", "Azure Files": "📁",
    "Azure Cache for Redis": "⚡", "Azure Cache for Redis Enterprise": "⚡",
    "Azure Synapse Analytics": "📊",
    "Azure Databricks": "🧱", "App Service": "🌍", "Azure Disk Storage": "💽",
    "Azure Dedicated Host": "🖲️", "Azure Container Instances": "📦",
    "Azure Container Apps": "🐳", "Azure Spring Apps Enterprise": "🌱",
    "Azure DocumentDB": "🍃", "Azure Database Migration Service": "🚚",
    "Microsoft Fabric": "🧵",
}


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


def _render_inventory_section(df: pd.DataFrame, key_prefix: str, show_type_col: bool):
    """Shared renderer for every per-service inventory sub-tab in the Inventory tab."""
    if df.empty:
        st.caption("No resources in this category.")
        return

    disp = df.copy()
    disp["Status"] = disp.apply(
        lambda r: "Orphaned" if r["Is Orphaned"]
        else ("Running" if r["Resource State"] == "Running" else "Stopped"),
        axis=1,
    )
    def _payg_cell(r):
        if r["PAYG Hourly Cost USD"]:
            return fmt(r["PAYG Hourly Cost USD"], 4)
        reason = _payg_blank_reason(r["Resource Type"], r["SKU"])
        return (reason[:87] + "...") if len(reason) > 90 else reason

    disp["PAYG Cost/hr"] = disp.apply(_payg_cell, axis=1)
    disp["Est. Monthly PAYG Cost"] = (
        disp["PAYG Hourly Cost USD"] * disp["Avg Daily Running Hours"] * 30
    ).apply(lambda x: fmt(x, 2) if x else "—")

    # Live mode: show which tenant a subscription ID belongs to, not just the raw GUID.
    if is_live_mode and is_live_configured and active_tenant is not None:
        disp["Subscription"] = disp["Subscription"].apply(
            lambda sid: f"{active_tenant.tenant_name} ({sid})" if sid else active_tenant.tenant_name
        )

    state_opts = disp["Resource State"].unique().tolist()
    region_opts = disp["Region"].unique().tolist()
    f1, f2, f3 = st.columns([1, 1, 0.9])
    with f1:
        fs = st.multiselect("Power State", state_opts, default=state_opts, key=f"{key_prefix}_state")
    with f2:
        fr = st.multiselect("Region", region_opts, default=region_opts, key=f"{key_prefix}_region")
    with f3:
        st.write("")
        focus_view = st.toggle("🔭 FOCUS View", key=f"{key_prefix}_focus", help="Show columns mapped to the FinOps Open Cost & Usage Specification (FOCUS) instead of the app's internal display names.")
    active_fs = fs if fs else state_opts
    active_fr = fr if fr else region_opts
    filtered = disp[disp["Resource State"].isin(active_fs) & disp["Region"].isin(active_fr)]

    if focus_view:
        raw_filtered = df.loc[filtered.index]
        focus_df = to_focus_view(raw_filtered, selected_provider)
        with st.expander(f"ℹ️ FOCUS v{FOCUS_SPEC_VERSION} column reference", expanded=False):
            st.caption(f"Columns below follow the [FinOps Open Cost & Usage Specification]({FOCUS_SPEC_URL}) v{FOCUS_SPEC_VERSION}. Each is mapped from this app's internal schema as noted.")
            st.dataframe(
                pd.DataFrame(FOCUS_COLUMN_DEFINITIONS, columns=["FOCUS Column", "Spec Definition", "How it's populated here"]),
                hide_index=True, width="stretch",
            )
            st.caption(
                "**Note on BilledCost/EffectiveCost:** BilledCost is shown equal to ListCost at this per-resource "
                "snapshot. This app *does* compute real discount economics from active RI/Savings-Plan commitments, "
                "but that math applies against pooled commitments across many resources - see the **RI Coverage** "
                "and **Savings Plan Analysis** tabs for the actual $ savings, rather than a guessed per-resource split."
            )
        st.dataframe(
            focus_df, hide_index=True, width="stretch",
            column_config={
                "ListUnitPrice": st.column_config.NumberColumn("ListUnitPrice", format="$%.4f"),
                "ListCost":      st.column_config.NumberColumn("ListCost", format="$%.2f"),
                "BilledCost":    st.column_config.NumberColumn("BilledCost", format="$%.2f"),
            },
        )
        return

    all_cols = ["Resource ID", "Resource Name", "Subscription"]
    if show_type_col:
        all_cols.append("Resource Type")
    all_cols += ["Status", "Region", "OS", "SKU", "Est. Monthly PAYG Cost", "PAYG Cost/hr"]
    all_cols = [c for c in all_cols if c in filtered.columns]
    default_cols = [c for c in all_cols if c != "Resource ID"]  # Resource ID hidden by default - toggle back on if needed

    chosen_cols = st.multiselect("Columns to display", all_cols, default=default_cols, key=f"{key_prefix}_cols")
    if not chosen_cols:
        chosen_cols = default_cols

    st.dataframe(
        filtered[chosen_cols], hide_index=True, width="stretch",
        column_config={
            "Resource Type":          st.column_config.TextColumn("Service"),
            "Status":                 st.column_config.TextColumn("Power State"),
            "Est. Monthly PAYG Cost": st.column_config.TextColumn("Est. Monthly PAYG Cost"),
            "PAYG Cost/hr":           st.column_config.TextColumn("PAYG Cost/hr"),
        },
    )


def _render_inventory_tab():
    st.subheader(f"{selected_provider} Infrastructure Asset Inventory")
    st.caption(
        f"Real-time resource registry across compute and database domains in {selected_provider}."
    )
    _finops_tag("Understand Usage & Cost", "Data Ingestion, Reporting & Analytics")

    if not inv_raw.empty:
        c_chart, c_meta = st.columns([1.5, 1])
        with c_chart:
            fig_donut = get_cost_distribution_chart(inv_raw, selected_provider, is_dark=is_dark_theme)
            st.plotly_chart(fig_donut, use_container_width=True)
        with c_meta:
            st.markdown("#### 📊 Domain Summary")
            st.markdown(f"• Total Managed Resources: **{len(inv_raw)}**")
            st.markdown(f"• Active Compute Workloads: **{len(vm_inventory)}** ({compute_label})")
            st.markdown(f"• Active Database Engines: **{len(db_inventory)}** ({db_label})")
            if len(other_inventory) > 0:
                st.markdown(f"• Other Managed Services: **{len(other_inventory)}**")
            st.markdown(f"• Total PAYG Run Rate: **{fmt(total_vm_payg_hr + total_db_payg_hr, 4)}/hr**")
            st.markdown(f"• Total Monthly Baseline: **{fmt((total_vm_payg_hr + total_db_payg_hr) * 730, 2)}/mo**")

    st.divider()

    mode_tag = env_mode.replace(" ", "_").replace("/", "_")
    resource_types_present = sorted(inv_raw["Resource Type"].unique().tolist()) if not inv_raw.empty else []

    tab_defs = [("📋", "All Resources", None)] + [
        (_TYPE_ICONS.get(rtype, "🔹"), rtype, rtype) for rtype in resource_types_present
    ]
    type_tabs = st.tabs([
        f"{icon} {label} ({len(inv_raw) if rtype is None else len(inv_raw[inv_raw['Resource Type'] == rtype])})"
        for icon, label, rtype in tab_defs
    ])

    for tab_widget, (icon, label, rtype) in zip(type_tabs, tab_defs):
        with tab_widget:
            section_df = inv_raw if rtype is None else inv_raw[inv_raw["Resource Type"] == rtype]
            key = f"{selected_provider}_{mode_tag}_{rtype or 'all'}".replace(" ", "_")
            _render_inventory_section(section_df, key, show_type_col=(rtype is None))


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYZE — SAVINGS PLAN ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════
def _render_sp_pool_economics(pool_label: str, pool_df: pd.DataFrame, existing_commitment_hr: float,
                               key_prefix: str, safety_buffer_frac: float, available_terms=("1yr", "3yr")):
    """Plain eligible -> committed -> remaining -> recommended flow for one
    Savings Plan pool (Compute or Database):
      1. which resources are eligible (shown as a table)
      2. their total PAYG $/hr
      3. how much of that is already committed
      4. how much is left, and how much of THAT the safety buffer recommends
         committing next.
    Real 1yr/3yr committed-rate pricing (where cached) lives in a collapsed
    detail section below - useful, but deliberately not driving the headline
    numbers, which stay in directly-comparable PAYG-dollar terms instead of
    silently mixing PAYG and committed-rate units the way the old version did.
    available_terms restricts which term(s) can be modeled - e.g. Database
    Savings Plans are 1-year only per Azure policy, so that pool never gets a
    3-year option here."""
    if pool_df.empty:
        st.caption(f"No resources are currently eligible for {pool_label} Savings Plan.")
        return

    baseline_hr = float(pool_df["PAYG Hourly Cost USD"].sum())
    remaining_hr = max(0.0, baseline_hr - existing_commitment_hr)
    recommended_hr = round(remaining_hr * safety_buffer_frac, 4)
    leakage_hr = max(0.0, existing_commitment_hr - baseline_hr)

    with st.expander(f"📋 {len(pool_df)} eligible resource(s) in this pool", expanded=len(pool_df) <= 6):
        elig_show = pool_df[["Resource Name", "Resource Type", "SKU", "PAYG Hourly Cost USD"]].copy()
        elig_show["PAYG Hourly Cost USD"] = elig_show["PAYG Hourly Cost USD"].apply(lambda x: fmt(x, 4))
        elig_show = elig_show.rename(columns={"PAYG Hourly Cost USD": "PAYG Cost/hr"})
        st.dataframe(elig_show, hide_index=True, width="stretch")

    with st.container(border=True):
        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("Eligible PAYG Cost/hr", fmt(baseline_hr) + "/hr")
        cc2.metric("Already Committed", fmt(existing_commitment_hr) + "/hr")
        cc3.metric(
            "Not Yet Committed", fmt(remaining_hr) + "/hr",
            delta=f"Over-committed by {fmt(leakage_hr)}/hr" if leakage_hr > 0 else None,
            delta_color="inverse",
        )
        cc4.metric(
            f"Recommended Purchase ({int(safety_buffer_frac*100)}% buffer)", fmt(recommended_hr) + "/hr",
            help="The rest stays on pay-as-you-go as headroom, in case usage drops.",
        )

    if len(available_terms) > 1:
        default_term = st.session_state.get(f"{key_prefix}_term_widget", "1yr")
        term_choice = st.segmented_control(
            f"Model {pool_label} commitment at term",
            options=["1-Year", "3-Year"],
            default=TERM_LABELS[default_term],
            key=f"{key_prefix}_term_display",
            help="Drives the detailed pricing below and the Recommendations tab's combined savings projection.",
        )
        term_key = "1yr" if term_choice == "1-Year" else "3yr"
    else:
        term_key = available_terms[0]
        st.caption(f"{pool_label} Savings Plans only support a {TERM_LABELS[term_key]} term (Azure policy).")
    st.session_state[f"{key_prefix}_term_widget"] = term_key

    cmp_df = savings_plan_term_comparison(pool_df, prices_df) if (is_azure and prices_df is not None and not prices_df.empty) else None
    if cmp_df is not None:
        cmp_df = cmp_df[cmp_df["term_key"].isin(available_terms)].reset_index(drop=True)
    has_real_pricing = cmp_df is not None and int(cmp_df["Priced Resources"].sum()) > 0

    if has_real_pricing:
        with st.expander("📊 Detailed pricing by term", expanded=False):
            show_df = cmp_df[["Term", "PAYG $/hr", "Committed $/hr", "Discount %", "Monthly Savings"]].copy()
            show_df["PAYG $/hr"] = cmp_df["PAYG $/hr"].apply(lambda x: fmt(x, 4))
            show_df["Committed $/hr"] = cmp_df["Committed $/hr"].apply(lambda x: fmt(x, 4))
            show_df["Discount %"] = cmp_df["Discount %"].apply(lambda x: f"{x:.1f}%")
            show_df["Monthly Savings"] = cmp_df["Monthly Savings"].apply(lambda x: fmt(x, 2))
            st.dataframe(show_df, hide_index=True, width="stretch")

            chosen = cmp_df[cmp_df["term_key"] == term_key].iloc[0]
            discount_pct = float(chosen["Discount %"])
            est_monthly_savings = recommended_hr * (discount_pct / 100.0) * 730
            st.caption(
                f"At the {TERM_LABELS[term_key]} rate ({discount_pct:.1f}% off PAYG), committing "
                f"{fmt(recommended_hr)}/hr is estimated to save about {fmt(est_monthly_savings, 2)}/month."
            )
    elif not is_azure:
        st.caption("ℹ️ Illustrative only — real-time AWS Savings Plans pricing isn't wired up yet.")
    else:
        st.caption("ℹ️ Illustrative only — no cached commitment pricing yet for this pool's SKUs; re-run a sync on the Tenants page.")


def _render_savings_plan_tab():
    st.subheader(f"{selected_provider} Savings Plan Analysis")
    st.caption("Shows which resources run continuously, compares them to what you've already committed, and recommends how much more to commit.")
    _finops_tag("Optimize Usage & Cost", "Rate Optimization")

    with st.expander(f"📋 {selected_provider} Savings Plan Coverage Policy", expanded=False):
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
            sp_coverage_rows = [
                {
                    "Savings Plan Type": "Compute Savings Plans (1-yr / 3-yr)",
                    "What Is Covered": "Amazon EC2, AWS Fargate, and AWS Lambda usage across any region, instance family, OS, or tenancy (up to 66% discount)",
                    "What Is NOT Covered": "EBS storage volumes, data transfer/bandwidth, software licensing surcharges, non-compute services"
                },
                {
                    "Savings Plan Type": "EC2 Instance Savings Plans (1-yr / 3-yr)",
                    "What Is Covered": "EC2 instance usage within a specific family in a designated Region (e.g., m5 in us-east-1, up to 72% discount)",
                    "What Is NOT Covered": "Amazon RDS databases, ElastiCache, Redshift, S3 storage, or instances outside the specified family/region"
                }
            ]
        st.dataframe(pd.DataFrame(sp_coverage_rows), hide_index=True, width="stretch")

    if not is_azure:
        st.info(
            "ℹ️ **AWS Commitment Policy Note:** AWS Savings Plans apply to **Compute (EC2, Fargate, Lambda)** "
            "and **EC2 Instance Families**. AWS database services (RDS, Aurora, DynamoDB) are covered under "
            "**RDS Reserved Instances** on the RI Coverage tab.",
            icon="ℹ️"
        )

    safety_buffer_pct_local = st.slider(
        "Safety Buffer % — how much of the steady-state footprint to commit",
        min_value=50, max_value=100,
        value=st.session_state.get("sp_safety_buffer_widget", int(DEFAULT_SAFETY_BUFFER * 100)),
        step=5, key="sp_safety_buffer_widget",
        help="The rest stays on PAYG as headroom, protecting against usage drops. Applies to both pools below.",
    )
    st.divider()

    st.markdown("### A — Compute Savings Plan Pool")
    st.caption("Resources below run 24 hours a day, so committing against them is safe — the usage won't drop. Resources that don't run continuously are excluded and stay on pay-as-you-go.")

    _render_sp_pool_economics("Compute", compute_24x7, compute_sp_commit, "sp_compute", safety_buffer)

    if is_live_mode and is_live_configured:
        if compute_sp_pool_inventory.empty:
            st.info(
                "ℹ️ No Savings Plan for Compute-eligible resource types found in this tenant "
                "(VMs, App Service, Functions Premium, Container Instances, Dedicated Host, "
                "Container Apps, Spring Apps) - nothing to baseline yet.", icon="ℹ️",
            )
        elif compute_24x7_candidates.empty:
            st.info(
                f"ℹ️ {len(compute_sp_pool_inventory)} SP-eligible-type resource(s) found, but none are "
                "both **Running** and **24x7** (Avg Daily Running Hours = 24) - Savings Plans are only "
                "recommended against a steady-state 24x7 baseline to avoid over-committing.", icon="ℹ️",
            )
        elif compute_24x7.empty:
            st.warning(
                f"⚠️ {len(compute_24x7_candidates)} resource(s) are running 24x7, but none are actually "
                "eligible for Savings Plan for Compute at their current SKU/tier - see the breakdown below.",
                icon="⚠️",
            )

        if not compute_sp_excluded.empty:
            with st.expander(f"🚫 {len(compute_sp_excluded)} running resource(s) excluded from the Compute SP baseline", expanded=False):
                st.dataframe(
                    compute_sp_excluded[["Resource Name", "Resource Type", "SKU", "SP Eligibility Note"]],
                    hide_index=True, width="stretch",
                    column_config={"SP Eligibility Note": st.column_config.TextColumn("Why excluded", width="large")},
                )

    if not compute_sp_df.empty:
        sp_c = compute_sp_df[["commitment_id", "scope_sku", "scope_region", "hourly_usd_commitment", "term", "expiry_date"]].copy()
        sp_c["hourly_usd_commitment"] = sp_c["hourly_usd_commitment"].apply(lambda x: fmt(x, 4) + "/hr")
        st.dataframe(sp_c, hide_index=True, width="stretch")

    st.divider()

    st.markdown(f"### B — {db_sp_title} Pool")
    st.caption("Resources below run continuously, so committing against them is safe. Resources that aren't currently running are excluded and stay on pay-as-you-go.")

    # Azure's Savings Plan for Databases is a real, 1-year-only product (see
    # the Coverage Policy expander above) - AWS's EC2 Instance Savings Plans
    # genuinely do offer both terms, so the restriction is Azure-specific.
    db_available_terms = ("1yr",) if is_azure else ("1yr", "3yr")
    _render_sp_pool_economics(db_label, db_running, db_sp_commit, "sp_db", safety_buffer, available_terms=db_available_terms)

    if is_live_mode and is_live_configured:
        if db_inventory.empty:
            st.info(f"ℹ️ No {db_sp_title}-eligible resource types found in this tenant - nothing to baseline yet.", icon="ℹ️")
        elif db_running_candidates.empty:
            st.info(
                f"ℹ️ {len(db_inventory)} SP-eligible-type database resource(s) found, but none are "
                "currently Running.", icon="ℹ️",
            )
        elif db_running.empty:
            st.warning(
                f"⚠️ {len(db_running_candidates)} database resource(s) are running, but none are actually "
                "eligible for Savings Plan for Databases at their current tier - see the breakdown below.",
                icon="⚠️",
            )

        if not db_sp_excluded.empty:
            with st.expander(f"🚫 {len(db_sp_excluded)} running resource(s) excluded from the {db_sp_title} baseline", expanded=False):
                st.dataframe(
                    db_sp_excluded[["Resource Name", "Resource Type", "SKU", "SP Eligibility Note"]],
                    hide_index=True, width="stretch",
                    column_config={"SP Eligibility Note": st.column_config.TextColumn("Why excluded", width="large")},
                )

    if not db_sp_df.empty:
        sp_d = db_sp_df[["commitment_id", "scope_sku", "scope_region", "hourly_usd_commitment", "term", "expiry_date"]].copy()
        sp_d["hourly_usd_commitment"] = sp_d["hourly_usd_commitment"].apply(lambda x: fmt(x, 4) + "/hr")
        st.dataframe(sp_d, hide_index=True, width="stretch")


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYZE — RI COVERAGE
# ═══════════════════════════════════════════════════════════════════════════════
def _render_ri_coverage_tab():
    """Mirrors the Savings Plan tab's clarity principles: a clean, primary
    table for the resources where a gap is a literal purchase recommendation
    (coverage_model == 'instance'), with pooled-capacity, volume-based, and
    not-eligible resources moved into their own labeled expanders instead of
    all being crammed into one wide table with paragraph-length notes stuffed
    into a 'Coverage Note' column - that was the actual complaint (this tab
    hadn't been touched by the Savings Plan cleanup, so it looked worse by
    comparison once that one got simplified)."""
    st.subheader(f"{selected_provider} Reserved Instance & Reserved Capacity Coverage")
    st.caption("Compares what's running against what you've already reserved, resource by resource, and flags real gaps to fix.")
    _finops_tag("Optimize Usage & Cost", "Rate Optimization")

    with st.expander(f"📋 {selected_provider} Reservation Coverage Rules", expanded=False):
        if is_azure:
            from db.seed import RI_COVERAGE_NOTES
        else:
            from db.aws_seed import AWS_RI_COVERAGE_NOTES as RI_COVERAGE_NOTES

        coverage_rows = [
            {"Service Domain": svc, "Coverage Scope": covers, "Exclusions": excludes}
            for svc, (covers, excludes) in RI_COVERAGE_NOTES.items()
        ]
        st.dataframe(pd.DataFrame(coverage_rows), hide_index=True, width="stretch")

    default_ri_term = st.session_state.get("ri_term_widget", "1yr")
    ri_term_choice = st.segmented_control(
        "Model new-purchase pricing at term",
        options=["1-Year", "3-Year"],
        default=TERM_LABELS[default_ri_term],
        key="ri_term_display",
        help="Drives the purchase-cost columns below and the Recommendations tab's combined savings projection.",
    )
    ri_term_key = "1yr" if ri_term_choice == "1-Year" else "3yr"
    st.session_state["ri_term_widget"] = ri_term_key

    with st.expander("📄 Active Reservation Contracts", expanded=False):
        if not ri_df.empty:
            ri_disp = ri_df[["commitment_id", "commitment_type", "scope_sku", "scope_region", "scope_os", "reserved_qty", "hourly_usd_commitment", "term", "expiry_date"]].copy()
            ri_disp["hourly_usd_commitment"] = ri_disp["hourly_usd_commitment"].apply(lambda x: fmt(x, 4) + "/hr each")
            st.dataframe(ri_disp, hide_index=True, width="stretch")
        else:
            st.info("No active Reserved Instance contracts found.")

    raw_cov = ri_result.coverage_table
    if is_azure and prices_df is not None and not prices_df.empty and not raw_cov.empty:
        cov = ri_gap_pricing(raw_cov, inv_raw, prices_df).copy()
    else:
        cov = raw_cov.copy()

    if cov.empty:
        st.info("No reservation-eligible resources found yet.")
        return

    def _status(row):
        if row["gap"] > 0:
            return f"⚠️ Short by {int(row['gap'])}"
        if row["excess"] > 0:
            return f"ℹ️ {int(row['excess'])} Idle"
        return "✅ Fully Covered"

    cov["Status"] = cov.apply(_status, axis=1)

    elig = cov[cov["is_eligible"]]
    ineligible = cov[~cov["is_eligible"]]
    instance_cov = elig[elig["coverage_model"] == "instance"]
    capacity_cov = elig[elig["coverage_model"] == "capacity"]
    unmeasurable_cov = elig[elig["coverage_model"] == "unmeasurable"]

    with st.container(border=True):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Fully Covered", int(((instance_cov["gap"] == 0) & (instance_cov["excess"] == 0)).sum()))
        m2.metric("Needs More RI", int((instance_cov["gap"] > 0).sum()))
        m3.metric("Idle / Unused RI", int((instance_cov["excess"] > 0).sum()))
        m4.metric("Not RI-Eligible", len(ineligible))

    st.markdown("#### Per-Resource Coverage")
    st.caption("Each row is one resource profile (SKU + region + OS). Reservations for these types are bought per unit, so a gap here is a real, literal purchase recommendation.")
    if not instance_cov.empty:
        rate_col = f"RI Rate {ri_term_choice} ($/hr)"
        savings_col = f"Monthly Savings if Purchased ({ri_term_choice})"
        has_pricing_cols = rate_col in instance_cov.columns
        show = instance_cov.rename(columns={
            "Resource Type": "Service", "SKU": "SKU / Tier",
            "running_count": "Running", "reserved_qty": "Reserved",
        }).copy()
        if has_pricing_cols:
            show[rate_col] = show[rate_col].apply(lambda x: fmt(x, 4) if pd.notna(x) else "—")
            show[savings_col] = show[savings_col].apply(lambda x: fmt(x, 2) if pd.notna(x) else "—")
        cols = ["Service", "SKU / Tier", "Region", "OS", "Running", "Reserved", "Status"]
        if has_pricing_cols:
            cols += [rate_col, savings_col]
        st.dataframe(show[[c for c in cols if c in show.columns]], hide_index=True, width="stretch")
        if not has_pricing_cols:
            st.caption(
                "ℹ️ Purchase-cost columns aren't shown - "
                + ("AWS Reserved Instance pricing isn't wired up yet." if not is_azure else "no cached pricing yet for these SKUs; re-run a sync on the Tenants page.")
            )
    else:
        st.caption("No per-instance-reservable resources in inventory yet.")

    if not capacity_cov.empty:
        with st.expander(f"ℹ️ {len(capacity_cov)} pooled-capacity resource(s) - not a per-instance purchase", expanded=False):
            st.caption(
                "Azure applies these reservations automatically across ALL matching resources in your "
                "subscription (RU/s, DBCU, cDWU, or vCore-hours), not to one specific resource - so the gap "
                "below is a rough signal, not a literal purchase instruction. Compare actual usage against "
                "your reservation size in Azure Cost Management before buying more."
            )
            show_c = capacity_cov.rename(columns={
                "Resource Type": "Service", "SKU": "SKU / Tier",
                "running_count": "Running", "reserved_qty": "Reserved",
            })
            st.dataframe(show_c[["Service", "SKU / Tier", "Region", "Running", "Reserved", "Status"]], hide_index=True, width="stretch")

    if not unmeasurable_cov.empty:
        with st.expander(f"📏 {len(unmeasurable_cov)} volume-based resource(s) - not tracked here", expanded=False):
            st.caption(
                "Reserved capacity for these services is sold in blocks far larger than a single resource "
                "(Storage: 100 TB / 1 PB; Files: 10 TiB / 100 TiB) and applies across your whole "
                "subscription's usage, not per resource. This dashboard tracks resource count, not data "
                "volume, so coverage genuinely can't be assessed here - check total volume in Azure Cost "
                "Management or Storage metrics before considering a purchase."
            )
            show_u = unmeasurable_cov.rename(columns={"Resource Type": "Service", "SKU": "SKU / Tier"})
            st.dataframe(show_u[["Service", "SKU / Tier", "Region"]], hide_index=True, width="stretch")

    if not ineligible.empty:
        with st.expander(f"🚫 {len(ineligible)} resource(s) not eligible for any Reservation", expanded=False):
            show_i = ineligible.rename(columns={"Resource Type": "Service", "SKU": "SKU / Tier", "eligibility_reason": "Why not eligible"})
            st.dataframe(show_i[["Service", "SKU / Tier", "Region", "Why not eligible"]], hide_index=True, width="stretch")

    if not ri_result.orphaned_ri_drain.empty:
        st.error(f"**{len(ri_result.orphaned_ri_drain)} stopped resource(s) draining active reservations**!")
        st.dataframe(ri_result.orphaned_ri_drain, hide_index=True, width="stretch")


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYZE — COST ANALYSIS (new)
# ═══════════════════════════════════════════════════════════════════════════════
def _render_cost_analysis_tab():
    st.subheader(f"{selected_provider} Cost Analysis")
    st.caption("Current spend baseline and commitment coverage - see the Recommendations tab for the combined savings projection.")
    _finops_tag("Optimize Usage & Cost", "Rate Optimization")

    st.segmented_control(
        "Analysis Window (days)", options=[7, 14, 30],
        default=st.session_state.get("analysis_window_widget", 30),
        key="analysis_window_widget",
        help="Historical daily evaluation period feeding the leakage / orphaned-capacity calculations used across this app. Changing this re-runs the simulation on the next interaction.",
    )

    if not inv_raw.empty:
        fig_waterfall = get_waterfall_savings_chart(total_vm_payg_hr, total_db_payg_hr, total_sp_commit, total_ri_commit, selected_provider, is_dark=is_dark_theme)
        st.plotly_chart(fig_waterfall, use_container_width=True)
    else:
        st.info("No inventory data to chart yet.")

    st.caption("For the projected savings if open recommendations are implemented, see the **Recommendations** tab.")


def _real_projected_savings():
    """The commitment-pricing-based combined SP+RI savings projection - the
    MORE ACCURATE of this app's two savings figures (the other being
    generate_recommendations()'s safety-buffer heuristic, used for individual
    per-item $ impacts where real cached pricing isn't available). Returns
    None when real pricing genuinely isn't available (AWS, or nothing cached
    yet for this tenant's SKUs) so the caller can fall back to the heuristic
    total instead of showing a wrong/absent number."""
    if not is_azure or prices_df is None or prices_df.empty:
        return None
    sp_term = st.session_state.get("sp_compute_term_widget", "1yr")
    ri_term = st.session_state.get("ri_term_widget", "1yr")
    sp_pool_cmps = []
    if not compute_24x7.empty:
        sp_pool_cmps.append(savings_plan_term_comparison(compute_24x7, prices_df))
    if not db_running.empty:
        sp_pool_cmps.append(savings_plan_term_comparison(db_running, prices_df))
    ri_priced = ri_gap_pricing(ri_result.coverage_table, inv_raw, prices_df) if not ri_result.coverage_table.empty else pd.DataFrame()
    return combined_monthly_savings(sp_pool_cmps, ri_priced, sp_term, ri_term)


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYZE — RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════════════════
def _render_recommendations_tab():
    st.subheader(f"Actionable FinOps Recommendations ({selected_provider})")
    st.caption("One clear next action per issue - not one card per affected resource.")
    _finops_tag("Optimize Usage & Cost", "Usage Optimization & Rate Optimization")

    high_count = sum(1 for r in recs if r.get("severity") == "HIGH")
    med_count  = sum(1 for r in recs if r.get("severity") == "MEDIUM")
    heuristic_savings_mo = sum(r.get("financial_impact_hr", 0.0) for r in recs) * 730

    # Two savings figures exist in this app: this heuristic total (a rough,
    # always-available safety-buffer estimate) and the real commitment-
    # pricing-based projection below (accurate, but needs cached Azure
    # pricing to compute). Previously these lived in two different tabs
    # showing two different numbers for a similarly-worded metric - a real
    # source of confusion. Now: the real figure REPLACES the heuristic one
    # in the headline metric whenever it's available, with the heuristic
    # kept only as an always-on fallback - one trustworthy number, not two
    # competing ones.
    real_savings = _real_projected_savings()
    headline_savings_mo = real_savings["total_monthly_savings"] if real_savings else heuristic_savings_mo
    savings_label = "Total Monthly Savings Potential" if real_savings else "Total Monthly Savings Potential (estimated)"

    with st.container(border=True):
        rc1, rc2, rc3 = st.columns(3)
        rc1.metric("Critical Priority Actions", f"{high_count} items", delta="Immediate Action Required" if high_count > 0 else "None", delta_color="inverse" if high_count > 0 else "off")
        rc2.metric("Purchase / Review Opportunities", f"{med_count} items", delta="Savings Available" if med_count > 0 else "Optimal", delta_color="normal" if med_count > 0 else "off")
        rc3.metric(savings_label, fmt(headline_savings_mo, 2) + "/mo", delta="Identified Opportunity")

    if real_savings:
        with st.expander("💡 Savings Plan vs. Reserved Instance breakdown (real cached pricing)", expanded=False):
            st.caption(
                f"Using the term choices selected on the Savings Plan Analysis tab ({real_savings['sp_term']} for Compute) "
                f"and the RI Coverage tab ({real_savings['ri_term']}) - change them there to update this."
            )
            b1, b2, b3 = st.columns(3)
            b1.metric(f"Savings Plan ({real_savings['sp_term']})", fmt(real_savings["sp_monthly_savings"], 2) + "/mo")
            b2.metric(f"Reserved Instance ({real_savings['ri_term']})", fmt(real_savings["ri_monthly_savings"], 2) + "/mo")
            b3.metric("Combined Total", fmt(real_savings["total_monthly_savings"], 2) + "/mo")
    else:
        st.info(
            "ℹ️ Real commitment-rate pricing isn't available yet for AWS (or there's no cached pricing for "
            "this Azure tenant's SKUs) - the figure above is the safety-buffer estimate used for each "
            "recommendation's individual $ impact below, not a real cached rate.", icon="ℹ️",
        )

    fig_recs = get_recommendation_opportunity_chart(recs, is_dark=is_dark_theme)
    st.plotly_chart(fig_recs, use_container_width=True)

    st.divider()
    actionable = [r for r in recs if r.get("type") != "OPTIMAL"]
    if not actionable:
        st.success(f"✅ Commitment portfolio is optimally configured for {selected_provider} - no optimization opportunities detected right now.")
    else:
        severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        for r in sorted(actionable, key=lambda r: severity_order.get(r.get("severity"), 3)):
            impact_mo = r.get("financial_impact_hr", 0.0) * 730
            with st.container(border=True):
                col_t, col_i = st.columns([3, 1])
                with col_t:
                    st.markdown(f"### {r.get('icon', '')} {r['title']}")
                    st.caption(r.get("category", ""))
                with col_i:
                    if impact_mo > 0:
                        st.markdown(f"#### 💰 {fmt(impact_mo, 2)}/mo")
                    else:
                        st.markdown("#### Sizing action")
                st.markdown(r["detail"])
                st.info(f"**Next action:** {r.get('action', '')}")
                items = r.get("items") or []
                if items:
                    with st.expander(f"📋 {len(items)} affected resource{'s' if len(items) != 1 else ''}"):
                        st.dataframe(pd.DataFrame(items), hide_index=True, width="stretch")

    with st.expander(f"📋 Full recommendation table ({len(recs)} categories)", expanded=False):
        master_data = [
            {
                "Severity": r.get("severity", "LOW"),
                "Category": r.get("category", ""),
                "Title": r.get("title", ""),
                "Next Action": r.get("action", ""),
                "Est. Monthly Savings": fmt(r.get("financial_impact_hr", 0.0) * 730, 2),
            }
            for r in recs
        ]
        st.dataframe(pd.DataFrame(master_data), hide_index=True, width="stretch")


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
# WORKSPACE — ANALYZE (single page, top tabs inside)
# ═══════════════════════════════════════════════════════════════════════════════
def page_analyze():
    _render_top_header()
    tabs = st.tabs([
        "🔍 Inventory",
        "💰 Savings Plan Analysis",
        "🏷️ RI Coverage",
        "📊 Cost Analysis",
        "⚡ Recommendations",
        "🧭 Maturity Assessment",
    ])
    with tabs[0]:
        _render_inventory_tab()
    with tabs[1]:
        _render_savings_plan_tab()
    with tabs[2]:
        _render_ri_coverage_tab()
    with tabs[3]:
        _render_cost_analysis_tab()
    with tabs[4]:
        _render_recommendations_tab()
    with tabs[5]:
        _render_maturity_tab()


# ─────────────────────────────────────────────────────────────────────────────
# NAVIGATION — small left sidebar rail: Manage (Tenants, User Management) and
# a single Workspace entry whose content is the top-tabbed Analyze view.
# ─────────────────────────────────────────────────────────────────────────────
tenants_page = st.Page(page_tenants, title="Tenants", icon="🏢")
users_page   = st.Page(page_users,   title="User Management", icon="👥")
analyze_page = st.Page(page_analyze, title="Analyze", icon="📊", default=True)

pg = st.navigation({
    "🏢 Manage":    [tenants_page, users_page],
    "📊 Workspace": [analyze_page],
}, position="sidebar")

# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR CONTROLS — rendered below the nav menu above. Only cross-cutting
# controls live here now (platform, data source, currency, account) -
# Commitment Term / Safety Buffer / Analysis Window moved into the sections
# that actually use them (Savings Plan Analysis, RI Coverage, Cost Analysis).
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    def reset_app_cache():
        st.cache_data.clear()

    # ── ☁️ Cloud & Data Source ──────────────────────────────────────────────
    st.markdown("#### ☁️ Cloud & Data Source")

    selected_provider = st.segmented_control(
        "Cloud Platform",
        options=["Azure", "AWS"],
        default="Azure",
        key="provider_selector_widget",
        on_change=reset_app_cache,
        help="Select cloud environment to optimize.",
    )
    if not selected_provider:
        selected_provider = "Azure"

    # Deliberately NOT a free-switching widget - see
    # ui.auth_page.render_switch_mode_control's docstring for why: demo and
    # live are separate login accounts and separate data now, so switching
    # means signing out and back in, not flipping a toggle mid-session.
    env_mode = render_switch_mode_control()

    if env_mode == "Live Cloud API" and st.button("🔁 Manage Tenant Connection", use_container_width=True, help="Connect, switch, or review your active Azure tenant."):
        st.session_state["setup_complete"] = False
        st.rerun()

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
    if not selected_currency:
        selected_currency = initial_currency

    # Update query parameters to persist selection across page refreshes
    st.query_params["currency"] = selected_currency

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
db_label = "Database Services" if is_azure else "RDS Databases"
db_sp_title = "Savings Plan for Databases" if is_azure else "EC2 Instance Savings Plan"
db_eligible_types = DATABASE_SP_ELIGIBLE_TYPES if is_azure else AWS_DATABASE_SP_TYPES
compute_sp_eligible_types = COMPUTE_SP_ELIGIBLE_TYPES if is_azure else AWS_COMPUTE_SP_TYPES

# Analysis Window now lives inside the Cost Analysis tab (as a widget keyed
# "analysis_window_widget") - read its persisted value here, before that
# widget renders later in the script, same pattern env_mode_widget already uses.
simulate_days = st.session_state.get("analysis_window_widget", 30)
# Safety Buffer now lives inside the Savings Plan Analysis tab.
safety_buffer_pct = st.session_state.get("sp_safety_buffer_widget", int(DEFAULT_SAFETY_BUFFER * 100))
safety_buffer = safety_buffer_pct / 100.0


# Check Live Credentials — the connected-tenant registry in SQL DB is the single
# source of truth for "is live configured", not the .env file (which only exists
# to pre-fill the connection form / support the cron function outside Streamlit).
active_tenant = get_active_tenant(selected_provider)
is_live_configured = active_tenant is not None
is_live_mode = (env_mode == "Live Cloud API")

@st.cache_data(show_spinner=False)
def load_benchmark_data(days: int, buffer: float, provider: str, sp_eligible_types: tuple):
    inv_raw       = get_compute_inventory(provider=provider, mode="demo")
    sp_df         = get_existing_savings_plans(provider=provider, mode="demo")
    compute_sp_df = get_compute_savings_plans(provider=provider, mode="demo")
    db_sp_df      = get_database_savings_plans(provider=provider, mode="demo")
    ri_df         = get_existing_reservations(provider=provider, mode="demo")
    wf            = run_waterfall(inv_raw, ri_df, sp_df, simulate_days=days)
    sp_res        = savings_plan_analysis(inv_raw, sp_df, safety_buffer=buffer, eligible_types=list(sp_eligible_types))
    ri_res        = reservation_analysis(inv_raw, ri_df)
    recs          = generate_recommendations(sp_res, ri_res, wf, safety_buffer=buffer)
    return inv_raw, sp_df, compute_sp_df, db_sp_df, ri_df, sp_res, ri_res, recs

@st.cache_data(show_spinner=False)
def load_live_data(provider: str, tenant_id: int, days: int, buffer: float, sp_eligible_types: tuple):
    """Same shape as load_benchmark_data, but reads ONLY the given tenant's
    live-ingested rows (tenant_id FK) from SQL DB - never demo/seed rows, and
    never another tenant's rows. The app never calls cloud APIs directly here;
    everything was already fetched and cached in SQL DB by the ingestion pipeline."""
    inv_raw       = get_compute_inventory(provider=provider, mode="live", tenant_id=tenant_id)
    sp_df         = get_existing_savings_plans(provider=provider, mode="live", tenant_id=tenant_id)
    compute_sp_df = get_compute_savings_plans(provider=provider, mode="live", tenant_id=tenant_id)
    db_sp_df      = get_database_savings_plans(provider=provider, mode="live", tenant_id=tenant_id)
    ri_df         = get_existing_reservations(provider=provider, mode="live", tenant_id=tenant_id)
    wf            = run_waterfall(inv_raw, ri_df, sp_df, simulate_days=days)
    sp_res        = savings_plan_analysis(inv_raw, sp_df, safety_buffer=buffer, eligible_types=list(sp_eligible_types))
    ri_res        = reservation_analysis(inv_raw, ri_df)
    recs          = generate_recommendations(sp_res, ri_res, wf, safety_buffer=buffer)
    return inv_raw, sp_df, compute_sp_df, db_sp_df, ri_df, sp_res, ri_res, recs

if is_live_mode and not is_live_configured:
    # Strict Live Mode with NO Connection: return empty data state
    cols = ["Resource ID", "Resource Name", "Resource Type", "Resource State", "Region", "OS", "SKU", "PAYG Hourly Cost USD", "Avg Daily Running Hours", "Subscription", "Provider", "Is Orphaned"]
    inv_raw = pd.DataFrame(columns=cols)
    sp_df = pd.DataFrame(columns=["commitment_id", "commitment_type", "scope_sku", "scope_region", "scope_os", "hourly_usd_commitment", "reserved_qty", "term", "expiry_date", "provider"])
    compute_sp_df = sp_df.copy()
    db_sp_df = sp_df.copy()
    ri_df = sp_df.copy()
    from analysis.engine import SPAnalysisResult, RIAnalysisResult, WaterfallResult
    sp_result = SPAnalysisResult(0.0, 0.0, 0.0, 0.0, 0.0, safety_buffer, [])
    ri_result = RIAnalysisResult(pd.DataFrame(), pd.DataFrame())
    recs = [{
        "type": "ACTION_REQUIRED", "severity": "HIGH", "icon": "⚠️", "category": "Live Connection",
        "title": f"No Live {selected_provider} Connection Configured",
        "detail": f"You are in **Live Cloud API** mode, but no tenant is connected for {selected_provider}. Please configure your API access on the **🏢 Tenants** page.",
        "action": "Connect a tenant on the Tenants page.",
        "financial_impact_hr": 0.0, "items": [],
    }]
elif is_live_mode and is_live_configured:
    inv_raw, sp_df, compute_sp_df, db_sp_df, ri_df, sp_result, ri_result, recs = load_live_data(
        selected_provider, active_tenant.id, simulate_days, safety_buffer,
        tuple(compute_sp_eligible_types | db_eligible_types),
    )
    if inv_raw.empty:
        recs = [{
            "type": "ACTION_REQUIRED", "severity": "MEDIUM", "icon": "ℹ️", "category": "Live Connection",
            "title": f"No Resources Synced Yet for '{active_tenant.tenant_name}'",
            "detail": "This tenant is connected, but no live inventory has been ingested yet. Go to the **🏢 Tenants** page and run a sync.",
            "action": "Run a manual sync on the Tenants page.",
            "financial_impact_hr": 0.0, "items": [],
        }]
else:
    inv_raw, sp_df, compute_sp_df, db_sp_df, ri_df, sp_result, ri_result, recs = load_benchmark_data(
        simulate_days, safety_buffer, selected_provider,
        tuple(compute_sp_eligible_types | db_eligible_types),
    )

# Real Savings Plan / Reserved Instance commitment pricing cache (Phase A) -
# a lightweight local DB read (the API calls already happened during
# sync/seed), so it's not wrapped in @st.cache_data.
prices_df = get_commitment_prices(get_engine(selected_provider, "live" if is_live_mode else "demo"), provider=selected_provider)

# ─────────────────────────────────────────────────────────────────────────────
# DERIVED SLICES & METRICS
# ─────────────────────────────────────────────────────────────────────────────
if not inv_raw.empty:
    vm_inventory = inv_raw[inv_raw["Resource Type"] == "Compute"].copy()
    db_inventory = inv_raw[inv_raw["Resource Type"].isin(db_eligible_types)].copy()
    # Everything else (Storage, Redis, Synapse, Databricks, ...) - previously
    # silently invisible in the Inventory tab since it matched neither bucket above.
    other_inventory = inv_raw[
        (inv_raw["Resource Type"] != "Compute") & ~inv_raw["Resource Type"].isin(db_eligible_types)
    ].copy()
    # Broader than vm_inventory: everything the Compute Savings Plan pool
    # actually covers per Azure policy (VMs, App Service, Functions Premium,
    # Container Instances, Dedicated Host, ...), not just literal VMs.
    compute_sp_pool_inventory = inv_raw[inv_raw["Resource Type"].isin(compute_sp_eligible_types)].copy()
else:
    vm_inventory = pd.DataFrame(columns=["Resource ID", "Resource Name", "Resource Type", "Resource State", "Region", "OS", "SKU", "PAYG Hourly Cost USD", "Avg Daily Running Hours", "Subscription", "Provider", "Is Orphaned"])
    db_inventory = vm_inventory.copy()
    other_inventory = vm_inventory.copy()
    compute_sp_pool_inventory = vm_inventory.copy()

running_vms      = vm_inventory[vm_inventory["Resource State"] == "Running"] if not vm_inventory.empty else pd.DataFrame()
running_dbs      = db_inventory[db_inventory["Resource State"] == "Running"] if not db_inventory.empty else pd.DataFrame()

total_vm_payg_hr = float(running_vms["PAYG Hourly Cost USD"].sum()) if not running_vms.empty else 0.0
total_db_payg_hr = float(running_dbs["PAYG Hourly Cost USD"].sum()) if not running_dbs.empty else 0.0

compute_sp_commit = float(compute_sp_df["hourly_usd_commitment"].sum()) if not compute_sp_df.empty else 0.0
db_sp_commit      = float(db_sp_df["hourly_usd_commitment"].sum())      if not db_sp_df.empty else 0.0
total_sp_commit   = compute_sp_commit + db_sp_commit
total_ri_commit   = float((ri_df["hourly_usd_commitment"] * ri_df["reserved_qty"]).sum()) if not ri_df.empty else 0.0
orphaned_count    = int(inv_raw["Is Orphaned"].sum()) if not inv_raw.empty else 0
high_recs         = sum(1 for r in recs if r["severity"] == "HIGH")

# Steady-state (24x7) SP-eligible pools - computed once, shared by the
# Savings Plan Analysis and Cost Analysis tabs so both work from the exact
# same slice instead of recomputing the eligibility split twice.
compute_24x7_candidates = compute_sp_pool_inventory[
    (compute_sp_pool_inventory["Resource State"] == "Running") &
    (compute_sp_pool_inventory["Avg Daily Running Hours"] == 24)
]
compute_24x7, compute_sp_excluded = _split_sp_eligible(compute_24x7_candidates, is_azure)
db_running_candidates = db_inventory[db_inventory["Resource State"] == "Running"]
db_running, db_sp_excluded = _split_sp_eligible(db_running_candidates, is_azure)

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
