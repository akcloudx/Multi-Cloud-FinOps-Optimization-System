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
  Host, Azure Container Apps (Dedicated workload profile only - the Consumption
  profile bills per-second with no fixed hourly rate to discount), Azure
  Spring Apps for Enterprise (the "...Enterprise" product specifically - a
  sibling "Azure Spring Apps" product shares meter names but carries no real
  savingsPlan data).

Savings plan for Databases (1-yr ONLY) covers:
  Azure SQL Database AND Azure SQL Elastic Pool (vCore purchasing model only -
  General Purpose, Business Critical, Hyperscale, and Serverless are ALL
  covered, unlike Reserved Capacity which excludes Serverless; Elastic Pool
  shares the exact same eligibility rule as Single Database - verified live
  that Azure prices vCore pools against the same catalog entries), Azure SQL
  Managed Instance, Azure Database for PostgreSQL Flexible Server (NOT legacy
  Single Server), Azure Database for MySQL Flexible Server (NOT legacy Single
  Server), Azure Cosmos DB provisioned throughput (NOT Serverless capacity
  mode), Azure Database Migration Service (all three tiers - Basic/General
  Purpose/Premium), Azure DocumentDB (eligible per Microsoft's official
  coverage list and real live pricing/savingsPlan data confirmed - but this
  app deliberately doesn't price it yet, see pricing/sku_mapping.py for the
  real, disclosed reason).

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
  Azure Cache for Redis, Azure Cache for Redis Enterprise, Azure Blob
  Storage, Azure Files, Azure Disk Storage, Azure Synapse Analytics,
  Azure Databricks, Azure Data Factory, Azure Data Explorer, Azure Backup
  Storage, Azure NetApp Files, Microsoft Fabric.

