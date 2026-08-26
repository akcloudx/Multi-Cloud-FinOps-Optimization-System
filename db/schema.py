"""
db/schema.py
Star Schema for Multi-Cloud FinOps Optimization System.
Compatible with SQLite and Microsoft Azure SQL Database.

Demo and live data are genuinely separate stores (2026-08 redesign) - every
table lives in one of 4 scopes, keyed by (provider, mode): azure_demo,
azure_live, aws_demo, aws_live. This replaced the old design where demo vs
live was just a CloudInventory.tenant_id NULL-vs-set flag inside one shared
table - that made it impossible to point a user at "the real Azure SQL
Database" and have them see a physically distinct set of tables for demo
data, which is exactly what was being verified when this redesign happened.

In production (real Azure SQL Database), the 4 scopes are 4 SQL Server
SCHEMAS inside the ONE `finops-db` database - schemas are free, so this adds
zero cost over the single-schema design, while still being real structural
separation (a `SELECT * FROM azure_demo.cloud_inventory` and a
`SELECT * FROM azure_live.cloud_inventory` are genuinely different tables,
not the same table filtered by a column). Locally (SQLite doesn't support
real schemas the same way), each scope is a separate .db file instead.
"""

import os
import time
from sqlalchemy import (
    create_engine, Column, String, Float, Integer, Boolean, Text, inspect, text
)
from sqlalchemy.orm import declarative_base

_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))

Base = declarative_base()
_VALID_MODES = {"demo", "live"}

# Raw connection to the physical SQL Server, ONE per provider, shared across
# both modes (demo and live are the SAME server/database in production, just
# different schemas) - never call this engine directly for table reads/writes,
# always go through get_engine(provider, mode) below.
_base_mssql_engines = {}
# Fully mode-scoped engines actually used everywhere else, keyed by
# (PROVIDER, mode). For mssql, these are schema_translate_map proxies over
# the shared base engine above. For sqlite, these are their own standalone
# per-file engines (no sharing possible/needed).
_mode_engines = {}


def _schema_name(provider_key: str, mode: str) -> str:
    return f"{provider_key.lower()}_{mode}"


def _sqlite_path(provider_key: str, mode: str) -> str:
    return os.path.join(_PROJECT_ROOT, f"{provider_key.lower()}_finops_{mode}.db")


def get_engine(provider: str = "Azure", mode: str = "demo"):
    """Return a singleton SQLAlchemy engine scoped to BOTH the cloud provider
    (Azure/AWS) and the environment mode (demo/live). See this module's
    docstring for why these are genuinely separate stores, not a shared-table
    flag. `mode` has no default that quietly does the wrong thing on purpose -
    every caller must say which one it means; a typo raises immediately
    rather than silently mixing demo and live data."""
    global _base_mssql_engines, _mode_engines
    mode = (mode or "").lower()
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be 'demo' or 'live', got {mode!r}")
    provider_key = provider.upper()
    cache_key = (provider_key, mode)
    if cache_key in _mode_engines:
        return _mode_engines[cache_key]

    db_url = os.getenv("DATABASE_URL")
    is_azure_env = bool(os.getenv("WEBSITE_SITE_NAME") or os.getenv("FUNCTIONS_WORKER_RUNTIME"))

    # Managed Identity path (preferred, tried 2026-08) - genuinely
    # passwordless: no secret exists anywhere for this app to leak, unlike
    # DATABASE_URL below. Used when AZURE_SQL_SERVER/AZURE_SQL_DATABASE are
    # set - these are plain identifiers, not credentials, safe to sit in App
    # Settings in the clear. Authenticates via this App Service/Function
    # App's own System-Assigned Managed Identity against Microsoft Entra -
    # requires that identity to be granted DB access first (CREATE USER ...
    # FROM EXTERNAL PROVIDER + role membership, one-time setup - see
    # azure_deploy/deploy_all_resources.ps1). ActiveDirectoryMsi (not
    # ActiveDirectoryDefault) is used deliberately: mssql-python's own docs
    # note ActiveDirectoryDefault walks its whole credential-provider chain
    # before finding the managed identity, which is unnecessary latency here
    # since Managed Identity is the ONLY credential source in this
    # environment - specifying it directly skips the chain-walk.
    #
    # Uses the mssql-python driver (mssql+mssqlpython:// dialect) rather than
    # pyodbc specifically because this app's real deployment target is
    # Oryx-built native Linux App Service/Function App (NOT a custom
    # container - confirmed via deploy_all_resources.ps1's `az webapp create
    # --runtime "PYTHON:3.12"`), which does not ship the Microsoft ODBC
    # Driver (msodbcsql18) pyodbc needs, and the Function App's Consumption
    # (Y1) plan has no shell access to install it. mssql-python bundles its
    # own driver as a pip dependency (mssql-python-odbc) - no OS-level
    # install needed on either.
    #
    # KNOWN RISK, disclosed not hidden: SQLAlchemy's mssql-python dialect
    # requires SQLAlchemy 2.1.0b2+, a PRE-RELEASE - Microsoft's own docs
    # explicitly say not to use it "for production systems with strict
    # stability requirements." Tried here anyway per an explicit choice to
    # accept that risk (a `pre-managed-identity-backup` git branch holds the
    # last known-good password-based state - see db/schema.py's DATABASE_URL
    # path below - if this needs to be reverted).
    if not db_url and is_azure_env:
        sql_server = os.getenv("AZURE_SQL_SERVER")
        sql_database = os.getenv("AZURE_SQL_DATABASE")
        if sql_server and sql_database:
            db_url = (
                f"mssql+mssqlpython://@{sql_server}/{sql_database}"
                "?authentication=ActiveDirectoryMsi&encrypt=yes"
            )

    # In Azure production environment (App Service / Function App), a real
    # connection MUST come from one of the two paths above - this used to
    # silently fall back to a hardcoded Azure SQL admin username/password (a
    # real credential, committed to source control) if DATABASE_URL was
    # missing. Found live 2026-08 (also duplicated in
    # azure_deploy/seed_azure_sql.py and deploy_all_resources.ps1, since
    # fixed too) - failing loudly here instead is deliberate: silently
    # falling through to local SQLite in an Azure environment would look
    # like it worked while actually writing to a non-persistent local file,
    # which is worse than a clear startup error.
    if not db_url and is_azure_env:
        raise RuntimeError(
            "No database connection configured. This app is running in an Azure App Service / "
            "Function App (detected via WEBSITE_SITE_NAME/FUNCTIONS_WORKER_RUNTIME) but has neither "
            "DATABASE_URL (password-based) nor AZURE_SQL_SERVER + AZURE_SQL_DATABASE (Managed Identity, "
            "passwordless, preferred) set. For Managed Identity: set AZURE_SQL_SERVER="
            "'<server>.database.windows.net' and AZURE_SQL_DATABASE='<database>' as App Settings, after "
            "enabling this app's System-Assigned Managed Identity and granting it database access - see "
            "azure_deploy/deploy_all_resources.ps1. For password auth instead: set DATABASE_URL, e.g. "
            "'mssql+pymssql://<user>:<password>@<server>.database.windows.net:1433/<database>'. "
            "Never hardcode credentials in source code."
        )

    if db_url and "mssql" in db_url:
        if "mssqlpython" not in db_url and "pymssql" not in db_url and "pyodbc" not in db_url:
            db_url = db_url.replace("mssql://", "mssql+pymssql://")
        # mssql-python's `timeout` is a real top-level connect() parameter
        # (verified against the installed package's own source, 2026-08) -
        # genuinely different from its documented-but-broken "Connection
        # Timeout" connection-STRING keyword (microsoft/mssql-python#339).
        # Set generously (60s, well beyond the default that was hitting
        # "TCP Provider: Timeout error [258]" on a cold-starting F1 Free
        # tier instance authenticating via Managed Identity - token
        # acquisition + TLS + TCP handshake all have to complete within it).
        engine_kwargs = {"echo": False, "future": True, "pool_pre_ping": True}
        if "mssqlpython" in db_url:
            engine_kwargs["connect_args"] = {"timeout": 60}
        try:
            if provider_key not in _base_mssql_engines:
                base_engine = create_engine(db_url, **engine_kwargs)
                # Retry the first real connection a few times with backoff -
                # a cold F1 instance's first outbound TCP attempt to Azure
                # SQL has genuinely been observed to time out while a
                # follow-up attempt (warm DNS/TLS/IMDS token cache) succeeds
                # moments later. Only wraps this initial probe, not every
                # query - pool_pre_ping above already handles later
                # recycling of connections that go stale mid-session.
                last_error = None
                for attempt in range(1, 4):
                    try:
                        with base_engine.connect() as conn:
                            pass
                        last_error = None
                        break
                    except Exception as retry_ex:
                        last_error = retry_ex
                        if attempt < 3:
                            print(f"[Info] Azure SQL connection attempt {attempt}/3 failed ({retry_ex}) - retrying in {attempt * 3}s...")
                            time.sleep(attempt * 3)
                if last_error is not None:
                    raise last_error
                _base_mssql_engines[provider_key] = base_engine
                print(f"[Info] Successfully connected to Production Azure SQL Database for {provider_key}")
            schema_name = _schema_name(provider_key, mode)
            engine = _base_mssql_engines[provider_key].execution_options(schema_translate_map={None: schema_name})
            _mode_engines[cache_key] = engine
            return engine
        except Exception as ex:
            print(f"[Warning] Azure SQL Database connection attempt failed ({ex}).")
            if is_azure_env:
                # A configured mssql connection that genuinely fails in
                # Azure must NEVER silently fall through to local SQLite
                # below - that file is ephemeral (wiped on every
                # restart/redeploy) and would make a real, broken
                # production database connection look like a working app.
                # This exact silent-fallback masked the mssql-python
                # timeout failures for a while (2026-08) - raising here
                # instead surfaces it immediately as a visible Streamlit
                # error, not a quiet wrong answer. Local dev (is_azure_env
                # False) is unaffected - SQLite is the CORRECT choice there.
                raise

    # Fallback to local SQLite ONLY for offline local desktop development
    db_path = _sqlite_path(provider_key, mode)
    db_url = f"sqlite:///{db_path}"
    _mode_engines[cache_key] = create_engine(db_url, echo=False, future=True)
    return _mode_engines[cache_key]


