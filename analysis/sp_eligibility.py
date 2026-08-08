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
  Azure SQL Database (ALL compute tiers, including Serverless, Hyperscale, and
  legacy DTU - unlike Reserved Capacity, there is no Serverless/DTU exclusion
  here), Azure SQL Managed Instance, Azure Database for PostgreSQL Flexible
  Server (NOT legacy Single Server), Azure Database for MySQL Flexible Server
  (NOT legacy Single Server), Azure Cosmos DB provisioned throughput (NOT
  Serverless capacity mode).

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
    # Unlike Reserved Capacity, Savings Plan for Databases covers ALL Azure
    # SQL Database compute tiers - Serverless and DTU-based included. It's a
    # dollar-denominated spend commitment, not a vCore-unit purchase, so the
    # purchasing-model restrictions that block RI don't apply here.
    return True, "Azure SQL Database is eligible for Savings Plan for Databases at any compute tier (including Serverless and DTU-based)."


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
    "Azure SQL Managed Instance":    lambda sku: (True, "Eligible for Savings Plan for Databases (compute cost)."),
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
