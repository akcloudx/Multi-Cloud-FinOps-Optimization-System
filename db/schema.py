"""
db/schema.py
Star Schema for Multi-Cloud FinOps Optimization System.
Compatible with SQLite and Microsoft Azure SQL Database.
"""

import os
from sqlalchemy import (
    create_engine, Column, String, Float, Integer, Boolean, Text, inspect, text
)
from sqlalchemy.orm import declarative_base

_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
AZURE_DB_PATH = os.path.join(_PROJECT_ROOT, "azure_finops.db")
AWS_DB_PATH   = os.path.join(_PROJECT_ROOT, "aws_finops.db")

Base = declarative_base()
_engines = {}


def get_engine(provider: str = "Azure"):
    """Return a singleton SQLAlchemy engine for the specified cloud provider."""
    global _engines
    provider_key = provider.upper()
    if provider_key not in _engines:
        db_url = os.getenv("DATABASE_URL")
        # In Azure production environment (App Service / Function App), use Azure SQL Database
        if not db_url and (os.getenv("WEBSITE_SITE_NAME") or os.getenv("FUNCTIONS_WORKER_RUNTIME")):
            db_url = "mssql+pymssql://finopsadmin:P%40ssw0rd2026%21FinOps@finops-sql-e0b96fd6.database.windows.net:1433/finops-db"

        if db_url and "mssql" in db_url:
            if "pymssql" not in db_url and "pyodbc" not in db_url:
                db_url = db_url.replace("mssql://", "mssql+pymssql://")
            try:
                engine = create_engine(db_url, echo=False, future=True, pool_pre_ping=True)
                with engine.connect() as conn:
                    pass
                _engines[provider_key] = engine
                print(f"[Info] Successfully connected to Production Azure SQL Database for {provider_key}")
                return _engines[provider_key]
            except Exception as ex:
                print(f"[Warning] Azure SQL Database connection attempt failed ({ex}).")

        # Fallback to local SQLite ONLY for offline local desktop development
        db_path = AWS_DB_PATH if provider_key == "AWS" else AZURE_DB_PATH
        db_url = f"sqlite:///{db_path}"
        _engines[provider_key] = create_engine(db_url, echo=False, future=True)
    return _engines[provider_key]


def _ensure_column(engine, table_name: str, column_name: str, column_type_sql: str):
    """Add a column to an already-existing table if missing (SQLAlchemy's create_all
    only creates missing tables, never alters existing ones). No 'COLUMN' keyword in
    the SQL - SQLite accepts it but SQL Server's ALTER TABLE ADD syntax does not."""
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
    if column_name not in existing_cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table_name} ADD {column_name} {column_type_sql}"))


def init_db(provider: str = "Azure"):
    """Create all tables for the specified cloud provider if they don't exist yet,
    and migrate any columns added after a table already existed in the wild."""
    engine = get_engine(provider)
    Base.metadata.create_all(engine)
    _ensure_column(engine, "cloud_inventory", "tenant_id", "INTEGER")
    _ensure_column(engine, "commitments", "tenant_id", "INTEGER")
    _ensure_column(engine, "commitment_price_cache", "resource_type", "VARCHAR(255)")
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
    payg_hourly_usd          = Column(Float, nullable=False)
    avg_daily_running_hours  = Column(Integer, nullable=False)
    subscription             = Column(String(255), nullable=True)
    provider                 = Column(String(255), default="Azure")
    is_orphaned              = Column(Boolean, default=False)
    # NULL = demo/seed data. Non-NULL = live-ingested, scoped to that cloud_tenants.id.
    tenant_id                = Column(Integer, nullable=True)


class Commitment(Base):
    __tablename__ = "commitments"

    commitment_id           = Column(String(500), primary_key=True)
    commitment_type         = Column(String(255), nullable=False)
    scope_sku               = Column(String(255), nullable=False)
    scope_region            = Column(String(255), nullable=False)
    scope_os                = Column(String(255), nullable=True)
    hourly_usd_commitment   = Column(Float, nullable=False)
    reserved_qty            = Column(Integer, default=0)
    term                    = Column(String(255), default="1-year")
    expiry_date             = Column(String(255), nullable=True)
    provider                = Column(String(255), default="Azure")
    # NULL = demo/seed data. Non-NULL = live-ingested, scoped to that cloud_tenants.id.
    tenant_id               = Column(Integer, nullable=True)


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
    Allows managing and switching between multiple cloud tenants.
    """
    __tablename__ = "cloud_tenants"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    tenant_name      = Column(String(255), nullable=False)
    provider         = Column(String(255), nullable=False)   # Azure | AWS
    tenant_id        = Column(String(255), nullable=False)
    subscription_id  = Column(String(255), nullable=False)
    client_id        = Column(String(255), nullable=False)
    client_secret    = Column(String(500), nullable=False)
    is_active        = Column(Boolean, default=True)
    created_at       = Column(String(255), nullable=False)


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