def _ensure_schema_exists(engine, schema_name: str):
    """SQL Server requires a schema to exist before CREATE TABLE can target
    it (unlike the default 'dbo' schema, which always exists) - a no-op for
    SQLite, which has no equivalent concept (each mode is already its own
    separate file there)."""
    if engine.dialect.name != "mssql":
        return
    with engine.connect() as conn:
        raw = conn.execution_options(schema_translate_map=None)
        exists = raw.execute(text("SELECT 1 FROM sys.schemas WHERE name = :n"), {"n": schema_name}).first()
        if not exists:
            raw.execute(text(f"CREATE SCHEMA [{schema_name}]"))
            raw.commit()


def _ensure_column(engine, schema_name, table_name: str, column_name: str, column_type_sql: str):
    """Add a column to an already-existing table if missing (SQLAlchemy's create_all
    only creates missing tables, never alters existing ones). No 'COLUMN' keyword in
    the SQL - SQLite accepts it but SQL Server's ALTER TABLE ADD syntax does not.
    schema_name is only meaningful for mssql - reflection (unlike schema_translate_map)
    doesn't automatically scope to the translated schema, so it must be passed
    explicitly here, and the raw ALTER TABLE text must be schema-qualified by hand
    too (schema_translate_map only rewrites compiled Table/Column objects, never
    hand-written SQL strings)."""
    is_mssql = engine.dialect.name == "mssql"
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names(schema=schema_name) if is_mssql else inspector.get_table_names()
    if table_name not in existing_tables:
        return
    existing_cols = {c["name"] for c in (inspector.get_columns(table_name, schema=schema_name) if is_mssql else inspector.get_columns(table_name))}
    if column_name not in existing_cols:
        qualified = f"[{schema_name}].{table_name}" if is_mssql else table_name
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {qualified} ADD {column_name} {column_type_sql}"))


def _widen_column(engine, schema_name, table_name: str, column_name: str, min_length: int):
    """Widens an already-existing VARCHAR column that turned out too narrow
    for real data - unlike _ensure_column (add-if-missing), this handles a
    column that exists but is undersized. SQL Server ENFORCES VARCHAR length
    and raises on overflow rather than silently truncating like SQLite does,
    so a too-narrow column breaks the write outright in production while
    looking fine in local/demo testing (real incident, 2026-08: missing_role
    started also carrying full plain-language auth-error text, not just a
    short role-name list, and blew past its original VARCHAR(255)).
    No-op on SQLite (doesn't enforce VARCHAR length, nothing to fix) and
    no-op once already wide enough, so this is cheap to call every startup."""
    if engine.dialect.name != "mssql":
        return
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names(schema=schema_name):
        return
    cols = {c["name"]: c for c in inspector.get_columns(table_name, schema=schema_name)}
    col = cols.get(column_name)
    if col is None:
        return
    current_length = getattr(col["type"], "length", None)
    if current_length is not None and current_length >= min_length:
        return
    qualified = f"[{schema_name}].{table_name}"
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {qualified} ALTER COLUMN {column_name} VARCHAR({min_length})"))


