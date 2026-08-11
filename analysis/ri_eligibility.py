"""
analysis/ri_eligibility.py
Real Azure Reserved Instance / Reserved Capacity eligibility rules, per service.

Every rule below is sourced from official Microsoft Learn documentation (linked
inline). This exists because the waterfall/RI-coverage engine used to treat
every running resource as if it *could* be covered by a Reserved Instance and
flagged a "gap" the moment reserved_qty fell short of running_count - which is
wrong for services/tiers that Azure simply does not sell reservations for at
all (e.g. a Basic App Service plan, or a Serverless Azure SQL Database). This
module distinguishes "not yet covered" (a real gap, worth recommending a
purchase for) from "can never be covered" (wrong tier/SKU for this service -
recommending a purchase would be nonsensical).

Used by analysis.engine.reservation_analysis.
"""

import re
from typing import Tuple


def _vm_eligibility(sku: str) -> Tuple[bool, str]:
    # https://learn.microsoft.com/en-us/azure/virtual-machines/reserved-vm-instance-size-flexibility
    return True, "All Azure VM series support Reserved VM Instances (discount applies within the VM's instance size flexibility group)."


def _sql_db_eligibility(sku: str) -> Tuple[bool, str]:
    # https://learn.microsoft.com/en-us/azure/azure-sql/database/service-tiers-sql-database-vcore
    # Reserved capacity is ONLY available for the vCore Provisioned compute tier.
    # Real Azure SKU names: GP_Gen5_4 (provisioned), GP_S_Gen5_2 (serverless - "_S_"
    # marks it), Basic/S0-S12/P1-P15/PRS1-PRS6 (legacy DTU tier names).
    s = (sku or "").upper()
    if not s or s == "N/A":
        return True, "Assumed vCore Provisioned tier (SKU not captured for this resource)."
    parts = s.split("_")
    if "SERVERLESS" in s or "S" in parts:
        return False, "Serverless compute tier is not eligible for reserved capacity - only the vCore Provisioned compute tier qualifies."
    # DC-series (confidential-computing hardware) Reservations are excluded
    # even for otherwise-eligible tiers - verified live 2026-08 (real
    # calculator config, Business Critical/DC-series/40 vCore): the Azure
    # pricing calculator explicitly shows "1/3 year reserved option is not
    # available for your instance selection" - not just unmapped, genuinely
    # not offered. See pricing/sku_mapping.py's _plan_sql_dc_series for the
    # full evidence (a matching Retail API catalog entry exists but is a
    # stale 2021 legacy listing, not something actually purchasable today).
    if any(p.replace("-", "") in ("DC", "DCSERIES") for p in parts):
        return False, "DC-series hardware is not eligible for Reserved Capacity - Azure's own pricing calculator explicitly shows this as unavailable, confirmed live."
    if any(p in ("GP", "BC", "HS") for p in parts):
        return True, "vCore Provisioned compute tier - eligible for reserved capacity."
    return False, "DTU-based purchasing model is not eligible for reserved capacity - only the vCore purchasing model qualifies."


def _app_service_eligibility(sku: str) -> Tuple[bool, str]:
    # https://learn.microsoft.com/en-us/azure/cost-management-billing/reservations/reservation-discount-app-service
    # Only Premium v3 (Pv3/Pmv3), Premium v4, and Isolated v2 plans are eligible.
    # Free, Shared, Basic, Standard, and Premium v2 are NOT eligible for any
    # commitment instrument (reservation or savings plan).
    s = (sku or "").upper()
    if not s or s == "N/A":
        return True, "Assumed Premium v3/v4 or Isolated v2 (SKU not captured for this resource)."
    if re.match(r"^P\d.*V[34]$", s):
        return True, "Premium v3/v4 plan - eligible for App Service reservations."
    if s.startswith("I") and "V2" in s:
        return True, "Isolated v2 plan - eligible for App Service reservations (compute and ASE Stamp Fee are reserved separately)."
    return False, f"App Service reservations only cover Premium v3/v4 and Isolated v2 plans - '{sku}' is not one of those tiers."


