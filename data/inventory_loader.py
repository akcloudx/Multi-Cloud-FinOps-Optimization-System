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

REAL AZURE EQUIVALENT (when you move off mock data):
  Azure Resource Graph query, e.g.:
    Resources
    | where type in (
        'microsoft.compute/virtualmachines',
        'microsoft.sql/servers/databases')
    | extend powerState = tostring(properties.extended.instanceView.powerState.displayStatus)
  Use azure-mgmt-resourcegraph SDK, authenticated via Workload Identity Federation.
"""

import pandas as pd
from sqlalchemy.orm import Session
from db.schema import init_db, get_engine, CloudInventory


def get_compute_inventory(provider: str = "Azure", tenant_id=None) -> pd.DataFrame:
    """
    Returns the normalized inventory DataFrame from SQL DB for the specified provider.
    Columns match FOCUS schema for multi-cloud compatibility.

    tenant_id: None (default) returns demo/seed rows only (tenant_id IS NULL) -
    it never mixes in live-ingested data. Pass an active tenant's DB id to get
    that tenant's live inventory instead. There is no "all rows" mode - demo and
    live data must never be shown together.
    """
    engine = get_engine(provider)
    init_db(provider)

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
            "PAYG Hourly Cost USD":     r.payg_hourly_usd,
            "Avg Daily Running Hours":  r.avg_daily_running_hours,
            "Subscription":             r.subscription,
            "Provider":                 r.provider,
            "Is Orphaned":              r.is_orphaned,
        }
        for r in rows
    ]
    columns = ["Resource ID", "Resource Name", "Resource Type", "Resource State", "Region", "OS",
               "SKU", "PAYG Hourly Cost USD", "Avg Daily Running Hours", "Subscription", "Provider", "Is Orphaned"]
    return pd.DataFrame(data, columns=columns)
