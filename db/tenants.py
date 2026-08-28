"""
db/tenants.py — Multi-tenant CloudTenant registry helpers.

Every connected Service Principal / IAM credential set is a row in cloud_tenants
(db/schema.py), persisted in SQL DB (SQLite locally, Azure SQL in production).
Exactly one tenant per (provider, mode) can be "active" at a time
(CloudTenant.is_active) - that's the tenant whose live-ingested CloudInventory
rows (tenant_id FK) the dashboard shows when in Live mode.

2026-08: tenants now exist in BOTH demo and live scopes - the Home page gives
Demo Mode a real (simulated) tenant entry too, not just Production's real
connections, so every function here takes an explicit `mode` instead of the
old hardcoded "live" (tenant registration used to be a Live-only concept).

client_secret is stored ENCRYPTED (db/crypto.py) - upsert_tenant/
upsert_subscription encrypt on the way in, get_tenant_credentials decrypts on
the way out, right at the point of use. Nothing else in this module ever
touches the raw secret.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from db.schema import (
    get_engine, init_db, CloudTenant, CloudInventory, Commitment,
    ReservationPurchase, SavingsPlanPurchase, TenantSubscription,
)
from db.crypto import encrypt_secret, decrypt_secret


def list_tenants(provider: str = "Azure", mode: str = "live") -> list[CloudTenant]:
    """All saved tenants for a (provider, mode) scope, oldest first. Detached
    from the session. client_secret stays encrypted here - decrypt only via
    get_tenant_credentials() at the point a real API call needs it."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        rows = session.query(CloudTenant).filter(
            CloudTenant.provider == provider
        ).order_by(CloudTenant.id).all()
        session.expunge_all()
        return rows


def get_active_tenant(provider: str = "Azure", mode: str = "live") -> Optional[CloudTenant]:
    """The one tenant currently selected for viewing in this (provider, mode)
    scope, or None."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        row = session.query(CloudTenant).filter(
            CloudTenant.provider == provider, CloudTenant.is_active == True  # noqa: E712
        ).order_by(CloudTenant.id).first()
        if row:
            session.expunge(row)
        return row


def get_tenant_credentials(tenant: CloudTenant) -> str:
    """Decrypts and returns the tenant's real client_secret. Call this right
    at the point of use (building AzureCredentials for an API call) - never
    store the decrypted result anywhere."""
    return decrypt_secret(tenant.client_secret)


def upsert_tenant(provider: str, mode: str, tenant_name: str, tenant_id: str,
                   subscription_id: str, client_id: str, client_secret: str,
                   domain: Optional[str] = None) -> int:
    """Insert a new tenant, or update in place if one already exists for this
    exact (provider, tenant_id, subscription_id, client_id). Either way it
    becomes the active tenant for the (provider, mode) scope. Returns the
    tenant's DB id."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    encrypted_secret = encrypt_secret(client_secret)
    with Session(engine) as session:
        existing = session.query(CloudTenant).filter(
            CloudTenant.provider == provider,
            CloudTenant.tenant_id == tenant_id,
            CloudTenant.subscription_id == subscription_id,
            CloudTenant.client_id == client_id,
        ).first()

        session.query(CloudTenant).filter(CloudTenant.provider == provider).update({"is_active": False})

        if existing:
            existing.tenant_name = tenant_name
            existing.client_secret = encrypted_secret
            existing.domain = domain
            existing.is_active = True
            session.commit()
            return existing.id

        row = CloudTenant(
            tenant_name=tenant_name, provider=provider, tenant_id=tenant_id,
            subscription_id=subscription_id, client_id=client_id,
            client_secret=encrypted_secret, domain=domain, is_active=True,
            created_at=now_iso,
        )
        session.add(row)
        session.commit()
        return row.id