def _redis_eligibility(sku: str) -> Tuple[bool, str]:
    # https://learn.microsoft.com/en-us/azure/azure-cache-for-redis/cache-reserved-pricing
    # Basic and Standard tiers are NOT eligible - only Premium, Enterprise, and
    # Enterprise Flash qualify.
    s = (sku or "").lower()
    if not s or s == "n/a":
        return True, "Assumed Premium/Enterprise tier (SKU not captured for this resource)."
    if "premium" in s or "enterprise" in s:
        return True, "Premium/Enterprise tier - eligible for Azure Cache for Redis reservations."
    return False, "Basic and Standard tiers are not eligible for Azure Cache for Redis reservations - only Premium, Enterprise, and Enterprise Flash qualify."


def _storage_eligibility(sku: str) -> Tuple[bool, str]:
    # https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-reserved-capacity
    # https://azure.microsoft.com/en-us/pricing/details/storage/blobs/
    # Eligible for Standard GPv2/Blob storage accounts (Block Blob / ADLS Gen2
    # data). Premium storage accounts are not eligible. Sold in 100 TB / 1 PB
    # blocks - see get_coverage_model() for why that makes per-resource gap
    # tracking meaningless regardless of SKU eligibility.
    s = (sku or "").lower()
    if "premium" in s:
        return False, "Storage reserved capacity only covers Standard GPv2/Blob storage accounts - Premium storage accounts are not eligible."
    return True, "Standard GPv2/Blob storage account - eligible for Storage reserved capacity, but only sold in 100 TB / 1 PB blocks (Block Blob / ADLS Gen2 data)."


def _disk_eligibility(sku: str) -> Tuple[bool, str]:
    # https://learn.microsoft.com/en-us/azure/virtual-machines/disks-reserved-capacity
    # Only Premium SSD disks at size P30 and larger (P30-P80) are eligible.
    # Standard SSD/HDD, Ultra Disk, and Premium SSD v2 are not eligible.
    s = (sku or "").upper()
    if not s or s == "N/A":
        return True, "Assumed Premium SSD P30+ (exact disk SKU/size not captured for this resource)."
    if "ULTRA" in s or "V2" in s:
        return False, "Ultra Disk and Premium SSD v2 are not eligible for Azure Disk Storage reservations."
    if "STANDARD" in s and "PREMIUM" not in s:
        return False, "Standard SSD/HDD disks are not eligible - only Premium SSD disks at size P30 and larger qualify."
    return True, "Premium SSD - eligible for Azure Disk Storage reservations if size is P30 or larger."


_RULES = {
    "Compute":                       _vm_eligibility,
    "Azure SQL Database":            _sql_db_eligibility,
    "Azure SQL Elastic Pool":        _sql_db_eligibility,   # same vCore/DTU-tier rule applies identically -
                                                              # verified live Azure prices vCore pools against
                                                              # the same Reservation catalog entries as Single
                                                              # Database (see pricing/sku_mapping.py).
    "Azure SQL Managed Instance":    lambda sku: (True, "SQL Managed Instance is always vCore-based - reservations cover compute cost (not storage)."),
    "Azure Database for MySQL":      lambda sku: (True, "Flexible Server is eligible for reserved capacity. (Legacy Single Server no longer accepts new reservations.)"),
    "Azure Database for PostgreSQL": lambda sku: (True, "Flexible Server is eligible for reserved capacity. (Legacy Single Server no longer accepts new reservations.)"),
    "Azure Cosmos DB":               lambda sku: (True, "Assumed provisioned throughput (capacity mode not captured for this resource) - Serverless Cosmos DB accounts are not eligible."),
    "Azure Blob Storage":            _storage_eligibility,
    "Azure Cache for Redis":         _redis_eligibility,
    "Azure Synapse Analytics":       lambda sku: (True, "Assumed Dedicated SQL Pool (cDWU) - a Serverless SQL Pool alone is not eligible for classic reserved capacity."),
    "Azure Databricks":              lambda sku: (True, "Databricks Commit Units (DBCU) prepurchase apply as a pooled discount across all workloads/tiers, not a per-resource reservation match."),
    "App Service":                   _app_service_eligibility,
    "Azure Disk Storage":            _disk_eligibility,
    "Azure Files":                   lambda sku: (True, "Eligible for Azure Files reservations, but only sold in 10 TiB / 100 TiB blocks (Hot/Cool tier capacity)."),
}