def init_db(provider: str = "Azure", mode: str = "demo"):
    """Create all tables for the specified (provider, mode) scope if they
    don't exist yet, and migrate any columns added after a table already
    existed in the wild."""
    engine = get_engine(provider, mode)
    schema_name = _schema_name(provider.upper(), mode)
    _ensure_schema_exists(engine, schema_name)
    Base.metadata.create_all(engine)
    _ensure_column(engine, schema_name, "cloud_inventory", "tenant_id", "INTEGER")
    _ensure_column(engine, schema_name, "commitments", "tenant_id", "INTEGER")
    _ensure_column(engine, schema_name, "commitment_price_cache", "resource_type", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "cloud_inventory", "redundancy", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "commitment_price_cache", "redundancy", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "commitments", "scope_redundancy", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "commitments", "scope_resource_type", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "cloud_inventory", "ha_replica_count", "INTEGER")
    _ensure_column(engine, schema_name, "cloud_tenants", "domain", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "cloud_tenants", "last_synced_at", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "cloud_tenants", "tenant_permission_status", "VARCHAR(50)")
    _ensure_column(engine, schema_name, "cloud_tenants", "tenant_missing_roles", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "commitments", "is_inferred_mapping", "BOOLEAN")
    _ensure_column(engine, schema_name, "commitments", "mapping_note", "VARCHAR(500)")
    _ensure_column(engine, schema_name, "cloud_tenants", "sync_interval_hours", "INTEGER")
    _ensure_column(engine, schema_name, "cloud_tenants", "last_sync_status", "VARCHAR(20)")
    _ensure_column(engine, schema_name, "cloud_tenants", "last_sync_message", "VARCHAR(500)")
    _ensure_column(engine, schema_name, "cloud_tenants", "tenant_assigned_roles", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "tenant_subscriptions", "assigned_roles", "VARCHAR(255)")
    _widen_column(engine, schema_name, "cloud_tenants", "tenant_missing_roles", 1000)
    _widen_column(engine, schema_name, "tenant_subscriptions", "missing_role", 1000)
    _widen_column(engine, schema_name, "aws_reservation_purchases", "service", 20)
    _ensure_column(engine, schema_name, "cloud_tenants", "aws_account_id", "VARCHAR(50)")
    _ensure_column(engine, schema_name, "commitments", "offering_class", "VARCHAR(50)")
    # Same collision risk CommitmentPriceCache's resource_type/redundancy
    # columns were added for (see that class's comments): without these,
    # an RDS Multi-AZ and Single-AZ instance of the identical instanceType/
    # region/OS would share one PAYG cache row despite Multi-AZ genuinely
    # costing roughly 2x - a real, well-known price difference, not a
    # hypothetical edge case. NULL for every existing Azure row (unused
    # there - VM PAYG pricing doesn't vary by redundancy the way RDS
    # Multi-AZ does) and for AWS EC2 rows (redundancy is N/A there too).
    _ensure_column(engine, schema_name, "retail_prices", "resource_type", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "retail_prices", "redundancy", "VARCHAR(255)")
    # Real Reservation/Savings Plan scope restriction (Single subscription /
    # Single resource group) - added 2026-08-23, see Commitment.scope_subscription_id's
    # own comment for why this was a real, not hypothetical, coverage-matching gap.
    _ensure_column(engine, schema_name, "cloud_inventory", "resource_group", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "commitments", "scope_subscription_id", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "commitments", "scope_resource_group_id", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "reservation_purchases", "applied_scope_resource_group_id", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "savings_plan_purchases", "applied_scope_subscription_id", "VARCHAR(255)")
    _ensure_column(engine, schema_name, "savings_plan_purchases", "applied_scope_resource_group_id", "VARCHAR(255)")
    # AWS EC2 "Zonal" Reserved Instance scope + account traceability - added
    # 2026-08-23, see Commitment.scope_availability_zone's own comment.
    _ensure_column(engine, schema_name, "cloud_inventory", "availability_zone", "VARCHAR(100)")
    _ensure_column(engine, schema_name, "commitments", "scope_availability_zone", "VARCHAR(100)")
    _ensure_column(engine, schema_name, "aws_reservation_purchases", "account_id", "VARCHAR(50)")
    _ensure_column(engine, schema_name, "aws_savings_plan_purchases", "account_id", "VARCHAR(50)")
    # VM Rightsizing - see CloudInventory's own comment on these 4, and
    # CloudTenant's on the 7 settings columns below.
    _ensure_column(engine, schema_name, "cloud_inventory", "avg_cpu_percent", "FLOAT")
    _ensure_column(engine, schema_name, "cloud_inventory", "p95_cpu_percent", "FLOAT")
    _ensure_column(engine, schema_name, "cloud_inventory", "avg_memory_percent", "FLOAT")
    _ensure_column(engine, schema_name, "cloud_inventory", "p95_memory_percent", "FLOAT")
    _ensure_column(engine, schema_name, "cloud_tenants", "rightsizing_percentile", "INTEGER")
    _ensure_column(engine, schema_name, "cloud_tenants", "rightsizing_cpu_under_pct", "FLOAT")
    _ensure_column(engine, schema_name, "cloud_tenants", "rightsizing_cpu_over_pct", "FLOAT")
    _ensure_column(engine, schema_name, "cloud_tenants", "rightsizing_mem_under_pct", "FLOAT")
    _ensure_column(engine, schema_name, "cloud_tenants", "rightsizing_mem_available_pct", "FLOAT")
    _ensure_column(engine, schema_name, "cloud_tenants", "rightsizing_lookback_days", "INTEGER")
    _ensure_column(engine, schema_name, "cloud_tenants", "rightsizing_headroom_pct", "FLOAT")
    _ensure_column(engine, schema_name, "cloud_tenants", "rightsizing_min_days", "INTEGER")
    return engine


# ── ORM Table Definitions (Explicit String lengths for SQL Server compatibility) ──

