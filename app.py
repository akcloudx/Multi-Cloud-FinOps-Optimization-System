"""
app.py — Multi-Cloud FinOps Optimization System (Azure & AWS)
Enterprise Cloud Cost & Commitment Engine

Tabs:
  1. Asset Inventory
  2. Savings Plan Analysis
  3. Reserved Instance Coverage
  4. FinOps Recommendations
  5. Cloud Credentials & API Settings
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
from db.schema import init_db
from db.seed import seed_if_empty, COMPUTE_SP_ELIGIBLE_TYPES, DATABASE_SP_ELIGIBLE_TYPES
from db.aws_seed import seed_aws_if_empty, AWS_COMPUTE_SP_TYPES, AWS_DATABASE_SP_TYPES

@st.cache_resource
def init_all_databases():
    try:
        init_db("Azure")
        seed_if_empty()
        init_db("AWS")
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
from ui.auth_page import require_login, render_logout_control
current_user = require_login()

# Imports
from data.inventory_loader import get_compute_inventory
from data.sync_pipeline import run_ingestion_pipeline, get_latest_sync_log
from pricing.retail_pricing import price_inventory, usd, fmt_currency, get_inr_rate
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
# SIDEBAR CONTROLS
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚡ FinOps Engine")
    st.caption("Cloud Cost & Commitment Optimizer")
    st.divider()

    def reset_app_cache():
        st.cache_data.clear()

    # 1. Cloud Provider Switcher
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

    st.divider()

    # 2. Environment Mode Switcher
    env_mode = st.radio(
        "Data Source Environment",
        options=["Demo / Benchmark Mode", "Live Cloud API"],
        index=0,
        key="env_mode_widget",
        on_change=reset_app_cache,
        help="Switch between pre-loaded benchmark infrastructure and live Cloud API connections.",
    )

    st.divider()

    # 3. Parameters
    commitment_term = st.segmented_control(
        "Commitment Term",
        options=["1-Year", "3-Year"],
        default="1-Year",
        help="Applies to Savings Plan for Compute and Reserved Instances.",
    )

    safety_buffer_pct = st.slider(
        "Safety Buffer %",
        min_value=50, max_value=100,
        value=int(DEFAULT_SAFETY_BUFFER * 100),
        step=5,
        help="Recommended SP commitment percentage of steady 24x7 run cost to prevent over-buying.",
    )
    safety_buffer = safety_buffer_pct / 100.0

    simulate_days = st.segmented_control(
        "Analysis Window",
        options=[7, 14, 30],
        default=30,
        help="Historical daily evaluation period.",
    )

    st.divider()

    # 4. Currency toggle
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

    st.divider()
    st.markdown(f"**Platform:** `{selected_provider}`")
    st.markdown(f"**Mode:** `{env_mode}`")
    st.markdown(f"**Currency:** `{selected_currency}`")

    st.divider()
    render_logout_control()

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


# Check Live Credentials — the connected-tenant registry in SQL DB is the single
# source of truth for "is live configured", not the .env file (which only exists
# to pre-fill the connection form / support the cron function outside Streamlit).
active_tenant = get_active_tenant(selected_provider)
is_live_configured = active_tenant is not None
is_live_mode = (env_mode == "Live Cloud API")

@st.cache_data(show_spinner=False)
def load_benchmark_data(days: int, buffer: float, provider: str, sp_eligible_types: tuple):
    inv_raw       = get_compute_inventory(provider=provider)
    sp_df         = get_existing_savings_plans(provider=provider)
    compute_sp_df = get_compute_savings_plans(provider=provider)
    db_sp_df      = get_database_savings_plans(provider=provider)
    ri_df         = get_existing_reservations(provider=provider)
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
    inv_raw       = get_compute_inventory(provider=provider, tenant_id=tenant_id)
    sp_df         = get_existing_savings_plans(provider=provider, tenant_id=tenant_id)
    compute_sp_df = get_compute_savings_plans(provider=provider, tenant_id=tenant_id)
    db_sp_df      = get_database_savings_plans(provider=provider, tenant_id=tenant_id)
    ri_df         = get_existing_reservations(provider=provider, tenant_id=tenant_id)
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
        "type": "ACTION_REQUIRED", "severity": "HIGH", "icon": "⚠️",
        "title": f"No Live {selected_provider} Connection Configured",
        "detail": f"You are in **Live Cloud API** mode, but no tenant is connected for {selected_provider}. Please configure your API access in Tab 5 (⚙️ Settings & Connections).",
        "financial_impact_hr": 0.0
    }]
elif is_live_mode and is_live_configured:
    inv_raw, sp_df, compute_sp_df, db_sp_df, ri_df, sp_result, ri_result, recs = load_live_data(
        selected_provider, active_tenant.id, simulate_days, safety_buffer,
        tuple(compute_sp_eligible_types | db_eligible_types),
    )
    if inv_raw.empty:
        recs = [{
            "type": "ACTION_REQUIRED", "severity": "MEDIUM", "icon": "ℹ️",
            "title": f"No Resources Synced Yet for '{active_tenant.tenant_name}'",
            "detail": "This tenant is connected, but no live inventory has been ingested yet. Go to **⚙️ Settings & Connections** and run a sync.",
            "financial_impact_hr": 0.0,
        }]
else:
    inv_raw, sp_df, compute_sp_df, db_sp_df, ri_df, sp_result, ri_result, recs = load_benchmark_data(
        simulate_days, safety_buffer, selected_provider,
        tuple(compute_sp_eligible_types | db_eligible_types),
    )

# ─────────────────────────────────────────────────────────────────────────────
# DERIVED SLICES & METRICS
# ─────────────────────────────────────────────────────────────────────────────
if not inv_raw.empty:
    vm_inventory = inv_raw[inv_raw["Resource Type"] == "Compute"].copy()
    db_inventory = inv_raw[inv_raw["Resource Type"].isin(db_eligible_types)].copy()
    # Everything else (Storage, Redis, Synapse, Databricks, ...) - previously
    # silently invisible in Tab 1 since it matched neither bucket above.
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

# ─────────────────────────────────────────────────────────────────────────────
# TOP NAVIGATION HEADER
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("## ☁️ Multi-Cloud FinOps Optimization System")
st.caption(f"Enterprise Cost & Commitment Optimization Platform  ·  Provider: **{selected_provider}**  ·  Mode: **{env_mode}**")

if is_live_mode and not is_live_configured:
    st.warning(
        f"⚠️ **Live Mode Active — No Connection Configured for {selected_provider}:** "
        f"Please go to **⚙️ Settings & Connections** tab to configure your {selected_provider} credentials, "
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

# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD TABS
# ─────────────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    f"🔍  {selected_provider} Inventory",
    "💰  Savings Plan Analysis",
    "🏷️  RI Coverage",
    "⚡  Recommendations",
    "⚙️  Settings & Connections",
])


_TYPE_ICONS = {
    "Compute": "🖥️", "Azure SQL Database": "🗄️", "Azure SQL Managed Instance": "🗄️",
    "Azure Database for MySQL": "🐬", "Azure Database for PostgreSQL": "🐘",
    "Azure Cosmos DB": "🌐", "Azure Blob Storage": "📦", "Azure Files": "📁",
    "Azure Cache for Redis": "⚡", "Azure Synapse Analytics": "📊",
    "Azure Databricks": "🧱", "App Service": "🌍", "Azure Disk Storage": "💽",
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


def _render_inventory_section(df: pd.DataFrame, key_prefix: str, show_type_col: bool):
    """Shared renderer for every per-service inventory sub-tab in Tab 1."""
    if df.empty:
        st.caption("No resources in this category.")
        return

    disp = df.copy()
    disp["Status"] = disp.apply(
        lambda r: "Orphaned" if r["Is Orphaned"]
        else ("Running" if r["Resource State"] == "Running" else "Stopped"),
        axis=1,
    )
    disp["PAYG Rate/hr"] = disp["PAYG Hourly Cost USD"].apply(
        lambda x: fmt(x, 4) if x else "Not RI/SP-metered"
    )
    disp["Est. Monthly Cost"] = (
        disp["PAYG Hourly Cost USD"] * disp["Avg Daily Running Hours"] * 30
    ).apply(lambda x: fmt(x, 2) if x else "—")

    # Live mode: show which tenant a subscription ID belongs to, not just the raw GUID.
    if is_live_mode and is_live_configured and active_tenant is not None:
        disp["Subscription"] = disp["Subscription"].apply(
            lambda sid: f"{active_tenant.tenant_name} ({sid})" if sid else active_tenant.tenant_name
        )

    state_opts = disp["Resource State"].unique().tolist()
    region_opts = disp["Region"].unique().tolist()
    f1, f2 = st.columns(2)
    with f1:
        fs = st.multiselect("Power State", state_opts, default=state_opts, key=f"{key_prefix}_state")
    with f2:
        fr = st.multiselect("Region", region_opts, default=region_opts, key=f"{key_prefix}_region")
    active_fs = fs if fs else state_opts
    active_fr = fr if fr else region_opts
    filtered = disp[disp["Resource State"].isin(active_fs) & disp["Region"].isin(active_fr)]

    all_cols = ["Resource ID", "Resource Name"]
    if show_type_col:
        all_cols.append("Resource Type")
    all_cols += ["Status", "Region", "OS", "SKU", "PAYG Rate/hr", "Est. Monthly Cost", "Subscription"]
    all_cols = [c for c in all_cols if c in filtered.columns]
    default_cols = [c for c in all_cols if c != "Resource ID"]  # Resource ID hidden by default - toggle back on if needed

    chosen_cols = st.multiselect("Columns to display", all_cols, default=default_cols, key=f"{key_prefix}_cols")
    if not chosen_cols:
        chosen_cols = default_cols

    st.dataframe(
        filtered[chosen_cols], hide_index=True, width="stretch",
        column_config={
            "Resource Type":     st.column_config.TextColumn("Service"),
            "Status":            st.column_config.TextColumn("Power State"),
            "PAYG Rate/hr":      st.column_config.TextColumn("PAYG Rate/hr"),
            "Est. Monthly Cost": st.column_config.TextColumn("Est. Monthly Cost"),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — ASSET INVENTORY
# ═══════════════════════════════════════════════════════════════════════════════
with tab1:
    st.subheader(f"{selected_provider} Infrastructure Asset Inventory")
    st.caption(
        f"Real-time resource registry across compute and database domains in {selected_provider}."
    )

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
# TAB 2 — SAVINGS PLAN ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.subheader(f"{selected_provider} Savings Plan Analysis")
    st.caption(f"Evaluates active $/hr commitment pools against steady-state baseline spend for {selected_provider}.")

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
                    "What Is Covered": "Azure SQL Database, SQL Managed Instance, Database for PostgreSQL, Database for MySQL, Cosmos DB provisioned throughput",
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
            "**RDS Reserved Instances** in Tab 3.",
            icon="ℹ️"
        )

    if not inv_raw.empty:
        fig_waterfall = get_waterfall_savings_chart(total_vm_payg_hr, total_db_payg_hr, total_sp_commit, total_ri_commit, selected_provider, is_dark=is_dark_theme)
        st.plotly_chart(fig_waterfall, use_container_width=True)

    st.markdown("### A — Compute Savings Plan Pool")

    compute_24x7_candidates = compute_sp_pool_inventory[
        (compute_sp_pool_inventory["Resource State"] == "Running") &
        (compute_sp_pool_inventory["Avg Daily Running Hours"] == 24)
    ]
    compute_24x7, compute_sp_excluded = _split_sp_eligible(compute_24x7_candidates, is_azure)
    compute_baseline_hr = float(compute_24x7["PAYG Hourly Cost USD"].sum())
    compute_recommended = compute_baseline_hr * safety_buffer
    compute_committed   = compute_sp_commit
    compute_leakage     = max(0.0, compute_committed - compute_baseline_hr)
    compute_gap         = max(0.0, compute_baseline_hr - compute_committed)

    with st.container(border=True):
        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("24x7 Compute Run Cost", fmt(compute_baseline_hr) + "/hr")
        cc2.metric("Compute SP Committed",  fmt(compute_committed) + "/hr")
        cc3.metric(f"Recommended ({safety_buffer_pct}%)", fmt(compute_recommended) + "/hr",
                   delta=f"Over by {fmt(compute_leakage)}/hr" if compute_leakage > 0 else (f"Under by {fmt(compute_gap)}/hr" if compute_gap > 0 else "Matched"),
                   delta_color="inverse" if (compute_leakage > 0 or compute_gap > 0) else "off")
        cc4.metric("Est. Monthly Saving", fmt(max(0.0, (compute_baseline_hr - compute_committed) * 730), 2))

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
        elif compute_baseline_hr == 0:
            st.warning(
                f"⚠️ {len(compute_24x7)} SP-eligible resource(s) are running 24x7, but their PAYG hourly "
                "cost shows $0.00 - live retail pricing may not have resolved for these SKUs yet "
                "(re-run a sync in Settings & Connections), or verify actual rates in Azure Cost Management.",
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

    db_running_candidates = db_inventory[db_inventory["Resource State"] == "Running"]
    db_running, db_sp_excluded = _split_sp_eligible(db_running_candidates, is_azure)
    db_baseline_hr  = float(db_running["PAYG Hourly Cost USD"].sum())
    db_recommended  = db_baseline_hr * safety_buffer
    db_committed    = db_sp_commit
    db_leakage      = max(0.0, db_committed - db_baseline_hr)
    db_gap          = max(0.0, db_baseline_hr - db_committed)

    with st.container(border=True):
        dc1, dc2, dc3, dc4 = st.columns(4)
        dc1.metric(f"{db_label} Run Cost",  fmt(db_baseline_hr) + "/hr")
        dc2.metric("Committed Pool",         fmt(db_committed) + "/hr")
        dc3.metric(f"Recommended ({safety_buffer_pct}%)", fmt(db_recommended) + "/hr",
                   delta=f"Over by {fmt(db_leakage)}/hr" if db_leakage > 0 else (f"Under by {fmt(db_gap)}/hr" if db_gap > 0 else "Matched"),
                   delta_color="inverse" if (db_leakage > 0 or db_gap > 0) else "off")
        dc4.metric("Est. Monthly Saving", fmt(max(0.0, (db_baseline_hr - db_committed) * 730), 2))

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
        elif db_baseline_hr == 0:
            st.warning(
                f"⚠️ {len(db_running)} SP-eligible database resource(s) are running, but their PAYG "
                "hourly cost shows $0.00 - live retail pricing may not have resolved for these SKUs yet "
                "(re-run a sync in Settings & Connections), or verify actual rates in Azure Cost Management.",
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
# TAB 3 — RI COVERAGE
# ═══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.subheader(f"{selected_provider} Reserved Instance & Reserved Capacity Coverage")
    st.caption(f"Rigid profile matching (SKU, Region, OS) for active reservation contracts.")

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

    st.markdown("#### Active Reservation Contracts")
    if not ri_df.empty:
        ri_disp = ri_df[["commitment_id", "commitment_type", "scope_sku", "scope_region", "scope_os", "reserved_qty", "hourly_usd_commitment", "term", "expiry_date"]].copy()
        ri_disp["hourly_usd_commitment"] = ri_disp["hourly_usd_commitment"].apply(lambda x: fmt(x, 4) + "/hr each")
        st.dataframe(ri_disp, hide_index=True, width="stretch")
    else:
        st.info("No active Reserved Instance contracts found.")

    st.markdown("#### 📊 Active Reservation & Capacity Coverage Tracker")
    st.caption(
        "Eligibility is checked against real Azure Reservation rules per service/SKU/tier "
        "(e.g. App Service reservations only cover Premium v3/v4 & Isolated v2; Azure SQL "
        "reservations require the vCore Provisioned tier, not Serverless or DTU) - a resource "
        "that Azure doesn't sell reservations for is marked **Not RI-Eligible**, not a coverage gap. "
        "Cosmos DB, SQL DB/MI, Databricks, and Synapse reservations apply as **pooled capacity** "
        "across all matching resources automatically, so their gap is a rough signal, not a literal "
        "purchase instruction. Storage and Files reservations are sold in blocks (100 TB+/10 TiB+) "
        "far larger than any single resource and can't be assessed by resource count at all - marked "
        "**Volume-Based**."
    )
    _POOLED_CAPACITY_NOTE = (
        "Reserved capacity for this service is purchased as pooled capacity/throughput "
        "(RU/s, DBCU, cDWU, or vCore-hours) and Azure applies it automatically across "
        "ALL matching resources in scope - it isn't bought per resource instance, so this "
        "count is a rough signal only. Compare actual usage against your reservation size "
        "in Azure Cost Management before purchasing more."
    )
    _UNMEASURABLE_NOTE = (
        "Reserved capacity for this service is sold in blocks far larger than a single "
        "resource (Storage: 100 TB / 1 PB; Files: 10 TiB / 100 TiB) and applies "
        "automatically across your whole subscription's matching usage, not per resource. "
        "This dashboard tracks resource count, not actual data volume stored, so "
        "per-resource coverage genuinely cannot be assessed here - check total data "
        "volume in Azure Cost Management or Storage metrics before considering a purchase."
    )
    cov = ri_result.coverage_table.copy()
    if not cov.empty:
        def _status(row):
            if not row.get("is_eligible", True):
                return "🚫 Not RI-Eligible"
            if row.get("coverage_model") == "unmeasurable":
                return "📏 Volume-Based (Not Tracked)"
            if row["gap"] > 0:
                if row.get("coverage_model") == "capacity":
                    return f"ℹ️ {int(row['gap'])} Uncovered (pooled)"
                return f"⚠️ Short by {int(row['gap'])}"
            if row["excess"] > 0: return f"ℹ️ {int(row['excess'])} Idle"
            return "✅ Fully Covered"

        def _note(row):
            if not row.get("is_eligible", True):
                return row.get("eligibility_reason", "")
            if row.get("coverage_model") == "unmeasurable":
                return _UNMEASURABLE_NOTE
            if row.get("coverage_model") == "capacity" and (row["gap"] > 0 or row["excess"] > 0):
                return _POOLED_CAPACITY_NOTE
            return ""

        cov["Status"] = cov.apply(_status, axis=1)
        cov["Coverage Note"] = cov.apply(_note, axis=1)
        cov = cov.rename(columns={
            "Resource Type": "Service", "SKU": "SKU / Tier",
            "running_count": "Running", "reserved_qty": "Reserved",
            "gap": "Uncovered Gap", "excess": "Unused Idle",
        })
        display_cols = ["Service", "SKU / Tier", "Region", "OS", "Running", "Reserved",
                         "Uncovered Gap", "Unused Idle", "Status", "Coverage Note"]
        st.dataframe(
            cov[[c for c in display_cols if c in cov.columns]],
            hide_index=True, width="stretch",
            column_config={"Coverage Note": st.column_config.TextColumn("Coverage Note", width="large")},
        )

    if not ri_result.orphaned_ri_drain.empty:
        st.error(f"**{len(ri_result.orphaned_ri_drain)} stopped resource(s) draining active reservations**!")
        st.dataframe(ri_result.orphaned_ri_drain, hide_index=True, width="stretch")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4 — RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════════════════
with tab4:
    st.subheader(f"Actionable FinOps Recommendations ({selected_provider})")
    st.caption(f"Prioritised cost optimization directives generated by the engine.")

    # 1. Summary KPI Bar
    high_count = sum(1 for r in recs if r.get("severity") == "HIGH")
    med_count  = sum(1 for r in recs if r.get("severity") == "MEDIUM")
    total_savings_mo = sum(r.get("financial_impact_hr", 0.0) for r in recs) * 730

    with st.container(border=True):
        rc1, rc2, rc3 = st.columns(3)
        rc1.metric("Critical Priority Actions", f"{high_count} items", delta="Immediate Action Required" if high_count > 0 else "None", delta_color="inverse" if high_count > 0 else "off")
        rc2.metric("Purchase Opportunities", f"{med_count} items", delta="Savings Available" if med_count > 0 else "Optimal", delta_color="normal" if med_count > 0 else "off")
        rc3.metric("Total Monthly Savings Potential", fmt(total_savings_mo, 2) + "/mo", delta="Identified Opportunity")

    if recs:
        fig_recs = get_recommendation_opportunity_chart(recs, is_dark=is_dark_theme)
        st.plotly_chart(fig_recs, use_container_width=True)

    # 2. Structured Recommendation Sub-Tabs
    rec_tab1, rec_tab2, rec_tab3 = st.tabs([
        f"🔴  Critical Directives ({high_count})",
        f"🟡  Purchase Opportunities ({med_count})",
        f"📋  Master Recommendation Table ({len(recs)})"
    ])

    with rec_tab1:
        high_recs = [r for r in recs if r.get("severity") == "HIGH"]
        if high_recs:
            for r in high_recs:
                impact_mo = r.get("financial_impact_hr", 0.0) * 730
                with st.container(border=True):
                    col_t, col_i = st.columns([3, 1])
                    with col_t:
                        st.markdown(f"### {r['icon']} {r['title']}")
                    with col_i:
                        st.markdown(f"#### 💰 Est. Savings: **{fmt(impact_mo, 2)}/mo**")
                    st.markdown(f"**Issue Description:** {r['detail']}")
                    st.markdown("---")
                    st.warning(f"**Recommended Strategy:** Take immediate action to resolve orphaned resource capacity or modify unutilized commitments.")
        else:
            st.success("✅ No critical priority alerts detected for " + selected_provider + ".")

    with rec_tab2:
        med_recs = [r for r in recs if r.get("severity") == "MEDIUM"]
        if med_recs:
            for r in med_recs:
                impact_mo = r.get("financial_impact_hr", 0.0) * 730
                with st.container(border=True):
                    col_t, col_i = st.columns([3, 1])
                    with col_t:
                        st.markdown(f"#### {r['icon']} {r['title']}")
                    with col_i:
                        st.markdown(f"**Saving:** `{fmt(impact_mo, 2)}/mo`")
                    st.caption(r["detail"])
        else:
            st.success("✅ No additional purchase opportunities required.")

    with rec_tab3:
        if recs:
            master_data = []
            for r in recs:
                master_data.append({
                    "Severity": r.get("severity", "LOW"),
                    "Directive Title": r.get("title", ""),
                    "Detail Summary": r.get("detail", ""),
                    "Hourly Impact": fmt(r.get("financial_impact_hr", 0.0), 4) + "/hr",
                    "Est. Monthly Savings": fmt(r.get("financial_impact_hr", 0.0) * 730, 2) + "/mo"
                })
            df_master = pd.DataFrame(master_data)
            st.dataframe(
                df_master,
                hide_index=True,
                width="stretch",
                column_config={
                    "Severity": st.column_config.TextColumn("Priority"),
                    "Directive Title": st.column_config.TextColumn("Recommendation Directive"),
                    "Detail Summary": st.column_config.TextColumn("Analysis Detail"),
                    "Est. Monthly Savings": st.column_config.TextColumn("Monthly Value ($)")
                }
            )


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5 — DYNAMIC SETTINGS & CLOUD CREDENTIALS
# ═══════════════════════════════════════════════════════════════════════════════
with tab5:
    st.subheader(f"⚙️ {selected_provider} Cloud Connection & Credentials")
    st.caption(f"Configure live API integration for {selected_provider}.")

    if is_azure:
        # ── AZURE CONNECTION SETTINGS ──────────────────────────────────────────
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
   - `appId` $\rightarrow$ **Client ID**
   - `password` $\rightarrow$ **Client Secret**
   - `tenant` $\rightarrow$ **Tenant ID**
   - `<YOUR_SUBSCRIPTION_ID>` $\rightarrow$ **Subscription ID**
""")

        env_azure = load_credentials_from_env()

        with st.form("azure_form"):
            col1, col2 = st.columns(2)
            with col1:
                az_name   = st.text_input("Tenant / Subscription Name", value="Prod Azure Tenant", placeholder="e.g. Main Production Azure")
                az_tenant = st.text_input("Tenant ID", value=env_azure.tenant_id if env_azure else "", placeholder="f946c54c-e759-4985-bbe0-3e166cef8fa0")
                az_client = st.text_input("Client ID (Application ID)", value=env_azure.client_id if env_azure else "", placeholder="84644394-8136-4625-9d36-564fc9c0b5e7")
            with col2:
                az_sub = st.text_input("Subscription ID", value=env_azure.subscription_id if env_azure else "", placeholder="e0b96fd6-b891-4a5f-80db-6cfed14e62ab")
                az_sec = st.text_input("Client Secret", value=env_azure.client_secret if env_azure else "", type="password")

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
                    # Upsert this Service Principal into the cloud_tenants registry
                    # (SQL DB) - updates the existing row if this exact tenant was
                    # already added, otherwise adds a new one. Becomes the active
                    # tenant either way.
                    tenant_db_id = upsert_tenant(
                        provider="Azure",
                        tenant_name=az_name or "Azure Tenant",
                        tenant_id=az_tenant,
                        subscription_id=az_sub,
                        client_id=az_client,
                        client_secret=az_sec,
                    )

                    # Run Live Ingestion via Azure Resource Graph API, scoped to this tenant
                    res = run_ingestion_pipeline("Azure", creds=new_az, tenant_db_id=tenant_db_id)
                    if res["status"] == "SUCCESS":
                        st.success(f"🎉 **Live Tenant Ingestion Complete!** {res['message']}")
                        st.rerun()
                    else:
                        st.error(f"❌ Connection or Ingestion Error: {res['message']}")
            else:
                st.error("Please fill in all Azure credential fields.")

        # ── Connected Tenants Registry (SQL DB) ────────────────────────────────
        st.divider()
        st.markdown("#### 🗂️ Connected Azure Tenants")
        azure_tenants = list_tenants("Azure")
        if not azure_tenants:
            st.caption("No tenants connected yet. Fill in the form above and click **Connect, Save & Ingest**.")
        else:
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
                        if b1.button("Activate", key=f"activate_az_{t.id}", use_container_width=True):
                            set_active_tenant("Azure", t.id)
                            st.rerun()
                    if b2.button("🗑️", key=f"delete_az_{t.id}", use_container_width=True, help="Remove this tenant and its synced data"):
                        delete_tenant("Azure", t.id)
                        st.rerun()
            st.caption("🟢 = active tenant shown in Live Cloud API mode. Only one tenant is active at a time.")

        st.markdown("#### Required Azure RBAC Roles")
        st.dataframe(pd.DataFrame(REQUIRED_ROLES)[["Role Name", "Scope", "Purpose"]], hide_index=True, width="stretch")

    else:
        # ── AWS CONNECTION SETTINGS ───────────────────────────────────────────
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
                st.success("✅ AWS credentials saved to Settings & Connections registry.")
                st.info("ℹ️ Live AWS inventory fetch (EC2/RDS via boto3) isn't implemented yet — this account is registered, but Live mode will show no resources until that's built.")
                st.rerun()
            else:
                st.error("Please fill in Access Key ID and Secret Access Key.")

        st.markdown("#### Required AWS IAM Permissions")
        st.dataframe(pd.DataFrame(REQUIRED_AWS_POLICIES), hide_index=True, width="stretch")

        st.divider()
        st.markdown("#### 🗂️ Connected AWS Accounts")
        aws_tenants = list_tenants("AWS")
        if not aws_tenants:
            st.caption("No AWS accounts connected yet.")
        else:
            for t in aws_tenants:
                cols = st.columns([4, 3, 2])
                cols[0].markdown(f"{'🟢' if t.is_active else '⚪'} **{t.tenant_name}**")
                cols[1].caption(f"Key: `{t.client_id[:6]}…` · Added {t.created_at[:10]}")
                with cols[2]:
                    if not t.is_active and st.button("Activate", key=f"activate_aws_{t.id}", use_container_width=True):
                        set_active_tenant("AWS", t.id)
                        st.rerun()

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
                st.success(f"✅ {res['message']}")
                st.rerun()
            else:
                st.error(f"❌ {res['message']}")

    # ── DASHBOARD USER ACCOUNTS ─────────────────────────────────────────────
    # Login to this app itself, not cloud credentials - one shared account
    # list for everyone. Placeholder ahead of real Entra ID / OIDC login.
    st.divider()
    st.markdown("### 👥 Dashboard User Accounts")
    st.caption("Accounts that can sign in to this dashboard. Shared across everyone - not tied to a cloud tenant.")

    from db.users import list_users, create_user as _create_user

    for u in list_users():
        ucols = st.columns([3, 3, 2])
        ucols[0].markdown(f"**{u.display_name or u.username}** (`{u.username}`)")
        ucols[1].caption(f"Added {u.created_at[:10]} · Last login: {u.last_login_at[:10] if u.last_login_at else 'never'}")
        ucols[2].caption("🟢 Active" if u.is_active else "⚪ Inactive")

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
                    _create_user(nu_username, nu_pw1, nu_display)
                    st.success(f"User '{nu_username}' added.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL ENTERPRISE SAAS FOOTER
# ─────────────────────────────────────────────────────────────────────────────
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
