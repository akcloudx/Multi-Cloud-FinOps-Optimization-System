"""
pricing/azure_vm_memory.py
Real total RAM (GB) per Azure VM SKU/region, cached in the
AzureVmSkuMemory SQL table - lets azure_conn/connector.py's
fetch_vm_utilization_metrics convert Azure Monitor's "Available Memory
Bytes" metric (a raw byte count) into the "% available" this app's
rightsizing engine (analysis/rightsizing.py) actually expects.

Exact sibling of pricing/azure_vm_flexibility.py (same API -> ingest ->
SQL DB -> caller-reads-DB-only architecture, same per-region cache/TTL/
best-effort discipline) - see azure_conn/connector.py::fetch_vm_sku_memory
for the real API call (Resource SKUs API, Microsoft.Compute/skus).

Called ONLY from the ingestion pipeline (data/sync_pipeline.py) - never
from analysis/rightsizing.py or app.py directly.
"""

from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from db.schema import AzureVmSkuMemory

CACHE_MAX_AGE_HOURS = 24 * 7   # 7-day TTL, same as AzureVmFlexibilityGroup - a SKU's total RAM never changes, this is generous, not tight.


def refresh_vm_sku_memory(engine, resource_rows: list[dict], creds) -> None:
    """For each unique (region, sku) combo among Azure "Compute" rows in
    resource_rows, ensures a fresh AzureVmSkuMemory cache row exists.
    Fetches ONE live call per distinct REGION that has any stale/missing
    SKU (fetch_vm_sku_memory already returns every VM SKU Azure knows
    about for that region in one call), then persists only the rows that
    overlap with SKUs actually present in this tenant's inventory - same
    narrow-keying discipline as refresh_vm_flexibility_groups. Best-effort
    per region: a failure fetching one region's catalog doesn't block any
    other region.
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
            for row in session.query(AzureVmSkuMemory).filter(
                AzureVmSkuMemory.region.in_(regions)
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
                live_memory = fetch_vm_sku_memory(creds, region)
            except Exception:
                continue   # best-effort - a failed region fetch shouldn't block others or the sync

            for sku in stale_skus:
                memory_gb = live_memory.get(sku)
                if memory_gb is None:
                    continue   # this SKU isn't in Azure's SKU catalog for this region - leave uncached, memory % conversion is skipped for it
                row = cached.get((region, sku))
                if row:
                    row.memory_gb = memory_gb
                    row.fetched_at = now_iso
                else:
                    session.add(AzureVmSkuMemory(
                        region=region, sku=sku, memory_gb=memory_gb, fetched_at=now_iso,
                    ))
        session.commit()


def get_vm_sku_memory(engine) -> dict:
    """Reads the full cached SKU->memory_gb table back out as a plain
    {sku: memory_gb} dict, merged across all cached regions - a VM SKU's
    total RAM is a fixed hardware spec, not region-dependent, so unlike
    AzureVmFlexibilityGroup's ratio (which Microsoft's own docs confirm
    CAN vary by region) this collapses cleanly to one dict keyed by SKU
    alone. Where the same SKU was somehow cached with two different
    memory_gb values across regions (shouldn't happen for a real SKU, but
    not fabricated-safe to assume), the most-recently-fetched row wins."""
    with Session(engine) as session:
        rows = session.query(AzureVmSkuMemory).order_by(AzureVmSkuMemory.fetched_at.asc()).all()
        return {row.sku: row.memory_gb for row in rows}


# Deferred import (not at module top) to avoid a circular import - same
# caution already taken in pricing/azure_vm_flexibility.py.
from azure_conn.connector import fetch_vm_sku_memory  # noqa: E402