def update_tenant_name(provider: str, mode: str, tenant_db_id: int, tenant_name: str) -> None:
    """Renames a tenant - the one edit action available in both Demo and
    Production (harmless/cosmetic, unlike credential edits which only make
    sense for real Production tenants)."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update({"tenant_name": tenant_name})
        session.commit()


def update_rightsizing_settings(provider: str, mode: str, tenant_db_id: int, settings: dict) -> None:
    """Saves this tenant's VM Rightsizing overrides (analysis/rightsizing.py's
    SETTINGS_FIELDS: percentile, cpu_under_pct, cpu_over_pct, mem_under_pct,
    mem_available_pct, lookback_days, headroom_pct, min_days). Same
    per-tenant, real-DB persistence as the rest of CloudTenant - not the
    session_state-only pattern the existing Savings Plan "Safety Buffer %"
    uses, since these need to survive a session/login, not just a rerun."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update({
            "rightsizing_percentile": settings["percentile"],
            "rightsizing_cpu_under_pct": settings["cpu_under_pct"],
            "rightsizing_cpu_over_pct": settings["cpu_over_pct"],
            "rightsizing_mem_under_pct": settings["mem_under_pct"],
            "rightsizing_mem_available_pct": settings["mem_available_pct"],
            "rightsizing_lookback_days": settings["lookback_days"],
            "rightsizing_headroom_pct": settings["headroom_pct"],
            "rightsizing_min_days": settings["min_days"],
        })
        session.commit()


def update_aws_account_id(provider: str, mode: str, tenant_db_id: int, account_id: Optional[str]) -> None:
    """AWS-only - stores the real AWS account ID resolved via STS
    GetCallerIdentity (aws/connector.py's test_aws_connection), shown in the
    Tenant Management table's "Account ID" column for AWS rows. Called
    whenever AWS credentials are saved or tested, best-effort - a failed
    resolution just leaves this None rather than blocking the save/check
    it's piggybacking on."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update({"aws_account_id": account_id})
        session.commit()


def update_aws_sp_pricing(provider: str, mode: str, tenant_db_id: int,
                           compute_1yr: Optional[float], compute_3yr: Optional[float],
                           sagemaker_1yr: Optional[float], sagemaker_3yr: Optional[float],
                           database_1yr: Optional[float],
                           updated_at: str) -> None:
    """AWS-only - stores the real cached Savings Plans discount % per
    pool/term (aws/connector.py's fetch_savings_plans_recommendation,
    ce:GetSavingsPlansPurchaseRecommendation), read by
    analysis/commitment_economics.py's aws_savings_plan_term_comparison.
    database_1yr only (no 3yr) - Database Savings Plans are a 1-year-term-
    only product (confirmed via AWS's own Savings Plans FAQ). Called by
    the sync pipeline after a live fetch, and once by db/aws_seed.py to
    pre-seed the demo tenant with illustrative-but-realistic values (same
    reasoning as Azure's demo shipping with cached CommitmentPriceCache
    rows instead of an empty table)."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update({
            "aws_sp_compute_discount_1yr": compute_1yr,
            "aws_sp_compute_discount_3yr": compute_3yr,
            "aws_sp_sagemaker_discount_1yr": sagemaker_1yr,
            "aws_sp_sagemaker_discount_3yr": sagemaker_3yr,
            "aws_sp_database_discount_1yr": database_1yr,
            "aws_sp_pricing_updated_at": updated_at,
        })
        session.commit()


def update_tenant_permission_status(provider: str, mode: str, tenant_db_id: int,
                                     status: str, missing_roles: Optional[str] = None,
                                     assigned_roles: Optional[str] = None) -> None:
    """Stamps the tenant-WIDE permission status (Reservations Reader /
    Savings Plan Reader - see azure_conn/connector.py's
    check_tenant_role_assignments). `missing_roles`/`assigned_roles` are
    plain comma-joined strings, not relational lists - there are only ever
    0-2 of these roles, a dedicated table would be overkill. Both are stored
    (not just missing) so the UI can show every required role's real state."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update({
            "tenant_permission_status": status,
            "tenant_missing_roles": missing_roles,
            "tenant_assigned_roles": assigned_roles,
        })
        session.commit()


def touch_last_synced(provider: str, mode: str, tenant_db_id: int) -> None:
    """Stamps CloudTenant.last_synced_at with now - called after a real sync
    completes (never in Demo, where the Run sync now action stays disabled)."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update({"last_synced_at": now_iso})
        session.commit()


