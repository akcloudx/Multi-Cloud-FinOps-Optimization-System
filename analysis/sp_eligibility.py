"""
analysis/sp_eligibility.py
Real Azure Savings Plan (Compute + Databases) eligibility rules, per service.

Azure Savings Plans are a SEPARATE commitment product from Reserved Instances /
Reserved Capacity (see analysis/ri_eligibility.py), with their own eligibility
rules that sometimes DIFFER for the exact same resource. The clearest example:
Azure SQL Database Serverless is NOT eligible for Reserved Capacity, but IS
eligible for Savings Plan for Databases. Never assume RI eligibility implies
(or excludes) Savings Plan eligibility - they are independently researched
here from the official Microsoft Learn overview:
https://learn.microsoft.com/en-us/azure/cost-management-billing/savings-plan/savings-plan-overview

Savings plan for Compute (1-yr or 3-yr) covers:
  Azure Virtual Machines, Azure App Service (Premium v3/v4 & Isolated v2 only -
  same tier restriction as App Service reservations), Azure Functions Premium
  plan only (not Consumption/Y1), Azure Container Instances, Azure Dedicated
  Host, Azure Container Apps, Azure Spring Apps for Enterprise.

Savings plan for Databases (1-yr ONLY) covers:
  Azure SQL Database AND Azure SQL Elastic Pool (vCore purchasing model only -
  General Purpose, Business Critical, Hyperscale, and Serverless are ALL
  covered, unlike Reserved Capacity which excludes Serverless; Elastic Pool
  shares the exact same eligibility rule as Single Database - verified live
  that Azure prices vCore pools against the same catalog entries), Azure SQL
  Managed Instance, Azure Database for PostgreSQL Flexible Server (NOT legacy
  Single Server), Azure Database for MySQL Flexible Server (NOT legacy Single
  Server), Azure Cosmos DB provisioned throughput (NOT Serverless capacity
  mode).

  CORRECTED 2026-08: an earlier version of this file claimed legacy DTU-tier
  SQL Database was ALSO covered ("ALL compute tiers... including... legacy
  DTU"). That was wrong - re-verified directly against Microsoft's official
  savings-plan-overview page, whose explicit coverage list names "Azure SQL
  Database Hyperscale" and "Azure SQL Database serverless" individually but
  never mentions DTU, and confirmed independently (a third-party FinOps
  source states plainly: "the DTU-based purchasing model is excluded from
  Database Savings Plans... no Hybrid Benefit, no reserved capacity, no
  Database Savings Plans"). This also matches what was already verified live
  against the Retail Prices API: zero DTU-tier Consumption items carry any
  Savings Plan pricing data at all. DTU is genuinely NOT eligible here - not
  "eligible but unpriceable," an earlier, weaker fix this file briefly had.

NOT covered by ANY savings plan - Reserved Capacity is the only commitment
option for these, and only for the tiers documented in ri_eligibility.py:
  Azure Cache for Redis, Azure Blob Storage, Azure Files, Azure Disk Storage,
  Azure Synapse Analytics, Azure Databricks.
"""

import re
from typing import Tuple

_NO_SAVINGS_PLAN_TYPES = {
    "Azure Cache for Redis", "Azure Blob Storage", "Azure Files",
    "Azure Disk Storage", "Azure Synapse Analytics", "Azure Databricks",
}


def _app_service_or_functions_sp(sku: str) -> Tuple[bool, str]:
    # Functions Premium plans and regular App Service plans both live under
    # microsoft.web/serverfarms in Azure - our inventory can't tell them apart
    # by Resource Type, only by SKU (EP1-EP3 = Functions Premium/"Elastic
    # Premium"; Pxv3/Pxv4/Ixv2 = App Service Premium v3/v4/Isolated v2).
    s = (sku or "").upper()
    if not s or s == "N/A":
        return True, "Assumed Premium v3/v4, Isolated v2, or Functions Premium (SKU not captured)."
    if re.match(r"^P\d.*V[34]$", s):
        return True, "Premium v3/v4 App Service plan - eligible for Savings Plan for Compute."
    if s.startswith("I") and "V2" in s:
        return True, "Isolated v2 App Service plan - eligible for Savings Plan for Compute."
    if s.startswith("EP"):
        return True, "Functions Premium (Elastic Premium) plan - eligible for Savings Plan for Compute."
    if s in ("Y1", "Y2", "Y3"):
        return False, "Consumption plan (Y1) has no fixed hourly compute cost to discount - it's billed per-execution, not eligible for any commitment discount."
    return False, f"Savings Plan for Compute only covers Premium v3/v4, Isolated v2, and Functions Premium plans - '{sku}' is not one of those tiers."