class CloudInventory(Base):
    __tablename__ = "cloud_inventory"

    resource_id              = Column(String(500), primary_key=True)
    resource_name            = Column(String(255), nullable=False)
    resource_type            = Column(String(255), nullable=False)
    resource_state           = Column(String(255), nullable=False)
    region                   = Column(String(255), nullable=False)
    os                       = Column(String(255), nullable=False)
    sku                      = Column(String(255), nullable=False)
    # "Zone Redundant" | "Locally Redundant" | "N/A" (services with no
    # redundancy-driven price variant). Verified live (2026-08): a
    # Zone-Redundant SQL DB/Elastic Pool meter is a genuinely DIFFERENT (and
    # often cheaper) price than the standard variant, not a small surcharge -
    # so this can't be inferred or defaulted, it must reflect the resource's
    # real ARM properties.zoneRedundant setting or pricing would silently use
    # the wrong baseline for a real zone-redundant resource.
    redundancy                = Column(String(255), nullable=True, default="N/A")
    # Number of Hyperscale High Availability secondary replicas (real ARM
    # property Microsoft.Sql/servers/databases.properties.
    # highAvailabilityReplicaCount, int 0-4). Verified live 2026-08 against
    # the real Azure pricing calculator: each replica is billed at the SAME
    # per-vCore rate as the primary Compute meter (confirmed identical $, no
    # distinct Retail API "replica" meter exists) - not a small surcharge,
    # a genuine 2x-or-more cost multiplier for a Hyperscale database with 1+
    # replicas. This column is captured for Business Critical too (the same
    # ARM property also applies there), but ONLY Hyperscale's billing
    # relationship for this property was verified this session - see
    # pricing/commitment_pricing.py, which deliberately does NOT multiply
    # by this for Business Critical, to avoid guessing an unverified rule
    # (Business Critical's base price already includes built-in HA
    # architecture; whether/how extra replicas cost more wasn't checked).
    ha_replica_count           = Column(Integer, nullable=True, default=0)
    payg_hourly_usd          = Column(Float, nullable=False)
    avg_daily_running_hours  = Column(Integer, nullable=False)
    subscription             = Column(String(255), nullable=True)
    # Real Resource Graph 'resourceGroup' value (bare name, e.g. "rg-prod").
    # Added 2026-08-23 alongside Commitment.scope_resource_group_id - needed
    # to match a Reservation/Savings Plan purchased with "Single resource
    # group" scope (a real, distinct Azure scoping option - see
    # pricing/commitment_mapping.py) against the specific resources it
    # actually covers, not just subscription/region/SKU.
    resource_group            = Column(String(255), nullable=True)
    # Real EC2 Placement.AvailabilityZone (e.g. "us-east-1a") - added
    # 2026-08-23, AWS-only (always NULL for Azure rows). Needed to match a
    # "Zonal" EC2 Reserved Instance (scope="Availability Zone", confirmed
    # real via boto3's DescribeReservedInstances model - a genuinely
    # tighter restriction than "Regional" scope RIs, which cover the whole
    # region) against the specific instances it actually covers - see
    # Commitment.scope_availability_zone and pricing/aws_commitment_mapping.py.
    availability_zone         = Column(String(100), nullable=True)
    # VM Rightsizing (Azure only for now) - real Azure Monitor Metrics,
    # not derived/estimated. NULL for every non-VM resource type, and for
    # a VM whose metrics haven't been fetched yet (a not-yet-synced live
    # tenant, or a demo VM this feature's seed data deliberately left
    # unset). avg/p95_memory_percent store *available* (free) memory, the
    # real name of Azure's own metric (`Available Memory Percentage`) -
    # deliberately NOT inverted to "used %" here, since the one place
    # that inversion actually matters is the classification threshold,
    # not storage - see analysis/rightsizing.py's own comment on this.
    # Independently nullable from CPU: a VM can have Percentage CPU
    # (host-level, no agent needed) with memory still NULL (needs the
    # Azure Monitor Agent, not confirmed present on every VM/region).
    avg_cpu_percent            = Column(Float, nullable=True)
    p95_cpu_percent            = Column(Float, nullable=True)
    avg_memory_percent         = Column(Float, nullable=True)
    p95_memory_percent         = Column(Float, nullable=True)
    provider                 = Column(String(255), default="Azure")
    is_orphaned              = Column(Boolean, default=False)
    # NULL = demo/seed data. Non-NULL = live-ingested, scoped to that cloud_tenants.id.
    tenant_id                = Column(Integer, nullable=True)


class Commitment(Base):
    __tablename__ = "commitments"

    commitment_id           = Column(String(500), primary_key=True)
    commitment_type         = Column(String(255), nullable=False)
    scope_sku               = Column(String(255), nullable=False)
    # Which Resource Type this reservation applies to. Added 2026-08 -
    # discovered live (via the redundancy work below) that scope_sku ALONE
    # is not enough to identify a reservation's real target: multiple
    # services share identical SKU strings (e.g. "GP_Gen5_4" is used by
    # Azure SQL Database, PostgreSQL Flexible Server, AND MySQL Flexible
    # Server), and analysis/engine.py's coverage matching used to INFER
    # Resource Type by looking up scope_sku in inventory - which silently
    # picked an arbitrary, possibly wrong service when the SKU collided
    # across services, misattributing a reservation's coverage to the wrong
    # resource type entirely. Explicit and required now, not inferred.
    scope_resource_type     = Column(String(255), nullable=True)
    scope_region            = Column(String(255), nullable=False)
    scope_os                = Column(String(255), nullable=True)
    # "Zone Redundant" | "Locally Redundant" | "N/A" - which redundancy
    # configuration this specific reservation applies to. Added 2026-08
    # alongside CloudInventory.redundancy: a reservation scoped to Standard
    # pricing does not automatically cover a Zone-Redundant resource of the
    # same SKU/region/OS (and vice versa) - they're genuinely different
    # priced meters (see pricing/commitment_pricing.py's
    # _filter_by_redundancy), so gap/coverage matching must account for it
    # too, not just the pricing lookup.
    scope_redundancy        = Column(String(255), nullable=True, default="N/A")
    hourly_usd_commitment   = Column(Float, nullable=False)
    reserved_qty            = Column(Integer, default=0)
    term                    = Column(String(255), default="1-year")
    expiry_date             = Column(String(255), nullable=True)
    provider                = Column(String(255), default="Azure")
    # NULL = demo/seed data. Non-NULL = live-ingested, scoped to that cloud_tenants.id.
    tenant_id               = Column(Integer, nullable=True)
    # True only for rows derived (pricing/commitment_mapping.py) from a real
    # live tenant's ReservationPurchase/SavingsPlanPurchase where Azure's own
    # API genuinely can't disambiguate the target (e.g. SQL Database vs SQL
    # Elastic Pool reservations share an identical purchase-record shape) -
    # best-effort defaulted rather than dropped, but flagged so the UI can
    # show a caveat instead of presenting a guess as fact. Always False for
    # demo/seed rows and every unambiguous real mapping.
    is_inferred_mapping     = Column(Boolean, default=False)
    mapping_note            = Column(String(500), nullable=True)
    # Real Azure scope restriction, carried through from ReservationPurchase/
    # SavingsPlanPurchase's applied_scope_subscription_id/
    # applied_scope_resource_group_id (see pricing/commitment_mapping.py).
    # NULL means unrestricted (Shared scope, or demo/seed data) - matches
    # tenant-wide, the ONLY behavior this app had before 2026-08-23. A
    # non-NULL value means this commitment was purchased with "Single
    # subscription" or "Single resource group" scope and Azure itself will
    # only ever apply its discount to resources inside that scope - real
    # Reservations/Savings Plans default to Single-subscription scope unless
    # the buyer deliberately picks Shared, so this was a real, not
    # hypothetical, gap: analysis/engine.py's coverage matching used to
    # ignore scope entirely and match ANY tenant resource with the right
    # SKU/region, overstating coverage for every Single-scoped commitment in
    # a multi-subscription tenant. Bare values (not fully-qualified ARM IDs)
    # to match CloudInventory.subscription/resource_group's own format -
    # normalized on the way in, see commitment_mapping.py's _bare_subscription_id/_bare_resource_group.
    scope_subscription_id   = Column(String(255), nullable=True)
    scope_resource_group_id = Column(String(255), nullable=True)
    # Real EC2 Reserved Instance "Zonal" scope (AvailabilityZone, e.g.
    # "us-east-1a") - added 2026-08-23, AWS-only (always NULL for Azure
    # rows and for AWS "Regional"-scope RIs, which cover the whole region -
    # confirmed via boto3's DescribeReservedInstances Scope field:
    # "Availability Zone" | "Region"). A DIFFERENT restriction axis than
    # scope_subscription_id/scope_resource_group_id above - AZ nests under
    # Region, not under Account, and AWS RI/SP account-level sharing
    # deliberately is NOT modeled as a matching restriction at all (see
    # pricing/aws_commitment_mapping.py's module docstring: unlike Azure,
    # AWS RIs/Savings Plans share unused discount across an Organization's
    # linked accounts BY DEFAULT when consolidated billing is active - this
    # app has no organizations/ram API access to detect the edge cases
    # where that sharing is disabled or restricted via Group Sharing, so
    # account-level scope is deliberately left unrestricted/tenant-wide
    # rather than guessed).
    scope_availability_zone = Column(String(100), nullable=True)
    # "standard" | "convertible" | None. EC2 Reserved Instances only - AWS's
    # own docs confirm RDS/ElastiCache/Redshift Reservations have no such
    # offering-class split, and Azure Reservations don't either. Doesn't
    # affect coverage matching (AWS's own docs: "The offering class ... does
    # not affect how the billing discount is applied") - display-only, so a
    # user can see which of their EC2 RIs are exchangeable (Convertible) vs
    # not (Standard) without needing to affect any pricing/matching logic.
    offering_class          = Column(String(50), nullable=True)


