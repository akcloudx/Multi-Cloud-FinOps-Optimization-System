"""
analysis/ri_eligibility.py
Real Reserved Instance / Reserved Capacity eligibility rules, per resource
type - mostly Azure (sourced from official Microsoft Learn documentation,
linked inline), plus a handful of AWS entries (sourced from boto3's own
service models - see the "AWS additions" block in _RULES below) for AWS
resource types that turned out to have no Reserved Instance product at all.

This exists because the waterfall/RI-coverage engine (shared by both
providers) used to treat every running resource as if it *could* be covered
by a Reserved Instance and flagged a "gap" the moment reserved_qty fell short
of running_count - which is wrong for services/tiers that don't sell
reservations for at all (e.g. a Basic App Service plan or a Serverless Azure
SQL Database on the Azure side; Amazon DocumentDB or AWS Fargate on the AWS
side). This module distinguishes "not yet covered" (a real gap, worth
recommending a purchase for) from "can never be covered" (wrong tier/SKU, or
no RI product exists for this service at all - recommending a purchase would
be nonsensical).

Used by analysis.engine.reservation_analysis.
"""

import re
from typing import Tuple


_VM_FAMILY_RE = re.compile(r"^(?:Basic|Standard)_([A-Za-z]+)")
# Families confirmed live 2026-08 to have ZERO Reservation entries anywhere
# in the Retail Prices API - a full-catalog scan (all VM Consumption AND
# Reservation items in eastus, ~12,000 rows across both price types), not a
# narrow single-query guess (see the DC-series/MI-Hyperscale correction
# earlier this session for why that distinction matters). A: legacy
# entry-level series (both Basic_A and Standard_A tiers - spot-checked
# Basic_A1/G2 directly: current effectiveStartDate, i.e. still an active
# current meter, genuinely just never had RI enabled). G/GS: legacy 2015-era
# series, superseded by newer families, no RI ever added. DS: legacy
# Premium-Storage-suffix naming (mostly superseded by newer Dsv5-style
# capacity-suffix naming, which DOES have RI - this only affects the old
# "DS2_v2"-style names specifically). NP: FPGA-accelerated series, no RI.
# H (base, NOT its HB/HC/HX variants - those DO have RI): older H-series
# generation. HC: has real Consumption + Savings Plan, but no Reservation.
# PB: newer AMD-based series, neither RI nor SP found.
_VM_FAMILIES_NO_RI = {"A", "G", "GS", "DS", "NP", "H", "HC", "PB"}


def _vm_eligibility(sku: str) -> Tuple[bool, str]:
    # https://learn.microsoft.com/en-us/azure/virtual-machines/reserved-vm-instance-size-flexibility
    # NOT a blanket "all VM series" claim (that was the old, wrong version of
    # this function) - see _VM_FAMILIES_NO_RI above for the live evidence.
    s = (sku or "").strip()
    if not s or s == "N/A":
        return True, "Assumed a mainstream VM series with Reserved Instance support (SKU not captured for this resource)."
    m = _VM_FAMILY_RE.match(s)
    family = m.group(1) if m else ""
    if family in _VM_FAMILIES_NO_RI:
        return False, f"'{family}'-series VMs have no Reserved Instance offering - verified live against the full Retail Prices API catalog (zero Reservation entries for this family, confirmed as a current/active series, not a stale listing)."
    return True, "Reserved VM Instances are available for this VM series (discount applies within the VM's instance size flexibility group)."


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
    # DC-series (confidential-computing hardware) Reservation eligibility is
    # TIER-SPECIFIC, not a blanket exclusion - corrected 2026-08 after
    # systematically re-checking every tier via the real Azure pricing
    # calculator (browser-automated dropdown enumeration, not another manual
    # screenshot): General Purpose + DC-series genuinely HAS a current
    # Reservation offering (calculator shows ~35%/~55% off as available);
    # only Business Critical and Hyperscale + DC-series show "1/3 year
    # reserved option is not available for your instance selection". An
    # earlier pass here wrongly blocked ALL DC-series regardless of tier,
    # based only on the Business Critical case. See pricing/sku_mapping.py's
    # _plan_sql_dc_series for the full evidence and correction.
    if any(p.replace("-", "") in ("DC", "DCSERIES") for p in parts) and any(p in ("BC", "HS") for p in parts):
        return False, "DC-series hardware is not eligible for Reserved Capacity for this tier - Azure's own pricing calculator explicitly shows this as unavailable for Business Critical/Hyperscale (General Purpose + DC-series IS eligible), confirmed live."
    if any(p in ("GP", "BC", "HS") for p in parts):
        return True, "vCore Provisioned compute tier - eligible for reserved capacity."
    return False, "DTU-based purchasing model is not eligible for reserved capacity - only the vCore purchasing model qualifies."