def _sql_db_sp(sku: str) -> Tuple[bool, str]:
    # Savings Plan for Databases covers the vCore purchasing model's General
    # Purpose/Business Critical/Hyperscale/Serverless tiers - unlike Reserved
    # Capacity, there's no Serverless exclusion here. DTU is genuinely NOT
    # covered (corrected 2026-08 - see module docstring for the two
    # independent sources this was re-verified against: Microsoft's own
    # savings-plan-overview coverage list, and live Retail Prices API data
    # showing zero DTU Consumption items carry any Savings Plan pricing).
    s = (sku or "").upper()
    if s and s != "N/A":
        parts = s.split("_")
        is_dtu = "SERVERLESS" not in s and not any(p in ("GP", "BC", "HS") for p in parts)
        if is_dtu:
            return False, ("DTU-based purchasing model is not eligible for Savings Plan for Databases - only the vCore purchasing model "
                            "(General Purpose, Business Critical, Hyperscale, Serverless) is covered (verified against Microsoft's official "
                            "coverage list and live Retail Prices API data - migrating to vCore is the only way to access this discount).")
    return True, "Azure SQL Database (vCore purchasing model) is eligible for Savings Plan for Databases at any compute tier, including Serverless and Hyperscale."


def _sql_mi_sp(sku: str) -> Tuple[bool, str]:
    # SQL Managed Instance Hyperscale is a real, priceable tier (see
    # ri_eligibility.py's _sql_mi_eligibility for the 2026-08 correction),
    # but genuinely has no Savings Plan offering either - verified live,
    # zero of 87 Consumption items checked carry any savingsPlan pricing
    # data for this tier.
    s = (sku or "").upper()
    if s and s != "N/A" and any(p in ("HS", "HYPERSCALE") for p in s.split("_")):
        return False, "SQL Managed Instance Hyperscale has no Savings Plan offering - verified live against the Retail Prices API (zero Consumption items carry any savingsPlan pricing for this tier, though PAYG pricing is real and available)."
    return True, "Eligible for Savings Plan for Databases (compute cost)."


def _flexible_server_sp(sku: str) -> Tuple[bool, str]:
    # Our resource_type mapping doesn't currently distinguish Single Server
    # from Flexible Server (azure_conn/connector.py maps both ARM types to the
    # same string) - Single Server is legacy/being retired and excluded from
    # Savings Plan for Databases, but we can't detect it from SKU alone here.
    return True, "Assumed Flexible Server - eligible for Savings Plan for Databases. (Legacy Single Server is excluded, but isn't distinguishable from our current inventory data.)"


def _cosmos_db_sp(sku: str) -> Tuple[bool, str]:
    return True, "Assumed provisioned throughput (capacity mode not captured for this resource) - Serverless Cosmos DB usage is not eligible for Savings Plan for Databases."


_COMPUTE_SP_RULES = {
    "Compute":       lambda sku: (True, "All Azure VM series are eligible for Savings Plan for Compute."),
    "App Service":   _app_service_or_functions_sp,
    "Azure Functions": _app_service_or_functions_sp,
}

_DATABASE_SP_RULES = {
    "Azure SQL Database":            _sql_db_sp,
    "Azure SQL Elastic Pool":        _sql_db_sp,   # same vCore/DTU rule applies identically - see ri_eligibility.py
    "Azure SQL Managed Instance":    _sql_mi_sp,
    "Azure SQL Managed Instance Pool": _sql_mi_sp,   # same eligibility rule - see ri_eligibility.py's matching
                                                       # entry and pricing/sku_mapping.py for why this type is
                                                       # eligible but deliberately left unpriced for now.
    "Azure Database for MySQL":      _flexible_server_sp,
    "Azure Database for PostgreSQL": _flexible_server_sp,
    "Azure Cosmos DB":               _cosmos_db_sp,
}


def check_sp_eligibility(resource_type: str, sku: str) -> Tuple[bool, str]:
    """Returns (is_eligible, reason) for Azure Savings Plan coverage (Compute
    or Databases, whichever applies to this resource type). Types with no
    savings plan product at all (Redis, Storage, Files, Disk, Synapse,
    Databricks) always return False with an explanatory reason - Reserved
    Capacity is the only commitment option for them."""
    if resource_type in _NO_SAVINGS_PLAN_TYPES:
        return False, f"'{resource_type}' isn't covered by any Azure Savings Plan (Compute or Databases) - Reserved Capacity is the only commitment discount available for this service."

    fn = _COMPUTE_SP_RULES.get(resource_type) or _DATABASE_SP_RULES.get(resource_type)
    if fn is None:
        return True, f"No specific Savings Plan eligibility rule encoded yet for '{resource_type}' - treat this baseline cautiously."
    return fn(sku)