class ReconciliationLog(Base):
    __tablename__ = "reconciliation_log"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    date                = Column(String(255), nullable=False)
    hour                = Column(String(255), nullable=False)
    resource_id         = Column(String(500), nullable=False)
    sku                 = Column(String(255), nullable=False)
    payg_rate_per_hour  = Column(Float, nullable=False)
    covered_by_ri       = Column(Float, default=0.0)
    covered_by_sp       = Column(Float, default=0.0)
    final_payg_overage  = Column(Float, default=0.0)
    ri_leakage          = Column(Float, default=0.0)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    recommendation_type = Column(String(255), nullable=False)
    severity            = Column(String(255), nullable=False)
    resource_id         = Column(String(500), nullable=True)
    commitment_id       = Column(String(500), nullable=True)
    message             = Column(Text, nullable=False)
    action              = Column(Text, nullable=False)
    financial_impact_hr = Column(Float, default=0.0)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    synced_at      = Column(String(255), nullable=False)
    provider       = Column(String(255), nullable=False)
    status         = Column(String(255), nullable=False)
    records_synced = Column(Integer, default=0)
    source         = Column(String(255), default="Azure Function (24h Cron)")


class RetailPrice(Base):
    """
    Cache table storing live rates fetched from Azure Retail Prices API & AWS Pricing API.
    """
    __tablename__ = "retail_prices"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    sku           = Column(String(255), nullable=False)
    region        = Column(String(255), nullable=False)
    os            = Column(String(255), nullable=False)
    payg_rate_usd = Column(Float, nullable=False)
    provider      = Column(String(255), nullable=False)
    fetched_at    = Column(String(255), nullable=False)
    # Added for AWS RDS pricing (2026-08) - see the _ensure_column migration
    # below for why. NULL/unused for Azure and for AWS EC2 rows.
    resource_type = Column(String(255), nullable=True)
    redundancy    = Column(String(255), nullable=True)


class CommitmentPriceCache(Base):
    """
    Cache of REAL commitment-discount pricing (Reserved Instances / Savings
    Plans), fetched from Azure Retail Prices API (api-version=2023-01-01-preview
    - required for the Reservation type's reservationTerm field and the
    Consumption type's nested savingsPlan[] rates) or, in future, AWS's Price
    List / Savings Plans APIs. Keyed narrowly to the SKU/region/OS combos
    actually present in a tenant's live (or demo) inventory - never a full
    regional catalog dump. See pricing/commitment_pricing.py.
    """
    __tablename__ = "commitment_price_cache"

    id                        = Column(Integer, primary_key=True, autoincrement=True)
    provider                  = Column(String(50), nullable=False)   # "Azure" | "AWS"
    instrument                = Column(String(30), nullable=False)   # "SavingsPlan" | "ReservedInstance"
    # Part of the cache key alongside sku/region/os/term (added 2026-08) -
    # without it, two different services sharing an identical SKU string
    # (e.g. SQL Managed Instance and SQL Elastic Pool both reporting
    # "GP_Gen5_8") silently overwrite each other's cached price, even though
    # resolve_sku_query() correctly fetches DIFFERENT real rates for each
    # (different armSkuName prefixes) - caught live via a real Elastic Pool
    # SKU colliding with an existing Managed Instance demo row.
    resource_type             = Column(String(255), nullable=True)
    region                    = Column(String(255), nullable=False)
    sku                       = Column(String(255), nullable=False)
    os                        = Column(String(255), nullable=True)
    # Also part of the cache key (added 2026-08) - same collision risk as
    # resource_type above: the SAME sku/region/os can have a genuinely
    # different price depending on redundancy (Zone-Redundant SQL DB/Elastic
    # Pool meters are priced differently, often cheaper, than Standard - see
    # CloudInventory.redundancy). Without this, a Zone-Redundant and a
    # Standard resource of the same SKU would silently share one cache row.
    redundancy                = Column(String(255), nullable=True)
    term                      = Column(String(20), nullable=False)   # "1yr" | "3yr"
    # Normalized to a $/hr rate either way - Savings Plan rates are natively
    # hourly; Reserved Instance rates come back as a total term price and are
    # divided by (term_months * 730) here so both instruments are directly
    # comparable to PAYG $/hr and to each other.
    effective_hourly_rate_usd = Column(Float, nullable=True)
    payg_hourly_rate_usd      = Column(Float, nullable=True)
    fetched_at                = Column(String(255), nullable=False)