def record_sync_result(provider: str, mode: str, tenant_db_id: int, status: str, message: str) -> None:
    """Persists the outcome of a run_ingestion_pipeline() call onto the
    tenant row itself (last_sync_status/last_sync_message) - the Manage
    Tenant dialog's Sync card reads these directly, so a real failure or
    partial-success detail (e.g. "RI/SP fetch skipped - AuthorizationFailed")
    stays visible after the dialog reopens or the page reloads, instead of
    only existing as a one-shot toast. Also stamps last_synced_at for
    SUCCESS/PARTIAL (inventory really did just refresh in both cases) but
    not FAILED (nothing was written)."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    updates = {"last_sync_status": status, "last_sync_message": message[:500]}
    if status in ("SUCCESS", "PARTIAL"):
        updates["last_synced_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update(updates)
        session.commit()


def update_sync_interval(provider: str, mode: str, tenant_db_id: int, hours: int) -> None:
    """Sets how often the shared hourly Azure Function cron should re-sync
    THIS tenant - see CloudTenant.sync_interval_hours in db/schema.py for how
    a single TimerTrigger serves every tenant on its own cadence."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update({"sync_interval_hours": hours})
        session.commit()


def set_active_tenant(provider: str, mode: str, tenant_db_id: int) -> None:
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.provider == provider).update({"is_active": False})
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update({"is_active": True})
        session.commit()


def delete_tenant(provider: str, mode: str, tenant_db_id: int) -> None:
    """Removes the tenant and every row ingested/registered for it -
    CloudInventory, Commitment, ReservationPurchase, SavingsPlanPurchase, and
    TenantSubscription (the first two were the only ones cleaned up before
    2026-08; the purchase-record tables were left orphaned - a real
    pre-existing gap, closed here since this function was already being
    rewritten for mode support)."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        session.query(CloudInventory).filter(CloudInventory.tenant_id == tenant_db_id).delete(synchronize_session=False)
        session.query(Commitment).filter(Commitment.tenant_id == tenant_db_id).delete(synchronize_session=False)
        session.query(ReservationPurchase).filter(ReservationPurchase.tenant_id == tenant_db_id).delete(synchronize_session=False)
        session.query(SavingsPlanPurchase).filter(SavingsPlanPurchase.tenant_id == tenant_db_id).delete(synchronize_session=False)
        session.query(TenantSubscription).filter(TenantSubscription.tenant_db_id == tenant_db_id).delete(synchronize_session=False)
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).delete(synchronize_session=False)
        session.commit()


def resource_count(provider: str, mode: str, tenant_db_id: int) -> int:
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        return session.query(CloudInventory).filter(CloudInventory.tenant_id == tenant_db_id).count()


# ── Subscriptions (child of a tenant, see TenantSubscription in db/schema.py) ──

def list_subscriptions(provider: str, mode: str, tenant_db_id: int) -> list[TenantSubscription]:
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    with Session(engine) as session:
        rows = session.query(TenantSubscription).filter(
            TenantSubscription.tenant_db_id == tenant_db_id
        ).order_by(TenantSubscription.id).all()
        session.expunge_all()
        return rows


def upsert_subscription(provider: str, mode: str, tenant_db_id: int, subscription_id: str,
                         subscription_name: Optional[str] = None,
                         permission_status: str = "unchecked",
                         missing_role: Optional[str] = None,
                         assigned_roles: Optional[str] = None) -> int:
    """Insert or update one subscription row under a tenant, keyed on
    (tenant_db_id, subscription_id). Used both by the real Azure "Sync
    subscriptions" flow (re-enumerating + re-checking permissions) and by
    demo seeding (writing pre-set simulated results directly). assigned_roles
    (comma-joined, alongside the existing missing_role) lets the UI show
    every required role's real state, not just the missing ones."""
    init_db(provider, mode)
    engine = get_engine(provider, mode)
    now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    with Session(engine) as session:
        existing = session.query(TenantSubscription).filter(
            TenantSubscription.tenant_db_id == tenant_db_id,
            TenantSubscription.subscription_id == subscription_id,
        ).first()
        if existing:
            if subscription_name:
                existing.subscription_name = subscription_name
            existing.permission_status = permission_status
            existing.missing_role = missing_role
            existing.assigned_roles = assigned_roles
            existing.last_checked_at = now_iso
            session.commit()
            return existing.id

        row = TenantSubscription(
            tenant_db_id=tenant_db_id, subscription_id=subscription_id,
            subscription_name=subscription_name, permission_status=permission_status,
            missing_role=missing_role, assigned_roles=assigned_roles, last_checked_at=now_iso,
        )
        session.add(row)
        session.commit()
        return row.id