Explicitly OUT OF SCOPE, not modeled at all (2026-08 decision, see
[[project-sku-mapping-per-service]]): SQL Server on Azure Virtual Machines
hourly licenses and SQL Server enabled by Azure Arc hourly licenses - both
are real Savings-Plan-for-Databases-eligible line items per Microsoft's
official list, but conflict with this app's existing license-cost-exclusion
design (and Arc would need a whole new hybrid-resource ingestion path).
"""

import re
from typing import Tuple

_NO_SAVINGS_PLAN_TYPES = {
    "Azure Cache for Redis", "Azure Cache for Redis Enterprise", "Azure Blob Storage", "Azure Files",
    "Azure Disk Storage", "Azure Synapse Analytics", "Azure Databricks",
    "Microsoft Fabric",  # verified live 2026-08 - zero savingsPlan data on any Fabric Capacity meter, not on either official SP coverage list either
    # Reserved-Capacity-only services added during the 2026-08 RI-coverage audit -
    # none of these are on either official Savings Plan coverage list, and none
    # have a per-resource SKU this app could price a Savings Plan against anyway
    # (see ri_eligibility.py's _UNMEASURABLE_TYPES for why).
    "Azure Data Factory", "Azure Data Explorer", "Azure Backup Storage", "Azure NetApp Files",
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
    # Blanket True is correct for every Flexible Server tier including
    # Burstable - verified live (2026-08, via this app's actual pinned
    # Retail Prices API version) that Burstable genuinely carries real
    # savingsPlan pricing data, for both PostgreSQL and MySQL, even though
    # the pricing calculator's UI doesn't currently show a Savings Options
    # panel for Burstable at all (see pricing/sku_mapping.py's
    # _plan_postgresql/_plan_mysql for the same finding on the pricing
    # side - this is a real calculator-vs-live-API disagreement, resolved
    # in favor of the live API per the same precedent already set for SQL
    # Managed Instance Hyperscale).
    return True, "Eligible for Savings Plan for Databases (compute cost). (Legacy Single Server is excluded, but isn't distinguishable from our current inventory data.)"


def _cosmos_db_sp(sku: str) -> Tuple[bool, str]:
    # SKU convention: "{CapacityMode}_{ServiceTier}_{RUs}" or bare
    # "Serverless" - see pricing/sku_mapping.py. Serverless has no hourly
    # rate for a $/hr Savings Plan commitment to discount against (it bills
    # per-request, $/million RU) - not eligible. Provisioned throughput
    # (Standard/Autoscale) genuinely is eligible per Azure's own pricing
    # page (a real, current 12% 1yr discount advertised), even though the
    # Retail Prices API's savingsPlan field doesn't publish it on the
    # RU/s meter itself - see pricing/sku_mapping.py's comment on
    # _plan_cosmos_db for that priceability-vs-eligibility distinction.
    if (sku or "").split("_")[0] == "Serverless":
        return False, "Serverless Cosmos DB bills per-request, not $/hr - no hourly commitment for a Savings Plan to discount."
    return True, "Provisioned throughput (Standard or Autoscale) - eligible for Savings Plan for Databases."


_VM_FAMILY_RE = re.compile(r"^(?:Basic|Standard)_([A-Za-z]+)")
# Families confirmed live 2026-08 to carry ZERO savingsPlan entries anywhere
# in the Retail Prices API - same full-catalog scan as ri_eligibility.py's
# _VM_FAMILIES_NO_RI (~12,000 rows, all VM Consumption items in eastus).
# Basic-tier A-series specifically (Standard-tier A-series retains some
# Savings Plan coverage - confirmed live, 7 of 113 Standard_A rows carry a
# savingsPlan, only Basic_A had literally zero). G/GS: same legacy series as
# the RI exclusion. NM/PB: newer specialized series with no Savings Plan
# found at all (NM does have some real Reservation rows though - RI and SP
# eligibility genuinely differ for this one family, which is why they're
# tracked as separate sets rather than one shared list).
_VM_FAMILIES_NO_SP_STANDARD = {"G", "GS", "NM", "PB"}


def _vm_sp(sku: str) -> Tuple[bool, str]:
    s = (sku or "").strip()
    if not s or s == "N/A":
        return True, "Assumed a mainstream VM series with Savings Plan for Compute support (SKU not captured for this resource)."
    m = _VM_FAMILY_RE.match(s)
    family = m.group(1) if m else ""
    if s.upper().startswith("BASIC_") and family == "A":
        return False, "Basic-tier A-series VMs have no Savings Plan offering - verified live against the full Retail Prices API catalog (zero savingsPlan entries for Basic_A, though Standard-tier A-series retains some coverage)."
    if family in _VM_FAMILIES_NO_SP_STANDARD:
        return False, f"'{family}'-series VMs have no Savings Plan for Compute offering - verified live against the full Retail Prices API catalog (zero savingsPlan entries for this family)."
    return True, "Savings Plan for Compute is available for this VM series."


def _container_apps_sp(sku: str) -> Tuple[bool, str]:
    # Consumption profile bills per-second/per-request (scale-to-zero
    # serverless) with no fixed hourly rate to discount - the same shape as
    # Functions' Y1 Consumption plan (see _app_service_or_functions_sp), and
    # this app's pricing/sku_mapping.py can't and doesn't price it for
    # exactly that reason. Dedicated profile (the only mapped shape) bills
    # per fixed-size node and IS real Savings-Plan-eligible - verified live
    # 2026-08 (both the vCPU and memory meters carry real savingsPlan data,
    # despite Microsoft Learn's own overview page just listing "Azure
    # Container Apps" as one line with no profile-level distinction).
    s = (sku or "")
    if s.startswith("Dedicated_"):
        return True, "Dedicated workload profile - eligible for Savings Plan for Compute, verified live on both the vCPU and memory meters."
    if s == "Consumption" or not s or s == "N/A":
        return False, "Consumption workload profile bills per-second/per-request with no fixed hourly rate - not eligible for any commitment discount (same reasoning as Azure Functions' Y1 Consumption plan)."
    return False, f"'{sku}' isn't a workload profile shape this app recognizes for Savings Plan eligibility."


_COMPUTE_SP_RULES = {
    "Compute":       _vm_sp,
    "App Service":   _app_service_or_functions_sp,
    "Azure Functions": _app_service_or_functions_sp,
    "Azure Dedicated Host": lambda sku: (True, "Eligible for Savings Plan for Compute - verified live, real savingsPlan pricing exists on the same 'Virtual Machines' service catalog Dedicated Host pricing lives under."),
    "Azure Container Instances": lambda sku: (True, "Eligible for Savings Plan for Compute - verified live on both the vCPU and memory meters. Not eligible for Reserved Capacity (see ri_eligibility.py) - Azure genuinely sells no reservation for this service."),
    "Azure Container Apps": _container_apps_sp,
    "Azure Spring Apps Enterprise": lambda sku: (True, "Eligible for Savings Plan for Compute - verified live on the Enterprise vCPU and Memory Group Duration meter, specifically on the 'Azure Spring Apps Enterprise' product (SP:True) - the sibling 'Azure Spring Apps' product shares the same meter name but carries no savingsPlan data."),
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
    "Azure DocumentDB":              lambda sku: (True, "Eligible for Savings Plan for Databases per Microsoft's official coverage list - real Retail API pricing/savingsPlan data confirmed live, but this app deliberately doesn't price it yet (see pricing/sku_mapping.py for the real, disclosed reason: an unconfirmed M-tier-to-vCore mapping and an unconfirmed Coordinator Node billing threshold)."),
    "Azure Database Migration Service": lambda sku: (True, "Eligible for Savings Plan for Databases - verified live, real savingsPlan data exists on all three tiers (Basic/General Purpose/Premium)."),
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