class ReservationPurchase(Base):
    """
    A purchased Reserved Instance / Reserved Capacity record, schema-matched
    field-for-field to Azure's real Microsoft.Capacity/reservationOrders/
    reservations API (ReservationsProperties) - NOT this app's own simplified
    Commitment table. Built deliberately service-by-service (Compute first)
    so every field is either independently verified against Microsoft's own
    REST API docs or explicitly absent, rather than guessed across every
    service at once. See db/seed.py's RESERVATION_PURCHASES for field-level
    verification notes per record.
    Reference: https://learn.microsoft.com/en-us/rest/api/reserved-vm-instances/reservation/get
    """
    __tablename__ = "reservation_purchases"

    id                              = Column(Integer, primary_key=True, autoincrement=True)
    reservation_order_id            = Column(String(100), nullable=False)
    reservation_id                  = Column(String(100), nullable=False)
    name                            = Column(String(255), nullable=False)
    type                            = Column(String(255), nullable=False)
    location                        = Column(String(255), nullable=False)
    sku_name                        = Column(String(255), nullable=False)
    sku_description                 = Column(String(255), nullable=True)
    reserved_resource_type          = Column(String(100), nullable=False)
    instance_flexibility            = Column(String(20), nullable=True)
    applied_scope_type              = Column(String(50), nullable=False)
    applied_scope_display_name      = Column(String(255), nullable=True)
    applied_scope_subscription_id   = Column(String(255), nullable=True)
    # Fully-qualified resource group ID (real AppliedScopeProperties field -
    # confirmed against the installed azure-mgmt-reservations SDK model,
    # 2026-08-23). Only set when applied_scope_type == "Single" AND the
    # reservation was further narrowed to "Single resource group" scope (a
    # real, distinct Azure scoping option beyond subscription-level Single
    # scope) - see pricing/commitment_mapping.py.
    applied_scope_resource_group_id = Column(String(255), nullable=True)
    billing_plan                    = Column(String(50), nullable=False)
    term                            = Column(String(10), nullable=False)   # ISO-8601: P1Y | P3Y | P5Y
    quantity                        = Column(Integer, nullable=False)
    provisioning_state               = Column(String(50), nullable=False)
    renew                            = Column(Boolean, default=False)
    purchase_date                    = Column(String(20), nullable=True)
    purchase_date_time              = Column(String(50), nullable=False)
    effective_date_time             = Column(String(50), nullable=False)
    benefit_start_time              = Column(String(50), nullable=False)
    expiry_date                     = Column(String(20), nullable=True)
    expiry_date_time                = Column(String(50), nullable=False)
    utilization_trend               = Column(String(20), nullable=True)
    utilization_1day_pct            = Column(Float, nullable=True)
    utilization_7day_pct            = Column(Float, nullable=True)
    utilization_30day_pct           = Column(Float, nullable=True)
    provider                        = Column(String(50), default="Azure")
    # NULL = demo/seed data. Non-NULL = live-ingested, scoped to that cloud_tenants.id.
    tenant_id                       = Column(Integer, nullable=True)


class AWSReservationPurchase(Base):
    """
    A purchased AWS Reserved Instance record - kept SEPARATE from
    ReservationPurchase (schema-matched to Azure's real API) rather than
    reused, since AWS's reservation model shares no real structure with
    Azure's: no "reservation order"/scope hierarchy, Duration is raw
    SECONDS not an ISO-8601 period, and the purchase record itself already
    carries FixedPrice/UsagePrice (Azure's carries no $ at all). Forcing
    AWS data into Azure-named fields would be actively misleading, not a
    simplification - same reasoning as AWSCredentials vs AzureCredentials.

    EC2 and RDS reservations come from two entirely separate AWS APIs
    (ec2:DescribeReservedInstances / rds:DescribeReservedDBInstances) with
    materially different fields - confirmed via boto3's own service model,
    not guessed. Unioned into one table via a `service` discriminator
    ("EC2"|"RDS"), the same simplification Azure's own ReservationPurchase
    already makes across ITS several resource types. Fields only one
    service populates are nullable.
    Reference: https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeReservedInstances.html
               https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DescribeReservedDBInstances.html
    """
    __tablename__ = "aws_reservation_purchases"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    # sts:GetCallerIdentity's Account - the SAME value already tagged onto
    # this account's CloudInventory rows via the "Subscription" column (see
    # aws/connector.py). Added 2026-08-23 for traceability only - NOT used
    # to restrict coverage matching, see Commitment.scope_availability_zone's
    # comment for why account-level scope is deliberately left unrestricted.
    account_id              = Column(String(50), nullable=True)
    service                = Column(String(20), nullable=False)    # "EC2" | "RDS" | "ElastiCache" | "Redshift" | "OpenSearch" - originally String(10), too narrow for "ElastiCache" (11 chars) - SQLite never enforced it (silently fine locally) but would have broken on first write to production Azure SQL. Widened here and via _widen_column below for tables that already exist in the wild.
    reserved_instance_id   = Column(String(255), nullable=False)   # ReservedInstancesId (EC2) or ReservedDBInstanceId (RDS)
    instance_type          = Column(String(100), nullable=False)   # InstanceType (EC2) or DBInstanceClass (RDS)
    region                 = Column(String(100), nullable=False)   # the region this purchase was found in during the all-region scan - not on the raw API response itself.
    availability_zone      = Column(String(100), nullable=True)    # EC2 only
    product_description    = Column(String(255), nullable=True)    # EC2: platform (e.g. "Linux/UNIX"); RDS: engine (e.g. "mysql") - confirmed same lowercase convention as DescribeDBInstances's Engine field, via a real example in AWS's own docs.
    instance_count         = Column(Integer, nullable=False)       # InstanceCount (EC2) or DBInstanceCount (RDS)
    duration_seconds       = Column(Integer, nullable=False)       # raw AWS Duration - confirmed valid values are exactly 31536000 (1yr) or 94608000 (3yr), not calendar-based.
    fixed_price            = Column(Float, nullable=True)
    usage_price             = Column(Float, nullable=True)         # $/hr while running, on top of amortized fixed_price - together these fully determine the effective hourly rate with NO separate pricing lookup needed (unlike Azure Reservations, which carry no $ at all).
    currency_code            = Column(String(10), nullable=True)
    offering_type             = Column(String(50), nullable=True)  # "All Upfront" | "Partial Upfront" | "No Upfront"
    offering_class             = Column(String(50), nullable=True) # EC2 only: "standard" | "convertible"
    instance_tenancy            = Column(String(20), nullable=True)  # EC2 only
    scope                        = Column(String(20), nullable=True) # EC2 only: "Availability Zone" | "Region"
    multi_az                     = Column(Boolean, nullable=True)    # RDS only
    state                         = Column(String(50), nullable=False)
    start_time                    = Column(String(50), nullable=True)
    recurring_charge_hourly       = Column(Float, nullable=True)   # pre-summed from RecurringCharges[] where Frequency == "Hourly" - same shape on both EC2 and RDS.
    provider                      = Column(String(50), default="AWS")
    # NULL = demo/seed data. Non-NULL = live-ingested, scoped to that cloud_tenants.id.
    tenant_id                     = Column(Integer, nullable=True)


