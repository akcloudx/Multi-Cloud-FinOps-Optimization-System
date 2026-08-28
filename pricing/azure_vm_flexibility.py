"""
pricing/azure_vm_flexibility.py
Real Azure VM Reserved Instance instance-size-flexibility group + ratio
per SKU/region, cached in the AzureVmFlexibilityGroup SQL table - lets
analysis/engine.py's reconciliation know which running VM sizes a
reservation purchased for a DIFFERENT size can still cover.

Deliberately a separate cache from pricing/commitment_pricing.py's
CommitmentPriceCache (2026-08-29) - this isn't a $ rate, isn't
term-dependent, and (confirmed via Microsoft's own docs) the ratio isn't
a generic size-name formula the way AWS's normalization factors are (real
example: the "BS Series" group starts at 0.25, "Ddsv5 Series" starts at
2 - no uniform ladder to hardcode the way AWS's was). Same
API -> ingest -> SQL DB -> analysis engine reads the DB only
architecture as every other cache in this app - see
azure_conn/connector.py::fetch_vm_flexibility_groups for the real API
call (Reservations Catalog API, Microsoft.Capacity/catalogs).

Called ONLY from the ingestion pipeline (data/sync_pipeline.py) and the
demo data loader - analysis/engine.py never calls the live API directly.
"""

from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from db.schema import AzureVmFlexibilityGroup

CACHE_MAX_AGE_HOURS = 24 * 7   # 7-day TTL, same as the AWS SP discount cache added earlier this session - this data changes rarely (Microsoft revises ratios occasionally, e.g. their own M-series ratio-change announcement), not worth a live call on every sync.


def refresh_vm_flexibility_groups(engine, resource_rows: list[dict], creds) -> None:
    """For each unique (region, sku) combo among Azure "Compute" rows in
    resource_rows, ensures a fresh AzureVmFlexibilityGroup cache row
    exists. Fetches ONE live call per distinct REGION that has any
    stale/missing SKU (fetch_vm_flexibility_groups already returns every
    VM SKU Azure knows about for that region in one call - no need to
    call it per-SKU), then persists only the rows that overlap with SKUs
    actually present in this tenant's inventory (narrow keying, not a
    full regional catalog dump - same discipline as
    pricing/commitment_pricing.py). Best-effort per region: a failure
    fetching one region's catalog doesn't block any other region.
    """
    if not creds:
        return
    vm_combos = {
        (r.get("Region") or r.get("region"), r.get("SKU") or r.get("sku"))
        for r in resource_rows
        if (r.get("Resource Type") or r.get("resource_type")) == "Compute"
    }
    vm_combos = {(region, sku) for region, sku in vm_combos if region and sku and sku != "N/A"}
    if not vm_combos:
        return

    regions = {region for region, _ in vm_combos}
    cutoff = (datetime.utcnow() - timedelta(hours=CACHE_MAX_AGE_HOURS)).strftime("%Y-%m-%d %H:%M:%S UTC")

    with Session(engine) as session:
        cached = {
            (row.region, row.sku): row
            for row in session.query(AzureVmFlexibilityGroup).filter(
                AzureVmFlexibilityGroup.region.in_(regions)
            ).all()
        }
        now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        for region in regions:
            region_skus = {sku for r, sku in vm_combos if r == region}
            stale_skus = {
                sku for sku in region_skus
                if (region, sku) not in cached or cached[(region, sku)].fetched_at < cutoff
            }
            if not stale_skus:
                continue
            try:
                live_groups = fetch_vm_flexibility_groups(creds, region)
            except Exception:
                continue   # best-effort - a failed region fetch shouldn't block others or the sync

            for sku in stale_skus:
                found = live_groups.get(sku)
                if found is None:
                    continue   # this SKU isn't in Azure's flexibility catalog for this region - leave uncached, reconciliation passes it through unchanged
                group_name, ratio = found
                row = cached.get((region, sku))
                if row:
                    row.flexibility_group = group_name
                    row.ratio = ratio
                    row.fetched_at = now_iso
                else:
                    session.add(AzureVmFlexibilityGroup(
                        region=region, sku=sku, flexibility_group=group_name,
                        ratio=ratio, fetched_at=now_iso,
                    ))
        session.commit()


def get_vm_flexibility_groups(engine) -> pd.DataFrame:
    """Reads the full cached VM flexibility-group table back out as a
    DataFrame - this is what analysis/engine.py actually reads; it never
    calls the live API directly."""
    columns = ["region", "sku", "flexibility_group", "ratio", "fetched_at"]
    with Session(engine) as session:
        rows = session.query(AzureVmFlexibilityGroup).all()
        data = [{c: getattr(r, c) for c in columns} for r in rows]
    return pd.DataFrame(data, columns=columns)


# Deferred import (not at module top) to avoid a circular import -
# azure_conn/connector.py doesn't import from pricing/, but this module
# being imported early in some paths (e.g. db/seed.py at startup) could
# still race Azure SDK package availability checks; matches the same
# "import where used" caution already taken elsewhere in this codebase
# for optional Azure SDK dependencies.
from azure_conn.connector import fetch_vm_flexibility_groups  # noqa: E402
