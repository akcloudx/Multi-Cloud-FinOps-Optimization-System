"""
pricing/cache_admin.py
UI-triggered maintenance for this app's pricing/commitment caches - a
DIFFERENT concern from every other pricing/*.py module, which are all
documented "ingestion pipeline only, UI reads the DB only" (see e.g.
pricing/commitment_pricing.py's own module docstring). This module is
the deliberate exception: a real admin action (Manage Tenant's Features
section) needs to clear these caches directly, not just read them.

Added 2026-09-02 after a real incident: a bug in pricing/
azure_retail_api.py cached a wrong (technically-real-but-contextually-
wrong) rate for an Azure SQL Serverless database. Fixing the CODE didn't
fix the already-cached bad value - the sync pipeline's own "keep stale
cache over no data at all" policy (reasonable for transient failures)
meant it would have persisted indefinitely without a manual
`DELETE FROM retail_prices ...` run directly in the Azure Portal's Query
editor. This gives that same fix as a button instead.

None of the 4 tables cleared here (RetailPrice, CommitmentPriceCache,
AzureVmFlexibilityGroup, AzureVmSkuMemory) carry a tenant_id - they're
all keyed by SKU/region/provider and shared across every tenant of that
provider within a schema (see each table's own docstring in
db/schema.py). Clearing is therefore scoped to (provider, mode), not to
one tenant, even though the button lives inside one tenant's Manage
dialog - the caller is responsible for making that scope clear in the
UI, this function doesn't pretend otherwise.
"""

from sqlalchemy.orm import Session

from db.schema import RetailPrice, CommitmentPriceCache, AzureVmFlexibilityGroup, AzureVmSkuMemory


def clear_pricing_caches(engine, provider: str) -> dict:
    """Deletes every cached pricing/commitment row for `provider` (all 4
    cache tables) - the next sync re-fetches everything from scratch, live,
    for whatever SKUs/regions are actually present in inventory at that
    time. Returns {table_name: rows_deleted} so the caller can show a real
    count, not just a generic "done".
    """
    deleted = {}
    with Session(engine) as session:
        deleted["retail_prices"] = (
            session.query(RetailPrice).filter(RetailPrice.provider == provider).delete(synchronize_session=False)
        )
        deleted["commitment_price_cache"] = (
            session.query(CommitmentPriceCache).filter(CommitmentPriceCache.provider == provider).delete(synchronize_session=False)
        )
        # Azure-only tables - a no-op delete (0 rows) for AWS, not an error,
        # since neither table has ever had an AWS row written to it.
        deleted["azure_vm_flexibility_group"] = (
            session.query(AzureVmFlexibilityGroup).delete(synchronize_session=False) if provider == "Azure" else 0
        )
        deleted["azure_vm_sku_memory"] = (
            session.query(AzureVmSkuMemory).delete(synchronize_session=False) if provider == "Azure" else 0
        )
        session.commit()
    return deleted
