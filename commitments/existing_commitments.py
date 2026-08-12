"""
commitments/existing_commitments.py
Loads active RI and Savings Plan contract positions from SQLite.

TWO SAVINGS PLAN TYPES (per Azure policy):
  - Savings Plan for Compute   : 1-year or 3-year. Covers VMs, App Service,
                                  Functions Premium, Container Instances, etc.
  - Savings Plan for Databases : 1-year ONLY. Covers SQL DB, SQL MI,
                                  PostgreSQL, MySQL, Cosmos DB, etc.

REAL AZURE EQUIVALENT:
  - Reservations : azure-mgmt-consumption → reservationSummaries / reservationDetails
  - Savings Plans: Cost Management BenefitUtilizationSummaries API
                   (Microsoft.CostManagement/benefitUtilizationSummaries)
"""

import pandas as pd
from sqlalchemy.orm import Session
from db.schema import init_db, get_engine, Commitment

_COMPUTE_SP_TYPE  = "Savings Plan for Compute"
_DATABASE_SP_TYPE = "Savings Plan for Databases"


def get_all_commitments(provider: str = "Azure", tenant_id=None) -> pd.DataFrame:
    """Returns active commitment contracts (RI + Savings Plans) for provider.
    tenant_id=None (default) returns demo/seed commitments only. Live tenants
    don't have fetched RI/SP data yet (Resource Graph only covers inventory),
    so a real tenant_id correctly returns empty rather than falling back to demo."""
    engine = get_engine(provider)
    init_db(provider)
    with Session(engine) as session:
        query = session.query(Commitment)
        if tenant_id is None:
            query = query.filter(Commitment.tenant_id.is_(None))
        else:
            query = query.filter(Commitment.tenant_id == tenant_id)
        rows = query.all()
    data = [
        {
            "commitment_id":         r.commitment_id,
            "commitment_type":       r.commitment_type,
            "scope_sku":             r.scope_sku,
            "scope_resource_type":   r.scope_resource_type,
            "scope_region":          r.scope_region,
            "scope_os":              r.scope_os,
            "scope_redundancy":      r.scope_redundancy or "N/A",
            "hourly_usd_commitment": r.hourly_usd_commitment,
            "reserved_qty":          r.reserved_qty,
            "term":                  r.term,
            "expiry_date":           r.expiry_date,
            "provider":              r.provider,
        }
        for r in rows
    ]
    columns = ["commitment_id", "commitment_type", "scope_sku", "scope_resource_type", "scope_region", "scope_os", "scope_redundancy",
               "hourly_usd_commitment", "reserved_qty", "term", "expiry_date", "provider"]
    return pd.DataFrame(data, columns=columns)


def get_existing_savings_plans(provider: str = "Azure", tenant_id=None) -> pd.DataFrame:
    """Returns ALL Savings Plan commitments."""
    df = get_all_commitments(provider, tenant_id)
    sp_types = [_COMPUTE_SP_TYPE, _DATABASE_SP_TYPE, "Compute Savings Plan", "EC2 Instance Savings Plan"]
    mask = df["commitment_type"].isin(sp_types)
    return df[mask].reset_index(drop=True)


def get_compute_savings_plans(provider: str = "Azure", tenant_id=None) -> pd.DataFrame:
    """Returns Compute Savings Plan commitments."""
    df = get_all_commitments(provider, tenant_id)
    mask = df["commitment_type"].isin([_COMPUTE_SP_TYPE, "Compute Savings Plan"])
    return df[mask].reset_index(drop=True)


def get_database_savings_plans(provider: str = "Azure", tenant_id=None) -> pd.DataFrame:
    """Returns Database / EC2 Instance Savings Plan commitments."""
    df = get_all_commitments(provider, tenant_id)
    mask = df["commitment_type"].isin([_DATABASE_SP_TYPE, "EC2 Instance Savings Plan"])
    return df[mask].reset_index(drop=True)


def get_existing_reservations(provider: str = "Azure", tenant_id=None) -> pd.DataFrame:
    """Returns Reserved Instance / Reserved Capacity commitments."""
    df = get_all_commitments(provider, tenant_id)
    mask = df["commitment_type"].isin(["Reserved Instance", "Reserved Capacity"])
    return df[mask].reset_index(drop=True)


def get_total_compute_sp_hr(provider: str = "Azure", tenant_id=None) -> float:
    """Total Savings Plan for Compute $/hr pool."""
    df = get_compute_savings_plans(provider, tenant_id)
    return float(df["hourly_usd_commitment"].sum()) if not df.empty else 0.0


def get_total_database_sp_hr(provider: str = "Azure", tenant_id=None) -> float:
    """Total Savings Plan for Databases $/hr pool."""
    df = get_database_savings_plans(provider, tenant_id)
    return float(df["hourly_usd_commitment"].sum()) if not df.empty else 0.0


def get_total_sp_commitment_hr(provider: str = "Azure", tenant_id=None) -> float:
    """Total $/hr across all Savings Plans."""
    return get_total_compute_sp_hr(provider, tenant_id) + get_total_database_sp_hr(provider, tenant_id)