def check_eligibility(resource_type: str, sku: str) -> Tuple[bool, str]:
    """Returns (is_eligible, reason). Resource types with no rule encoded yet
    default to eligible with a generic note - we'd rather under-flag than
    assert a wrong exclusion for a service we haven't researched."""
    fn = _RULES.get(resource_type)
    if fn is None:
        return True, f"No specific Azure reservation eligibility rule encoded yet for '{resource_type}' - treat this gap number cautiously."
    return fn(sku)


# Three fundamentally different ways Azure applies a reservation discount:
#
# "instance"     - bought as a literal count of units (N VMs / N App Service
#                   plan instances / N Redis nodes / N disks). running_count -
#                   reserved_qty is a directly meaningful gap: "buy N more of
#                   this exact SKU."
#
# "capacity"     - bought as a pooled amount of capacity/throughput (RU/s,
#                   DBCU, cDWU, vCore-hours) applied AUTOMATICALLY across ALL
#                   matching resources in scope, at increments small enough
#                   to plausibly match one resource's real usage. A "gap" is
#                   a rough signal ("this resource type isn't covered at
#                   all"), not a literal instance count to buy.
#
# "unmeasurable" - same pooled/automatic application as "capacity", but sold
#                   in minimum blocks vastly larger than a single resource's
#                   typical footprint, and this dashboard has no way to know
#                   the actual usage volume needed to size a purchase at all
#                   (we track resource *existence*, not GB/TiB stored). A
#                   resource-count "gap" here isn't just approximate, it's
#                   meaningless - showing one would actively mislead.
#
# Sources:
#   Storage:   https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-reserved-capacity
#              purchased in blocks of 100 TB or 1 PB
#              (https://azure.microsoft.com/en-us/pricing/details/storage/blobs/)
#              - "cannot be limited to a specific storage account... applies
#              to any... resources that match the terms of the reservation."
#              A typical storage account holds nowhere near 100 TB, so a
#              per-resource "gap" is not an actionable number here.
#   Files:     https://learn.microsoft.com/en-us/azure/storage/files/files-reserve-capacity
#              purchased in blocks of 10 TiB or 100 TiB - same problem.
#   Cosmos DB: https://learn.microsoft.com/en-us/azure/cosmos-db/reserved-capacity
#              RU/s increments are small enough to plausibly match one
#              account's provisioned throughput - kept in "capacity", not
#              "unmeasurable".
#   SQL DB/MI: https://learn.microsoft.com/en-us/azure/azure-sql/database/reservations-discount-overview
#              vCore-hour increments correlate reasonably with one database's
#              size - "applied automatically... not allocated per individual
#              database," but not absurdly oversized like Storage/Files.
#   Databricks/Synapse: pooled DBCU/cDWU consumption, same "capacity" pattern.
_CAPACITY_POOLED_TYPES = {
    "Azure Cosmos DB", "Azure Databricks", "Azure Synapse Analytics",
    "Azure SQL Database", "Azure SQL Managed Instance", "Azure SQL Elastic Pool",
}
_UNMEASURABLE_TYPES = {"Azure Blob Storage", "Azure Files"}


def get_coverage_model(resource_type: str) -> str:
    """Returns "unmeasurable" (pooled, but minimum purchase size dwarfs a
    single resource and we don't track actual usage volume - never show a
    resource-count gap), "capacity" (pooled but at a scale that roughly
    tracks one resource - gap is a rough signal), or "instance" (bought per
    literal unit - gap is directly actionable)."""
    if resource_type in _UNMEASURABLE_TYPES:
        return "unmeasurable"
    if resource_type in _CAPACITY_POOLED_TYPES:
        return "capacity"
    return "instance"