def _sql_mi_eligibility(sku: str) -> Tuple[bool, str]:
    # SQL Managed Instance Hyperscale IS a real tier (corrected 2026-08 after
    # an earlier pass wrongly concluded it wasn't, caused by a case-sensitive
    # search bug - see pricing/sku_mapping.py's module-level comment above
    # _SQL_RESERVATION_PREFIX for the full correction), but it genuinely has
    # no Reserved Capacity offering - verified live, zero Reservation entries
    # exist anywhere in the Retail Prices API for this tier. General Purpose
    # and Business Critical remain fully eligible.
    s = (sku or "").upper()
    if not s or s == "N/A":
        return True, "Assumed vCore-based General Purpose/Business Critical (SKU not captured for this resource)."
    if any(p in ("HS", "HYPERSCALE") for p in s.split("_")):
        return False, "SQL Managed Instance Hyperscale has no Reserved Capacity offering - verified live against the Retail Prices API (zero Reservation entries for this tier, though Consumption/PAYG pricing is real and available)."
    return True, "SQL Managed Instance is always vCore-based - reservations cover compute cost (not storage)."


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
    # Basic and Standard tiers are NOT eligible - only Premium qualifies.
    # Redis Enterprise (aka "Azure Managed Redis") is a genuinely separate
    # ARM resource type (Microsoft.Cache/redisEnterprise, confirmed 2026-08)
    # tracked under its own resource_type "Azure Cache for Redis Enterprise"
    # - see _redis_enterprise_ri below, not handled here.
    s = (sku or "").lower()
    if not s or s == "n/a":
        return True, "Assumed Premium tier (SKU not captured for this resource)."
    if "premium" in s:
        return True, "Premium tier - eligible for Azure Cache for Redis reservations."
    return False, "Basic and Standard tiers are not eligible for Azure Cache for Redis reservations - only Premium qualifies."


def _redis_enterprise_ri(sku: str) -> Tuple[bool, str]:
    # Verified live across all 6 real families (Balanced/MemoryOptimized/
    # ComputeOptimized/FlashOptimized/Enterprise/EnterpriseFlash) - every
    # one has real Reservation catalog entries, unlike classic Redis where
    # only Premium does. Genuinely always eligible at this resource type.
    return True, "Azure Cache for Redis Enterprise (Azure Managed Redis) - eligible for Reserved Capacity at every service tier."


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


def _flexible_server_ri(sku: str) -> Tuple[bool, str]:
    # SKU convention: "{tier}_{arm sku.name}" (see pricing/sku_mapping.py).
    # Verified live against the Retail Prices API AND the real Azure
    # pricing calculator (2026-08, both PostgreSQL and MySQL Flexible
    # Server): Burstable tier has ZERO Reservation entries in every region
    # checked, and the calculator shows no Reservation option at all when
    # Burstable is selected - a real, confirmed policy exclusion, not a
    # lookup gap. General Purpose and Memory Optimized (MySQL's "Business
    # Critical" tier maps to this same ARM sku.tier value - see
    # pricing/sku_mapping.py's _plan_mysql) ARE reservable per the
    # calculator; some individual newer VM series haven't rolled out real
    # Reservation catalog entries yet (verified: e.g. PostgreSQL/MySQL's
    # newest Ddsv5/Ddsv6 General Purpose series currently return none) -
    # that's a priceability gap this app's pricing layer already handles
    # safely (returns no rate rather than a wrong one), not a reason to
    # mark the whole tier ineligible, matching the same eligibility-vs-
    # priceability split already used for Fsv2-series SQL Database.
    tier = (sku or "").split("_", 1)[0]
    if tier == "Burstable":
        return False, "Burstable tier has no Reserved Capacity offering - verified live (zero Reservation entries in the Retail Prices API for any B-series SKU) and confirmed in the pricing calculator (no Reservation option shown for Burstable)."
    return True, "General Purpose/Memory Optimized (or MySQL's equivalent 'Business Critical') tier - eligible for Reserved Capacity. (Legacy Single Server no longer accepts new reservations, but isn't distinguishable from our current inventory data.)"


