"""
data/inventory_loader.py
Loads the compute & database asset registry from the SQLite database.

Internal working schema columns:
  resource_id, resource_name, resource_type, resource_state,
  region, os, sku, payg_hourly_usd, avg_daily_running_hours,
  subscription, provider, is_orphaned

These are normalized/provider-neutral in spirit but are NOT the FOCUS
(FinOps Open Cost & Usage Specification) column names or vocabularies -
see analysis/focus_mapping.py for the actual FOCUS v1.2 projection, exposed
in the UI via the Asset Inventory tab's "FOCUS View" toggle.

This module is purely the READ side and doesn't care whether a row came
from demo/seed data or a real live fetch - both land in the identical
`cloud_inventory` table shape via the same CloudInventory model, so one
query path serves both. Live fetch is real and already wired up (Azure:
azure_conn/connector.py's fetch_live_inventory via Resource Graph; AWS:
aws/connector.py's fetch_live_inventory across all regions) - called from
data/sync_pipeline.py's write side, not from here.
"""

import pandas as pd
from sqlalchemy.orm import Session
from db.schema import init_db, get_engine, CloudInventory


def get_compute_inventory(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> pd.DataFrame:
    """
    Returns the normalized inventory DataFrame from SQL DB for the specified
    (provider, mode) scope. Columns match FOCUS schema for multi-cloud
    compatibility.

    mode: "demo" or "live" - which physically separate scope to read from
    (db/schema.py's demo/live split). tenant_id only matters within "live"
    (which specific connected tenant's rows to read, when more than one is
    registered) - within "demo" it's always None, since the demo scope only
    ever holds seed rows with tenant_id IS NULL by construction.
    """
    engine = get_engine(provider, mode)
    init_db(provider, mode)

    with Session(engine) as session:
        query = session.query(CloudInventory)
        if tenant_id is None:
            query = query.filter(CloudInventory.tenant_id.is_(None))
        else:
            query = query.filter(CloudInventory.tenant_id == tenant_id)
        rows = query.all()

    data = [
        {
            "Resource ID":              r.resource_id,
            "Resource Name":            r.resource_name,
            "Resource Type":            r.resource_type,
            "Resource State":           r.resource_state,
            "Region":                   r.region,
            "OS":                       r.os,
            "SKU":                      r.sku,
            "Redundancy":               r.redundancy or "N/A",
            "HA Replicas":              r.ha_replica_count or 0,
            "PAYG Hourly Cost USD":     r.payg_hourly_usd,
            "Avg Daily Running Hours":  r.avg_daily_running_hours,
            "Subscription":             r.subscription,
            "Provider":                 r.provider,
            "Is Orphaned":              r.is_orphaned,
        }
        for r in rows
    ]
    columns = ["Resource ID", "Resource Name", "Resource Type", "Resource State", "Region", "OS",
               "SKU", "Redundancy", "HA Replicas", "PAYG Hourly Cost USD", "Avg Daily Running Hours", "Subscription", "Provider", "Is Orphaned"]
    return pd.DataFrame(data, columns=columns)