class AWSSavingsPlanPurchase(Base):
    """
    A purchased AWS Savings Plan record, schema-matched field-for-field to
    savingsplans:DescribeSavingsPlans - kept separate from
    SavingsPlanPurchase (Azure-shaped) for the same reason as
    AWSReservationPurchase above. Unlike Azure (whose sku_name requires
    guessing the plan type - see pricing/commitment_mapping.py), AWS's API
    states savingsPlanType directly - confirmed via boto3's own service
    model enum: "Compute" | "EC2Instance" | "SageMaker" | "Database" - no
    inference needed for the type itself.
    Reference: https://docs.aws.amazon.com/savingsplans/latest/APIReference/API_SavingsPlan.html
    """
    __tablename__ = "aws_savings_plan_purchases"

    id                          = Column(Integer, primary_key=True, autoincrement=True)
    # Same traceability-only field as AWSReservationPurchase.account_id above.
    account_id                  = Column(String(50), nullable=True)
    savings_plan_id             = Column(String(100), nullable=False)
    savings_plan_arn            = Column(String(255), nullable=True)
    description                 = Column(String(255), nullable=True)
    start                       = Column(String(50), nullable=True)
    end                         = Column(String(50), nullable=True)
    state                       = Column(String(50), nullable=False)
    region                      = Column(String(100), nullable=True)   # "" for a Compute Savings Plan (region-agnostic by design); real region for an EC2 Instance Savings Plan (region-locked).
    ec2_instance_family         = Column(String(50), nullable=True)
    savings_plan_type           = Column(String(50), nullable=False)   # "Compute" | "EC2Instance" | "SageMaker" | "Database"
    payment_option               = Column(String(50), nullable=False)  # "No Upfront" | "Partial Upfront" | "All Upfront"
    product_types                 = Column(String(255), nullable=True) # comma-joined
    currency                      = Column(String(10), nullable=True)
    commitment_hourly_usd          = Column(Float, nullable=False)     # 'commitment' field - already a real $/hr rate, no lookup needed (same as Azure's Savings Plans).
    upfront_payment_amount          = Column(Float, nullable=True)
    recurring_payment_amount        = Column(Float, nullable=True)
    term_duration_seconds            = Column(Integer, nullable=False)
    provider                          = Column(String(50), default="AWS")
    # NULL = demo/seed data. Non-NULL = live-ingested, scoped to that cloud_tenants.id.
    tenant_id                         = Column(Integer, nullable=True)


class SavingsPlanPurchase(Base):
    """
    A purchased Savings Plan record, schema-matched field-for-field to
    Azure's real Microsoft.BillingBenefits/savingsPlanOrders/savingsPlans
    API (SavingsPlanModel) - NOT this app's own simplified Commitment table.
    Same verification discipline as ReservationPurchase above.
    Reference: https://learn.microsoft.com/en-us/rest/api/billingbenefits/savings-plan/get
    """
    __tablename__ = "savings_plan_purchases"

    id                          = Column(Integer, primary_key=True, autoincrement=True)
    savings_plan_order_id       = Column(String(100), nullable=False)
    savings_plan_id             = Column(String(100), nullable=False)
    name                        = Column(String(255), nullable=False)
    type                        = Column(String(255), nullable=False)
    sku_name                    = Column(String(255), nullable=False)
    billing_scope_id            = Column(String(255), nullable=False)
    billing_plan                = Column(String(20), nullable=False)   # ISO-8601: P1M
    commitment_grain            = Column(String(20), nullable=False)   # "Hourly"
    commitment_currency_code    = Column(String(10), nullable=False)
    commitment_amount           = Column(Float, nullable=False)
    applied_scope_type          = Column(String(50), nullable=False)
    # Added 2026-08-23 - previously missing entirely (unlike
    # ReservationPurchase, which already captured applied_scope_subscription_id).
    # Real, flattened SavingsPlanModel fields (confirmed against the
    # installed azure-mgmt-billingbenefits SDK: s.applied_scope_properties.
    # subscription_id / .resource_group_id) - same "Single subscription" /
    # "Single resource group" scoping Reservations support. See
    # pricing/commitment_mapping.py.
    applied_scope_subscription_id   = Column(String(255), nullable=True)
    applied_scope_resource_group_id = Column(String(255), nullable=True)
    display_name                = Column(String(255), nullable=True)
    term                        = Column(String(10), nullable=False)   # ISO-8601: P1Y | P3Y | P5Y
    provisioning_state          = Column(String(50), nullable=False)
    renew                       = Column(Boolean, default=False)
    purchase_date_time          = Column(String(50), nullable=False)
    effective_date_time         = Column(String(50), nullable=False)
    benefit_start_time          = Column(String(50), nullable=False)
    expiry_date_time            = Column(String(50), nullable=False)
    utilization_trend           = Column(String(20), nullable=True)
    utilization_1day_pct        = Column(Float, nullable=True)
    utilization_7day_pct        = Column(Float, nullable=True)
    utilization_30day_pct       = Column(Float, nullable=True)
    provider                    = Column(String(50), default="Azure")
    # NULL = demo/seed data. Non-NULL = live-ingested, scoped to that cloud_tenants.id.
    tenant_id                   = Column(Integer, nullable=True)


