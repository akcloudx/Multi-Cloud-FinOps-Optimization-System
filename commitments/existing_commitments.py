"""
commitments/existing_commitments.py
Loads active RI and Savings Plan contract positions from SQLite.

TWO SAVINGS PLAN TYPES (per Azure policy):
  - Savings Plan for Compute   : 1-year or 3-year. Covers VMs, App Service,
                                  Functions Premium, Container Instances, etc.
  - Savings Plan for Databases : 1-year ONLY. Covers SQL DB, SQL MI,
                                  PostgreSQL, MySQL, Cosmos DB, etc.

REAL AZURE EQUIVALENT (live tenant fetch - see azure_conn/connector.py's
fetch_live_reservations/fetch_live_savings_plans and pricing/
commitment_mapping.py for the derivation into this table):
  - Reservations : azure-mgmt-reservations -> Microsoft.Capacity/reservations
                   "List All" (embeds utilization directly)
  - Savings Plans: azure-mgmt-billingbenefits -> Microsoft.BillingBenefits/
                   savingsPlans "List All" (embeds utilization directly)
"""

import pandas as pd
from sqlalchemy.orm import Session
from db.schema import init_db, get_engine, Commitment

_COMPUTE_SP_TYPE  = "Savings Plan for Compute"
_DATABASE_SP_TYPE = "Savings Plan for Databases"

# AWS side (pricing/aws_commitment_mapping.py). "EC2 Instance Savings Plan"
# used to sit in the DATABASE bucket below as a placeholder, written before
# anyone confirmed real AWS "Database Savings Plans" existed - corrected
# 2026-08-22 after AWS's own FAQ (https://aws.amazon.com/savingsplans/faqs/)
# confirmed Database Savings Plans are a real product covering Aurora/RDS/
# DynamoDB/ElastiCache/DocumentDB, and share the same "1-year term ONLY"
# constraint this file's own docstring above already describes for Azure's
# Savings Plan for Databases - genuinely analogous products, not just
# similarly named. EC2 Instance Savings Plans are purely EC2/compute (never
# covered databases at any point) and now correctly bucket as Compute.
_AWS_COMPUTE_SP_TYPES  = ["Compute Savings Plan", "EC2 Instance Savings Plan"]
_AWS_DATABASE_SP_TYPES = ["Database Savings Plan"]
# SageMaker Savings Plans - a genuinely separate, first-class AWS SP type
# (boto3 savingsplans client's savingsPlanType enum: 'Compute'|'EC2Instance'|
# 'SageMaker'|'Database'), added 2026-08-23. AWS-only - Azure has no
# equivalent product, so there is no corresponding Azure bucket here.
_AWS_SAGEMAKER_SP_TYPES = ["SageMaker Savings Plan"]


def _sp_type_list() -> list:
    return [_COMPUTE_SP_TYPE, _DATABASE_SP_TYPE] + _AWS_COMPUTE_SP_TYPES + _AWS_DATABASE_SP_TYPES + _AWS_SAGEMAKER_SP_TYPES


def get_all_commitments(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> pd.DataFrame:
    """Returns active commitment contracts (RI + Savings Plans) for the given
    (provider, mode) scope. tenant_id only matters within "live" mode (which
    connected tenant's commitments to read) - live tenants don't have fetched
    RI/SP data yet (Resource Graph only covers inventory), so a real tenant_id
    correctly returns empty rather than falling back to demo data."""
    engine = get_engine(provider, mode)
    init_db(provider, mode)
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
            "is_inferred_mapping":   bool(r.is_inferred_mapping),
            "mapping_note":          r.mapping_note,
            "offering_class":        r.offering_class,
        }
        for r in rows
    ]
    columns = ["commitment_id", "commitment_type", "scope_sku", "scope_resource_type", "scope_region", "scope_os", "scope_redundancy",
               "hourly_usd_commitment", "reserved_qty", "term", "expiry_date", "provider", "is_inferred_mapping", "mapping_note", "offering_class"]
    return pd.DataFrame(data, columns=columns)


def get_existing_savings_plans(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> pd.DataFrame:
    """Returns ALL Savings Plan commitments."""
    df = get_all_commitments(provider, mode, tenant_id)
    mask = df["commitment_type"].isin(_sp_type_list())
    return df[mask].reset_index(drop=True)


def get_compute_savings_plans(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> pd.DataFrame:
    """Returns Compute Savings Plan commitments."""
    df = get_all_commitments(provider, mode, tenant_id)
    mask = df["commitment_type"].isin([_COMPUTE_SP_TYPE] + _AWS_COMPUTE_SP_TYPES)
    return df[mask].reset_index(drop=True)


def get_database_savings_plans(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> pd.DataFrame:
    """Returns Database Savings Plan commitments."""
    df = get_all_commitments(provider, mode, tenant_id)
    mask = df["commitment_type"].isin([_DATABASE_SP_TYPE] + _AWS_DATABASE_SP_TYPES)
    return df[mask].reset_index(drop=True)


def get_sagemaker_savings_plans(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> pd.DataFrame:
    """Returns SageMaker Savings Plan commitments. AWS-only - always empty for Azure."""
    df = get_all_commitments(provider, mode, tenant_id)
    mask = df["commitment_type"].isin(_AWS_SAGEMAKER_SP_TYPES)
    return df[mask].reset_index(drop=True)


def get_existing_reservations(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> pd.DataFrame:
    """Returns Reserved Instance / Reserved Capacity commitments."""
    df = get_all_commitments(provider, mode, tenant_id)
    mask = df["commitment_type"].isin(["Reserved Instance", "Reserved Capacity"])
    return df[mask].reset_index(drop=True)


def get_total_compute_sp_hr(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> float:
    """Total Savings Plan for Compute $/hr pool."""
    df = get_compute_savings_plans(provider, mode, tenant_id)
    return float(df["hourly_usd_commitment"].sum()) if not df.empty else 0.0


def get_total_database_sp_hr(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> float:
    """Total Savings Plan for Databases $/hr pool."""
    df = get_database_savings_plans(provider, mode, tenant_id)
    return float(df["hourly_usd_commitment"].sum()) if not df.empty else 0.0


def get_total_sagemaker_sp_hr(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> float:
    """Total SageMaker Savings Plan $/hr pool. AWS-only - always 0.0 for Azure."""
    df = get_sagemaker_savings_plans(provider, mode, tenant_id)
    return float(df["hourly_usd_commitment"].sum()) if not df.empty else 0.0


def get_total_sp_commitment_hr(provider: str = "Azure", mode: str = "demo", tenant_id=None) -> float:
    """Total $/hr across all Savings Plans."""
    return (get_total_compute_sp_hr(provider, mode, tenant_id)
            + get_total_database_sp_hr(provider, mode, tenant_id)
            + get_total_sagemaker_sp_hr(provider, mode, tenant_id))

