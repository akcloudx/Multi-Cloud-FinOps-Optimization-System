"""
db/tenants.py — Multi-tenant CloudTenant registry helpers.

Every connected Service Principal / IAM credential set is a row in cloud_tenants
(db/schema.py), persisted in SQL DB (SQLite locally, Azure SQL in production).
Exactly one tenant per provider can be "active" at a time (CloudTenant.is_active) -
that's the tenant whose live-ingested CloudInventory rows (tenant_id FK) the
dashboard shows when in Live mode.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from db.schema import get_engine, init_db, CloudTenant, CloudInventory, Commitment


def list_tenants(provider: str = "Azure") -> list[CloudTenant]:
    """All saved tenants for a provider, oldest first. Detached from the session."""
    init_db(provider)
    engine = get_engine(provider)
    with Session(engine) as session:
        rows = session.query(CloudTenant).filter(
            CloudTenant.provider == provider
        ).order_by(CloudTenant.id).all()
        session.expunge_all()
        return rows


def get_active_tenant(provider: str = "Azure") -> Optional[CloudTenant]:
    """The one tenant currently selected for Live mode viewing, or None."""
    init_db(provider)
    engine = get_engine(provider)
    with Session(engine) as session:
        row = session.query(CloudTenant).filter(
            CloudTenant.provider == provider, CloudTenant.is_active == True  # noqa: E712
        ).order_by(CloudTenant.id).first()
        if row:
            session.expunge(row)
        return row


def upsert_tenant(provider: str, tenant_name: str, tenant_id: str,
                   subscription_id: str, client_id: str, client_secret: str) -> int:
    """Insert a new tenant, or update in place if one already exists for this
    exact (provider, tenant_id, subscription_id, client_id). Either way it
    becomes the active tenant for the provider. Returns the tenant's DB id."""
    init_db(provider)
    engine = get_engine(provider)
    now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
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
            existing.client_secret = client_secret
            existing.is_active = True
            session.commit()
            return existing.id

        row = CloudTenant(
            tenant_name=tenant_name, provider=provider, tenant_id=tenant_id,
            subscription_id=subscription_id, client_id=client_id,
            client_secret=client_secret, is_active=True, created_at=now_iso,
        )
        session.add(row)
        session.commit()
        return row.id


def set_active_tenant(provider: str, tenant_db_id: int) -> None:
    init_db(provider)
    engine = get_engine(provider)
    with Session(engine) as session:
        session.query(CloudTenant).filter(CloudTenant.provider == provider).update({"is_active": False})
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).update({"is_active": True})
        session.commit()


def delete_tenant(provider: str, tenant_db_id: int) -> None:
    """Removes the tenant and every CloudInventory/Commitment row ingested for it."""
    init_db(provider)
    engine = get_engine(provider)
    with Session(engine) as session:
        session.query(CloudInventory).filter(CloudInventory.tenant_id == tenant_db_id).delete(synchronize_session=False)
        session.query(Commitment).filter(Commitment.tenant_id == tenant_db_id).delete(synchronize_session=False)
        session.query(CloudTenant).filter(CloudTenant.id == tenant_db_id).delete(synchronize_session=False)
        session.commit()


def resource_count(provider: str, tenant_db_id: int) -> int:
    init_db(provider)
    engine = get_engine(provider)
    with Session(engine) as session:
        return session.query(CloudInventory).filter(CloudInventory.tenant_id == tenant_db_id).count()