class CloudTenant(Base):
    """
    Registry of connected Azure Tenants & AWS Accounts stored in Azure SQL DB.
    Allows managing and switching between multiple cloud tenants. Exists in
    BOTH demo and live scopes (2026-08) - Demo Mode's Home page gets a real
    (simulated) tenant entry too, not just Production's real connections.

    client_secret is stored ENCRYPTED (see db/crypto.py's Fernet helper),
    unlike AppUser.password_hash which is one-way bcrypt - this value must be
    recoverable since the app needs the real secret to authenticate against
    Azure, not just verify a match.
    """
    __tablename__ = "cloud_tenants"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    tenant_name      = Column(String(255), nullable=False)
    provider         = Column(String(255), nullable=False)   # Azure | AWS
    tenant_id        = Column(String(255), nullable=False)
    subscription_id  = Column(String(255), nullable=False)
    client_id        = Column(String(255), nullable=False)
    client_secret    = Column(String(500), nullable=False)
    domain           = Column(String(255), nullable=True)
    is_active        = Column(Boolean, default=True)
    created_at       = Column(String(255), nullable=False)
    last_synced_at   = Column(String(255), nullable=True)
    # Tenant-WIDE permission status (Reservations Reader / Savings Plan
    # Reader - see azure_conn/connector.py's check_tenant_role_assignments) -
    # deliberately separate from TenantSubscription's per-subscription
    # permission_status, since these two roles are tenant-scoped, not
    # subscription-scoped, so there's exactly one status per tenant, not one
    # per subscription.
    tenant_permission_status = Column(String(50), default="unchecked")   # "unchecked" | "ready" | "missing_role" | "error"
    # 1000 chars, not a short role-name list - this column also carries the
    # full plain-language message from azure_conn.connector._friendly_auth_error
    # (e.g. the AADSTS7000215 explanation) when status == "error"; SQL Server
    # raises on overflow rather than silently truncating like SQLite, so this
    # was originally undersized at 255 (real bug, caught 2026-08).
    tenant_missing_roles     = Column(String(1000), nullable=True)
    # Which of REQUIRED_TENANT_ROLES are actually assigned, comma-joined -
    # added alongside tenant_missing_roles so the UI can show every required
    # role's real state (assigned/missing), not just the ones missing. Only
    # tenant_missing_roles alone can't distinguish "checked, 0 of 2 missing"
    # from "checked, 2 of 2 missing" without this.
    tenant_assigned_roles    = Column(String(255), nullable=True)
    # How often the 24h-cron Azure Function should re-sync THIS tenant -
    # every tenant shares the same TimerTrigger firing (hourly - see
    # azure_function/function_app.py), which only actually calls
    # run_ingestion_pipeline() for a tenant once now - last_synced_at >=
    # this many hours, so different tenants can run on different cadences
    # without needing a separate Function/Durable orchestration per tenant.
    sync_interval_hours      = Column(Integer, default=24)
    # Persisted result of the most recent sync attempt for this tenant - the
    # Manage Tenant dialog's "Sync" card reads this directly so the result
    # (including a real failure/partial message) survives a dialog reopen or
    # page reload, instead of only a one-shot toast that's gone as soon as
    # the next rerun happens.
    last_sync_status         = Column(String(20), nullable=True)    # "SUCCESS" | "PARTIAL" | "FAILED"
    last_sync_message        = Column(String(500), nullable=True)
    # AWS-only - resolved via sts:GetCallerIdentity (azure_conn... no,
    # aws/connector.py's test_aws_connection) whenever credentials are
    # saved or tested. Shown in the Tenant Management table's "Account ID"
    # column for AWS rows in place of Azure's "Subscriptions" count, which
    # has no AWS equivalent (one credential set = one account here, not a
    # list of sub-scopes) - user's own call, confirmed live 2026-08.
    aws_account_id           = Column(String(50), nullable=True)
    # VM Rightsizing per-tenant overrides (Azure only for now) - NULL on
    # every field means "use the app's documented default", not "unset the
    # feature" (see analysis/rightsizing.py's get_rightsizing_settings).
    # Deliberately real per-tenant DB storage, not a session_state widget
    # like the existing Savings Plan "Safety Buffer %" - that one resets
    # every session, which doesn't satisfy "remember this for their
    # tenant" the way these need to.
    rightsizing_percentile        = Column(Integer, nullable=True)   # 90 | 95 | 99
    rightsizing_cpu_under_pct     = Column(Float, nullable=True)
    rightsizing_cpu_over_pct      = Column(Float, nullable=True)
    rightsizing_mem_under_pct     = Column(Float, nullable=True)
    rightsizing_mem_available_pct = Column(Float, nullable=True)
    rightsizing_lookback_days     = Column(Integer, nullable=True)
    rightsizing_headroom_pct      = Column(Float, nullable=True)
    rightsizing_min_days          = Column(Integer, nullable=True)


class TenantSubscription(Base):
    """
    One row per Azure subscription registered under a CloudTenant - a tenant
    can span multiple subscriptions, each independently permission-checked
    (see azure_conn/connector.py's check_role_assignments). Distinct from
    CloudTenant.subscription_id (kept as-is for backward compatibility with
    already-ingested CloudInventory rows keyed to it) - this table is the
    source of truth for "which subscriptions does this tenant cover" going
    forward, used by the Home/Manage Tenant pages.
    """
    __tablename__ = "tenant_subscriptions"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    tenant_db_id       = Column(Integer, nullable=False)
    subscription_id    = Column(String(255), nullable=False)
    subscription_name  = Column(String(255), nullable=True)
    # "unchecked" | "ready" | "missing_role" | "error"
    permission_status  = Column(String(50), default="unchecked")
    # 1000 chars - see CloudTenant.tenant_missing_roles above, same reasoning
    # (this column also carries the full _friendly_auth_error message on
    # status == "error", not just a short role-name list).
    missing_role       = Column(String(1000), nullable=True)
    # Which of REQUIRED_SUBSCRIPTION_ROLES are actually assigned, comma-joined
    # - same reasoning as CloudTenant.tenant_assigned_roles above.
    assigned_roles      = Column(String(255), nullable=True)
    last_checked_at    = Column(String(255), nullable=True)


class AppUser(Base):
    """
    Simple shared login store for the dashboard itself (not cloud credentials).
    One shared user list for the whole app - no per-user tenant scoping yet.
    Passwords are bcrypt-hashed, never stored or logged in plaintext. This is
    a deliberately minimal placeholder ahead of real Entra ID / OIDC login
    (see PROJECT_CONTEXT.md) - it exists so the app isn't wide open before
    that lands, not as a long-term identity system.
    """
    __tablename__ = "app_users"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    username       = Column(String(255), nullable=False, unique=True)
    password_hash  = Column(String(255), nullable=False)
    display_name   = Column(String(255), nullable=True)
    is_active      = Column(Boolean, default=True)
    created_at     = Column(String(255), nullable=False)
    last_login_at  = Column(String(255), nullable=True)


class AppSession(Base):
    """
    Server-side session store backing login persistence across a real browser
    refresh - plain st.session_state alone does NOT survive one (confirmed by
    reproducing it on the local dev server, not just in Azure - a fresh page
    load always gets a brand-new, empty session_state in Streamlit; only
    reruns over the same still-open WebSocket connection, like a button
    click, keep it). The fix: a random token is put in the page's URL query
    string (st.query_params) at login time, a row here maps that token back
    to who's signed in, and ui/auth_page.py's require_login() restores
    st.session_state from this row whenever auth_user is missing but the
    token is present - i.e. exactly on a fresh load, not on every rerun.

    Backed by the real DB (not an in-memory dict) so it also survives an App
    Service cold-start restart (this app runs on a Free/F1 plan with no
    "Always On" - the whole process, and any in-memory state, is gone after
    an idle period), not just a same-process page refresh.

    Always lives in the Azure/live schema regardless of whether the session
    itself is a demo or production login - the whole point of looking a
    token up is to find out its mode, so the table it lives in can't itself
    be split by mode the way AppUser is (that would mean checking both
    schemas for every unauthenticated request).
    """
    __tablename__ = "app_sessions"

    token          = Column(String(64), primary_key=True)
    user_id        = Column(Integer, nullable=False)
    username       = Column(String(255), nullable=False)
    display_name   = Column(String(255), nullable=True)
    mode           = Column(String(10), nullable=False)   # "demo" | "live"
    created_at     = Column(String(255), nullable=False)
    last_seen_at   = Column(String(255), nullable=False)
