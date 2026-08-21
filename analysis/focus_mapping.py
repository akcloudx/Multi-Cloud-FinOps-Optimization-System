"""
analysis/focus_mapping.py — FOCUS (FinOps Open Cost & Usage Specification)
presentation layer for the Asset Inventory tab.

FOCUS is the FinOps Foundation's open, provider-neutral billing schema
(https://focus.finops.org/focus-specification/), currently referenced here
against spec v1.2. The app's internal inventory schema (see
data/inventory_loader.py) is a working schema shaped around the waterfall /
RI / Savings-Plan engines - it is FOCUS-*inspired* in spirit (normalized,
provider-neutral column names) but does not use FOCUS's actual column names
or controlled vocabularies. This module is the honest mapping layer: it
projects the internal schema onto real FOCUS column names for anyone who
wants to see the FOCUS view directly, without disturbing the internal schema
the rest of the app depends on.

Deliberately scoped to columns this app can populate faithfully from a point-
in-time resource snapshot: identity/categorization columns plus List/Billed
cost. FOCUS's ChargePeriodStart/End are genuinely time-series billing-ledger
concepts (one row per billing period) that don't exist yet in this table (the
closest real time-series data the app has is the daily waterfall/
reconciliation log, not this flat inventory) - rather than fabricate them,
they're left out and noted as a roadmap item below. Likewise EffectiveCost
(FOCUS's fully amortized, discount-inclusive cost) requires joining a
resource against its actual RI/Savings-Plan coverage over time; that math is
real and already implemented, but lives in the RI Coverage and Savings Plan
Analysis tabs against pooled commitments, not as a clean per-resource number
- so BilledCost is shown here equal to ListCost with an explicit caveat
rather than a guessed discount split.
"""

from __future__ import annotations

import pandas as pd

from pricing.commitment_pricing import MONTH_HOURS

FOCUS_SPEC_VERSION = "1.2"
FOCUS_SPEC_URL = "https://focus.finops.org/focus-specification/"

# FOCUS's ServiceCategory is a controlled vocabulary (highest-level grouping
# of a service by its core function). Only the categories this app's tracked
# resource types actually land in are used below.
_SERVICE_CATEGORY_MAP = {
    "Compute": "Compute",
    "App Service": "Web and Mobile",
    "Azure SQL Database": "Databases",
    "Azure SQL Managed Instance": "Databases",
    "Azure Database for MySQL": "Databases",
    "Azure Database for PostgreSQL": "Databases",
    "Azure Cosmos DB": "Databases",
    "Azure Cache for Redis": "Databases",
    "Azure Cache for Redis Enterprise": "Databases",
    "Azure Blob Storage": "Storage",
    "Azure Files": "Storage",
    "Azure Disk Storage": "Storage",
    "Azure Synapse Analytics": "Analytics",
    "Azure Databricks": "Analytics",
    "Azure Dedicated Host": "Compute",
    "Azure Container Instances": "Compute",
    "Azure Container Apps": "Compute",
    "Azure Spring Apps Enterprise": "Web and Mobile",
    "Azure DocumentDB": "Databases",
    "Azure Database Migration Service": "Databases",
    "Microsoft Fabric": "Analytics",
}

_PROVIDER_NAMES = {
    "Azure": "Microsoft Azure",
    "AWS": "Amazon Web Services",
}

# Reference table rendered in the UI expander - real FOCUS v1.2 column names
# and definitions, with a note on how (or whether) each is populated here.
FOCUS_COLUMN_DEFINITIONS = [
    ("ProviderName", "The company providing the service billed.", "Selected cloud platform."),
    ("InvoiceIssuerName", "The entity issuing the invoice (may differ from ProviderName for resellers).", "Same as ProviderName - no reseller/CSP layer tracked."),
    ("BillingAccountId", "Unique identifier of the overall billing account.", "Subscription / account ID."),
    ("ChargeCategory", "Highest-level classification of a charge: Usage, Purchase, Tax, Credit, or Adjustment.", "Always 'Usage' - Purchase (commitment buys), Tax, Credit and Adjustment rows aren't unified into this ledger yet."),
    ("ResourceId", "Provider-assigned unique identifier of the resource.", "Direct 1:1 mapping."),
    ("ResourceName", "Display name of the resource.", "Direct 1:1 mapping."),
    ("ResourceType", "The specific type of resource, e.g. an instance/SKU family.", "Mapped from the resource's SKU."),
    ("ServiceName", "The name of the service associated with the charge.", "Mapped from the internal Resource Type field."),
    ("ServiceCategory", "Highest-level classification of a service by its core function (controlled list).", "Mapped per-service - see _SERVICE_CATEGORY_MAP."),
    ("RegionId", "Provider-assigned identifier for the region/location of the resource.", "Direct 1:1 mapping."),
    ("PricingUnit", "The unit a resource is priced in.", "Always 'Hour' - every tracked resource here is hourly PAYG-priced."),
    ("ListUnitPrice", "The list (undiscounted) price per PricingUnit.", "PAYG hourly rate."),
    ("ListCost", "Cost before any discounts, at list price.", "PAYG hourly rate x (avg. daily running hours / 24) x 730 (monthly estimate, same MONTH_HOURS convention the RI/Savings Plan engines already use)."),
    ("BilledCost", "The actual invoiced cost, net of discounts, excluding amortization.", "Shown equal to ListCost at this per-resource view - real RI/Savings-Plan discount economics are computed separately (pooled, not per-resource) in the RI Coverage and Savings Plan Analysis tabs."),
]


def _map_service_category(resource_type: str) -> str:
    return _SERVICE_CATEGORY_MAP.get(resource_type, "Other")


def to_focus_view(df: pd.DataFrame, provider: str) -> pd.DataFrame:
    """Projects the internal inventory schema onto real FOCUS v1.2 column
    names. Expects the RAW inventory columns (Resource ID, Resource Name,
    Resource Type, Region, SKU, PAYG Hourly Cost USD, Avg Daily Running
    Hours, Subscription) - i.e. before any display-only formatting/renaming
    has been applied to them."""
    if df.empty:
        return pd.DataFrame(columns=[c for c, _, _ in FOCUS_COLUMN_DEFINITIONS])

    provider_name = _PROVIDER_NAMES.get(provider, provider)
    # MONTH_HOURS (730) matches the same constant the RI/Savings Plan
    # engines already annualize with (pricing/commitment_pricing.py) -
    # previously a flat "* 30" (720hrs), silently inconsistent with that
    # and with AWS's own pricing calculator (real gap caught live 2026-08-21).
    list_cost = df["PAYG Hourly Cost USD"] * (df["Avg Daily Running Hours"] / 24.0) * MONTH_HOURS

    return pd.DataFrame({
        "ProviderName": provider_name,
        "InvoiceIssuerName": provider_name,
        "BillingAccountId": df["Subscription"],
        "ChargeCategory": "Usage",
        "ResourceId": df["Resource ID"],
        "ResourceName": df["Resource Name"],
        "ResourceType": df["SKU"],
        "ServiceName": df["Resource Type"],
        "ServiceCategory": df["Resource Type"].apply(_map_service_category),
        "RegionId": df["Region"],
        "PricingUnit": "Hour",
        "ListUnitPrice": df["PAYG Hourly Cost USD"],
        "ListCost": list_cost,
        "BilledCost": list_cost,
    })