def _cosmos_db_ri(sku: str) -> Tuple[bool, str]:
    # SKU convention: "{CapacityMode}_{ServiceTier}_{RUs}" or bare
    # "Serverless" - see pricing/sku_mapping.py. Serverless accounts have
    # nothing provisioned to reserve capacity against - verified live, zero
    # Reservation entries apply and the real pricing page confirms
    # reservations only cover provisioned throughput. Standard/Autoscale
    # provisioned throughput IS eligible (real Reservation catalog entries
    # exist, sold in fixed RU/s bucket sizes as a subscription-wide pool -
    # see pricing/sku_mapping.py's reservation_unsupported_reason for why
    # this app doesn't compute a per-resource $ rate for it even though the
    # resource itself is genuinely eligible).
    if (sku or "").split("_")[0] == "Serverless":
        return False, "Serverless Cosmos DB accounts have no provisioned throughput to reserve capacity against."
    return True, "Provisioned throughput (Standard or Autoscale) - eligible for Reserved Capacity, purchased as a subscription-wide RU/s pool rather than per-resource."


def _synapse_ri(sku: str) -> Tuple[bool, str]:
    # This app's SKU is captured directly from the real Dedicated SQL Pool
    # child resource's top-level sku.name (Microsoft.Synapse/workspaces/
    # sqlPools, e.g. "DW500c" - a separate, billable resource type from the
    # workspace container itself, confirmed via Microsoft's own ARM
    # template reference, 2026-08 - see azure_conn/connector.py). A
    # genuine, correctly-formatted "DW<n>c" SKU is always the real
    # Dedicated SQL Pool, which IS eligible - Serverless SQL Pool and
    # Apache Spark Pools are architecturally incapable of ever producing
    # this SKU shape (Serverless has no dedicated resource/SKU at all;
    # Spark Pools bill per vCore-hour via a completely different resource
    # type this app doesn't currently ingest), so no separate exclusion
    # check is needed here - the SKU shape itself is the signal.
    if not re.match(r"^DW\d+c$", (sku or "").strip(), re.IGNORECASE):
        return False, f"SKU '{sku}' doesn't match the Dedicated SQL Pool 'DW<n>c' convention - Serverless SQL Pool and Apache Spark Pools are architecturally different resources not eligible for classic Reserved Capacity."
    return True, "Dedicated SQL Pool (cDWU) - eligible for Reserved Capacity, but only sold at the DW100c unit (larger tiers priced as a multiple - see pricing/sku_mapping.py)."


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
    "Azure SQL Managed Instance":    _sql_mi_eligibility,
    "Azure SQL Managed Instance Pool": _sql_mi_eligibility,   # same vCore-tier eligibility rule - eligibility
                                                                # (can Azure sell this) and priceability (can this
                                                                # app compute a rate) are different questions -
                                                                # see pricing/sku_mapping.py, where this type is
                                                                # deliberately left unpriced despite being eligible.
    "Azure Database for MySQL":      _flexible_server_ri,
    "Azure Database for PostgreSQL": _flexible_server_ri,
    "Azure Cosmos DB":               _cosmos_db_ri,
    "Azure Blob Storage":            _storage_eligibility,
    "Azure Cache for Redis":         _redis_eligibility,
    "Azure Cache for Redis Enterprise": _redis_enterprise_ri,
    "Azure Synapse Analytics":       _synapse_ri,
    "Azure Databricks":              lambda sku: (True, "Databricks Commit Units (DBCU) prepurchase apply as a pooled discount across all workloads/tiers, not a per-resource reservation match."),
    "App Service":                   _app_service_eligibility,
    "Azure Disk Storage":            _disk_eligibility,
    "Azure Files":                   lambda sku: (True, "Eligible for Azure Files reservations, but only sold in 10 TiB / 100 TiB blocks (Hot/Cool tier capacity)."),
    "Azure Dedicated Host":          lambda sku: (True, "Eligible for Reserved Instances - verified live, real Reservation catalog entries exist for most VM-family Dedicated Host types (some newer/niche series may not have rolled out RI yet, handled safely by the pricing layer's live lookup)."),
    "Azure Container Instances":     lambda sku: (False, "Azure Container Instances has no Reservation offering at all - verified live, it's a pure consumption service with no capacity to reserve. (It IS eligible for Savings Plan for Compute - see sp_eligibility.py.)"),
    "Azure Container Apps":          lambda sku: (False, "Azure Container Apps has no Reservation offering at all - verified live, zero Reservation entries exist for any workload profile. (The Dedicated profile IS eligible for Savings Plan for Compute - see sp_eligibility.py.)"),
    "Azure Spring Apps Enterprise":  lambda sku: (False, "Azure Spring Apps Enterprise has no Reservation offering at all - verified live, zero Reservation entries exist. (It IS eligible for Savings Plan for Compute - see sp_eligibility.py.)"),
    "Azure DocumentDB":              lambda sku: (True, "Eligible for Reserved Instances - verified live, real Reservation catalog entries exist (Coordinator Node 1 vCore, confirmed 1yr/3yr pricing). This app doesn't price DocumentDB yet (see sp_eligibility.py/pricing/sku_mapping.py for the real, disclosed reason), but the underlying Azure product genuinely does sell reservations for it."),
    "Azure Database Migration Service": lambda sku: (False, "Azure Database Migration Service has no Reservation offering at all - verified live, zero Reservation entries exist across all three tiers. (It IS eligible for Savings Plan for Databases - see sp_eligibility.py.)"),
    "Azure Data Factory": lambda sku: (True, "Eligible for Reserved Capacity, but it's a pure subscription-wide 'buy N cores of a compute type' spend commitment with no per-resource allocation at all - Microsoft's own docs confirm a reservation 'does not pre-allocate or reserve specific infrastructure' and applies automatically to ANY matching data flow, existing or future. Not eligible for Savings Plan for Compute or Databases (not on either official coverage list)."),
    "Azure Data Explorer": lambda sku: (True, "Eligible for Reserved Capacity, but it ONLY discounts a separate services 'markup' fee - cluster compute/networking/storage are billed and reserved (via Savings Plan for Compute, if applicable) separately. Verified live: purchasing needs no SKU/size/region at all, just a term - applies globally to every Data Explorer deployment in the subscription."),
    "Azure Backup Storage": lambda sku: (True, "Eligible for Reserved Capacity for the vault-standard tier only (not vault-archive, not Protected Instance cost) - verified live, sold ONLY in 100 TiB/1 PiB blocks, applied subscription/resource-group-wide, not tied to any specific Recovery Services Vault."),
    "Azure NetApp Files": lambda sku: (True, "Eligible for Reserved Capacity for Standard/Premium/Ultra service levels (not the Flexible service level) - verified live, sold ONLY in 100 TiB/1 PiB blocks per service-level+region, not tied to a specific capacity pool. Cool-access capacity pools only get the reservation benefit on 'hot' tier consumption; cross-region replication and backup add-ons aren't covered."),
    "Microsoft Fabric": lambda sku: (True, "Eligible for Reserved Capacity - verified live, real per-CU pricing exists (1yr and 3yr both give the same ~$0.1249/CU-hr effective rate, a real finding not a bug). Not eligible for any Savings Plan - see sp_eligibility.py."),

    # AWS additions, 2026-08-22 - added alongside the new live inventory
    # fetch for these 5 resource types (aws/connector.py). Without an
    # explicit rule here, check_eligibility()'s default (True, "no rule
    # encoded yet") would have wrongly counted every running one of these
    # as an RI "gap" - a real regression caught in browser verification
    # (RI Coverage's "Needs More RI" count jumped from 7 to 12 the moment
    # this inventory started flowing through, before this fix). Confirmed
    # via boto3's own service model for each client (docdb/neptune/dms/
    # keyspaces/ecs): none expose any DescribeReserved*-style operation at
    # all - genuinely no Reserved Instance product exists for any of them,
    # not just an unresearched gap. All are Savings-Plan-eligible instead
    # (Database SP for the first four, Compute SP for Fargate - see
    # db/aws_seed.py's AWS_DATABASE_SP_TYPES/AWS_COMPUTE_SP_TYPES).
    "Amazon DocumentDB":              lambda sku: (False, "Amazon DocumentDB has no Reserved Instance offering - confirmed via boto3's docdb service model (no DescribeReservedDBInstances-equivalent operation exists). Eligible for Database Savings Plans instead."),
    # Added 2026-08-23 alongside DocumentDB Serverless's new live fetch
    # (aws/connector.py) - genuinely can't have an RI even in principle
    # (Reserved Instances require a fixed instance class to reserve;
    # Serverless has none, billing per-DCU-hour instead), same underlying
    # reason as classic DocumentDB above.
    "Amazon DocumentDB Serverless":   lambda sku: (False, "DocumentDB Serverless has no Reserved Instance offering - Reserved Instances require a fixed instance class to reserve, and Serverless bills per-DCU-hour instead. Eligible for Database Savings Plans instead."),
    "Amazon Neptune":                 lambda sku: (False, "Amazon Neptune has no Reserved Instance offering - confirmed via boto3's neptune service model (no Reserved*-style operation exists). Eligible for Database Savings Plans instead."),
    "AWS DMS Replication Instance":   lambda sku: (False, "AWS DMS has no Reserved Instance offering for replication instances - confirmed via boto3's dms service model (no Reserved*-style operation exists). Eligible for Database Savings Plans instead."),
    "Amazon Keyspaces":               lambda sku: (False, "Amazon Keyspaces is fully serverless (provisioned Read/Write Capacity Units, no instance to reserve) - confirmed via boto3's keyspaces service model, no Reserved*-style operation exists at all. Eligible for Database Savings Plans instead."),
    "AWS Fargate":                    lambda sku: (False, "AWS Fargate has no Reserved Instance concept - you bill your own chosen vCPU/memory directly, not a purchasable instance type. Confirmed via boto3's ecs service model. Eligible for Compute Savings Plans instead."),
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
#   Microsoft Fabric: verified live (Microsoft Learn, 2026-08) - CU
#              increments are purchased in units of 1 (finer-grained than
#              SQL DB's whole-vCore increments) and "the reservation
#              discount is automatically applied to your provisioned
#              instances that exist in that region" - same pooled-but-
#              resource-scale-plausible pattern as SQL DB/Cosmos DB, not
#              Storage/Files' oversized-block "unmeasurable" pattern.
_CAPACITY_POOLED_TYPES = {
    "Azure Cosmos DB", "Azure Databricks", "Azure Synapse Analytics",
    "Azure SQL Database", "Azure SQL Managed Instance", "Azure SQL Elastic Pool",
    "Azure SQL Managed Instance Pool", "Microsoft Fabric",
}
#   Azure Data Factory: verified live (Microsoft Learn, 2026-08) - a data flow
#              reservation is a pure subscription-wide "buy N cores of compute
#              type X (General Purpose/Memory Optimized)" spend commitment
#              with NO per-resource allocation at all - the docs' own words:
#              "Purchasing reserved capacity does not pre-allocate or reserve
#              specific infrastructure resources... for your use," and "You do
#              not need to assign the reservation to a specific factory or
#              integration runtime." Unlike Cosmos DB/SQL DB/Databricks (which
#              at least have a standing, per-resource provisioned value this
#              app captures and could compare a pool against), Data Factory
#              data flow compute is ephemeral/execution-based per pipeline run
#              - there's no standing "Data Factory compute" resource for this
#              app's inventory model to even represent, so a resource-count
#              gap is meaningless here in a more absolute way than Storage/
#              Files' "minimum purchase size dwarfs a resource" reason -
#              kept in "unmeasurable", not "capacity".
#   Azure Data Explorer: verified live (Microsoft Learn, 2026-08) - even more
#              purely pooled than Data Factory. A Data Explorer reservation
#              doesn't cover cluster compute/networking/storage at all (those
#              bill as normal VM costs) - it ONLY discounts a separate
#              "markup" fee layered on top, and purchasing one needs no SKU,
#              size, core count, or even region: "you only need to specify
#              the term, it will apply to all deployments of Azure Data
#              Explorer in all regions" (the docs' own words). Zero
#              dimensions exist to match against any single resource -
#              "unmeasurable" is the only correct classification.
#   Azure Backup Storage: verified live (Microsoft Learn, 2026-08) - same
#              "unmeasurable" shape as Blob Storage/Files, for the same
#              reason: sold ONLY in 100 TiB or 1 PiB blocks, applied
#              subscription/resource-group-wide, and explicitly "can't be
#              limited to a specific storage account, container, or object" -
#              i.e. not even tied to a specific Recovery Services Vault, let
#              alone a single resource. This app doesn't ingest Recovery
#              Services Vaults at all, so there's no per-resource concept to
#              compare against regardless.
#   Azure NetApp Files: verified live (Microsoft Learn, 2026-08) - the SAME
#              100 TiB/1 PiB block-size pattern as Blob Storage/Files/Backup
#              Storage, applied per service-level+region rather than to one
#              specific capacity pool - a typical NetApp capacity pool is far
#              smaller than the 100 TiB minimum purchase, so per-pool gap
#              tracking would be meaningless even though this app could
#              technically ingest individual capacity pool resources.
_UNMEASURABLE_TYPES = {"Azure Blob Storage", "Azure Files", "Azure Data Factory", "Azure Data Explorer", "Azure Backup Storage", "Azure NetApp Files"}


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
