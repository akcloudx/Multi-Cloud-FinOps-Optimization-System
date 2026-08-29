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
# Families with NO Reservation entries anywhere in the Retail Prices API -
# re-verified 2026-08-30 with a genuinely different methodology than the
# claim this replaced. The prior version of this set (and its own comment
# claiming a "full-catalog scan") was checked against ONLY eastus - two
# real, live-confirmed errors traced directly to that one blind spot: NP
# and HC both have ZERO entries in eastus but real, currently-active
# Reservation pricing in the specific regions where that hardware actually
# exists (NP: US West 2; HC: CA Central and UK South, some effective since
# 2021) - a single-region scan can prove a family unavailable THERE, never
# that it's unavailable everywhere. Every family below was re-checked with
# an UNFILTERED-BY-REGION query (armSkuName eq '<sku>' and priceType eq
# 'Reservation', no armRegionName clause), which returns real zero-
# everywhere-or-not-at-all - a query design NP/HC's original check didn't
# use.
#   A: Standard_A2_v2 - zero entries, any region.
#   G: Standard_G2 - zero entries, any region.
#   GS: Standard_GS2 - zero entries, any region.
#   DS: Standard_DS2_v2 - zero entries, any region (newer Dsv5-style
#       capacity-suffix naming DOES have RI - this only affects the old
#       "DS2_v2"-style names specifically).
#   H (base, NOT its HB/HC/HX variants): Standard_H8 - zero entries, any
#       region.
#   PB: Standard_PB6s - zero entries, any region.
# NP and HC REMOVED from this set 2026-08-30 (see above - both genuinely
# have real Reservation pricing, just region-restricted to where that
# specialized hardware is actually deployed). B also required a real
# split, not a blanket call either way - see _vm_eligibility() below.
#
# Known, disclosed limitation: this whole approach is still family-level,
# not region-level - a family kept in this set because eastus (or any
# other single region checked) has zero entries could still have a real,
# narrow regional exception this pass didn't happen to query for, the
# exact failure mode that produced the NP/HC errors above. A more correct
# design would check (family, tenant's actual region) rather than a global
# per-family yes/no, at the cost of a live API call per distinct region
# instead of a static set - not implemented here, flagged as a real
# follow-up rather than silently left as a repeat of the same mistake.
_VM_FAMILIES_NO_RI = {"A", "G", "GS", "DS", "H", "PB"}


def _vm_eligibility(sku: str) -> Tuple[bool, str]:
    # https://learn.microsoft.com/en-us/azure/virtual-machines/reserved-vm-instance-size-flexibility
    # NOT a blanket "all VM series" claim (that was the old, wrong version of
    # this function) - see _VM_FAMILIES_NO_RI above for the live evidence.
    s = (sku or "").strip()
    if not s or s == "N/A":
        return True, "Assumed a mainstream VM series with Reserved Instance support (SKU not captured for this resource)."
    # B-series needed a genuine split, not a single verdict either way -
    # caught live 2026-08-30 (a user's real Azure pricing calculator
    # screenshot showed "not available" for a classic Standard_B2ms, which
    # a first, too-hasty fix generalized to "exclude all B" - a broader
    # re-check (a full Reservation-price scan across all VM families,
    # eastus) surfaced Standard_B2pls_v2 and Standard_B32s_v2 WITH real
    # Reservation pricing, both from the newer "Bpsv2/Bsv2" generation.
    # Confirmed directly: classic B2ms/B2s/B4ms (no version suffix) all
    # return zero Reservation entries in any region; the "_v2" generation
    # is a real, separate, currently-eligible product line, not the same
    # thing under new naming.
    if s.startswith(("Standard_B", "Basic_B")):
        if s.endswith("_v2"):
            return True, "Newer 'Bpsv2/Bsv2'-generation burstable VMs have a real Reserved Instance offering (confirmed live) - unlike the classic B-series, which does not."
        return False, "Classic B-series (burstable) VMs have no Reserved Instance offering - confirmed live against the Retail Prices API, zero entries in any region. (The newer '_v2' B-series generation IS eligible - a different, unrelated product line.)"
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
    # https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types
    # ("Azure disk reservations offer a one-year commitment plan for
    # Premium SSD SKUs from P30 (1 TiB) to P80 (32 TiB)") - only Premium
    # SSD disks at size P30 and larger are eligible. Standard SSD/HDD,
    # Ultra Disk, and Premium SSD v2 are not eligible at any size.
    s = (sku or "").upper()
    if not s or s == "N/A":
        return True, "Assumed Premium SSD P30+ (exact disk SKU/size not captured for this resource)."
    if "ULTRA" in s or "V2" in s:
        return False, "Ultra Disk and Premium SSD v2 are not eligible for Azure Disk Storage reservations."
    if "STANDARD" in s and "PREMIUM" not in s:
        return False, "Standard SSD/HDD disks are not eligible - only Premium SSD disks at size P30 and larger qualify."
    # Corrected 2026-08-23 - this branch previously defaulted to True for
    # ANY Premium disk regardless of size, contradicting its own stated
    # "if size is P30 or larger" condition - the real size check wasn't
    # possible until live inventory + a real diskSizeGB round-up table
    # existed (see pricing/sku_mapping.py's disk_reservation_eligible,
    # added alongside this fix). Falls back to the old (defaulted-True)
    # behavior only when the SKU isn't in this app's own
    # "{family}_{redundancy}_{diskSizeGB}" convention at all - e.g. the
    # legacy demo-only "P30_Premium_SSD" string, or a genuinely uncaptured
    # SKU - since there's no size to check in that case either way.
    from pricing.sku_mapping import disk_reservation_eligible
    eligible = disk_reservation_eligible(sku)
    if eligible is None:
        return True, "Premium SSD - eligible for Azure Disk Storage reservations if size is P30 or larger (exact size not in this app's parseable SKU convention for this resource)."
    if eligible:
        return True, "Premium SSD, P30 or larger - eligible for Azure Disk Storage reservations."
    return False, f"Premium SSD smaller than P30 ('{sku}') - Azure only sells Disk Reservations from P30 (1 TiB) up."


# Added 2026-08-30 during the wider AWS RI audit. Confirmed via AWS's own
# EC2 Mac Instances FAQ: "available only as On-Demand Instances... not
# available as Spot Instances or Reserved Instances" - Mac instances are
# billed per Dedicated Host with a 24-hour minimum allocation (Apple's
# macOS EULA), and Savings-Plan-eligible instead (both Compute and
# Instance SP). All real Mac SKUs share this prefix (mac1.metal,
# mac2.metal, mac2-m2.metal, mac2-m2pro.metal, mac2-m1ultra.metal,
# mac-m3ultra.metal, mac-m4.metal/m4pro/m4max - confirmed against
# botocore's own DescribeReservedInstancesOfferings InstanceType enum).
def _ec2_eligibility(sku: str) -> Tuple[bool, str]:
    s = (sku or "").strip().lower()
    if s.startswith("mac"):
        return False, "EC2 Mac instances have no Reserved Instance offering - confirmed via AWS's own Mac Instances FAQ (available only as On-Demand, billed per Dedicated Host with a 24-hour minimum allocation for Apple's macOS EULA). Eligible for Compute/Instance Savings Plans instead."
    return True, "EC2 Standard/Convertible Reserved Instances are broadly available across current-generation instance families."


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
    # Corrected 2026-08-23, once this app started pricing DocumentDB's
    # Worker Node cost (see pricing/sku_mapping.py's _plan_documentdb): the
    # PREVIOUS "True" here was accurate about the underlying Azure product
    # ("real Reservation catalog entries exist... Coordinator Node 1 vCore")
    # but that's genuinely NOT eligible for what THIS app tracks and would
    # show a gap - "buy more Worker Node RIs" - for a purchase that doesn't
    # exist. Verified live: zero Reservation entries exist for Worker Node
    # at any size; the only real Reservation product targets the
    # Coordinator Node, which this app can't price at all (no ARM-
    # observable field for it - see _plan_documentdb's own comment) and so
    # was never eligible for a per-resource gap here in the first place.
    "Azure DocumentDB":              lambda sku: (False, "No Reservation product exists for Worker Node (the cost this app tracks) at any size - verified live, zero Reservation entries. The only real Reservation product for this service targets the Coordinator Node, which isn't exposed as an ARM-observable property this app could size or track a purchase against."),
    "Azure Database Migration Service": lambda sku: (False, "Azure Database Migration Service has no Reservation offering at all - verified live, zero Reservation entries exist across all three tiers. (It IS eligible for Savings Plan for Databases - see sp_eligibility.py.)"),
    "Azure Data Factory": lambda sku: (True, "Eligible for Reserved Capacity, but it's a pure subscription-wide 'buy N cores of a compute type' spend commitment with no per-resource allocation at all - Microsoft's own docs confirm a reservation 'does not pre-allocate or reserve specific infrastructure' and applies automatically to ANY matching data flow, existing or future. Not eligible for Savings Plan for Compute or Databases (not on either official coverage list)."),
    "Azure Data Explorer": lambda sku: (
        (True, "Eligible for Reserved Capacity (Standard tier) - only discounts a separate services 'markup' fee (~$0.11/hr/node in most regions, verified live), NOT cluster compute/networking/storage (billed and reserved separately). Purchased per-node; Microsoft's docs say the discount applies to all deployments/regions once bought, but the reservation itself is still priced per-region at purchase time (verified live: real catalog entries currently exist for only some regions, e.g. westus2, not others like australiaeast).")
        if (sku or "").split("_", 1)[0] == "Standard" else
        (False, f"Basic (Dev/No SLA) tier Data Explorer clusters have no Engine Cluster Markup fee at all - verified live, zero pricing/reservation items exist for this tier. '{sku}' is Basic tier.")
    ),
    "Azure-SSIS Integration Runtime": lambda sku: (False, "No Reservation product exists for Azure-SSIS Integration Runtime nodes at all - verified live, zero Reservation entries for this service. Not on Microsoft's own Savings Plan for Compute coverage list either - this is genuinely Consumption-only, no commitment discount of any kind available. (Azure DOES sell a 'Data Factory' Reserved Capacity product under the shared reserved_resource_type - see the 'Azure Data Factory' entry above - but that's Data Flow's unrelated, pooled compute-cores prepurchase with no per-node dimension; it can't cover this.)"),
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
    # Renamed from the generic "Compute" 2026-08-23 - that string was ALSO
    # Azure's own resource_type for VMs (both providers share one flat
    # resource_type namespace throughout this app), so AWS EC2 rows were
    # silently routed through _vm_eligibility() above - Azure's VM-family
    # regex/exclusion-list logic. Harmless by coincidence (no EC2 SKU, e.g.
    # "m5.large", ever matches the Azure "Basic_"/"Standard_" family prefix
    # pattern, so it always fell through to the generic True branch with an
    # Azure-flavored reason string - confirmed that reason text is never
    # actually shown to a user, app.py only renders eligibility_reason for
    # INeligible resources), but fragile and confusing, not a real EC2
    # rule. This is EC2's own, dedicated rule: EC2 Standard/Convertible
    # Reserved Instances are broadly available across current-generation
    # instance families.
    # Updated 2026-08-30 during the wider AWS RI audit: unlike Azure's VM
    # lineup, AWS doesn't publish a static family-exclusion list or expose
    # an unauthenticated catalog API this app can scan the way Azure's
    # Retail Prices API allowed - DescribeReservedInstancesOfferings'
    # InstanceType enum (checked via botocore's service model) includes
    # Mac/high-memory/P4-P5-Trn instance types too, but that only proves
    # the API accepts a query for them, not that real offerings exist for
    # it (the exact same "enum presence isn't proof of a product" trap
    # already caught for SageMaker's Reserved Capacity above). Mac
    # instances are the one exclusion with actual documented AWS text
    # behind it (confirmed via AWS's own EC2 Mac Instances FAQ): "available
    # only as On-Demand Instances... not available as Spot Instances or
    # Reserved Instances" - billed per Dedicated Host with a 24-hour
    # minimum allocation (Apple's macOS EULA), Savings-Plan-eligible
    # instead (both Compute and Instance SP). High-memory (u-*/u7i-*) and
    # P4/P5/Trn ML instances were researched too but found NO equivalent
    # documented exclusion (Capacity Blocks for ML is a real, separate
    # purchase option for the latter, but nothing found stating it
    # replaces/excludes standard RIs) - left eligible=True, same
    # "don't assert an exclusion we can't confirm" discipline as
    # everywhere else in this file, not a claim they've been fully audited.
    "Amazon EC2":                     _ec2_eligibility,
    "Amazon DocumentDB":              lambda sku: (False, "Amazon DocumentDB has no Reserved Instance offering - confirmed via boto3's docdb service model (no DescribeReservedDBInstances-equivalent operation exists). Eligible for Database Savings Plans instead."),
    # Added 2026-08-23 alongside DocumentDB Serverless's new live fetch
    # (aws/connector.py) - genuinely can't have an RI even in principle
    # (Reserved Instances require a fixed instance class to reserve;
    # Serverless has none, billing per-DCU-hour instead), same underlying
    # reason as classic DocumentDB above.
    "Amazon DocumentDB Serverless":   lambda sku: (False, "DocumentDB Serverless has no Reserved Instance offering - Reserved Instances require a fixed instance class to reserve, and Serverless bills per-DCU-hour instead. Eligible for Database Savings Plans instead."),
    "Amazon Neptune":                 lambda sku: (False, "Amazon Neptune has no Reserved Instance offering - confirmed via boto3's neptune service model (no Reserved*-style operation exists). Eligible for Database Savings Plans instead."),
    # Added 2026-08-23 alongside Neptune Serverless's new live fetch
    # (aws/connector.py) - same reasoning as DocumentDB Serverless: Reserved
    # Instances require a fixed instance class to reserve, and Serverless
    # bills per-NCU-hour instead, so no RI product can exist for it even in
    # principle. Added proactively this time (not after a caught bug) -
    # this exact "forgot the explicit rule, defaulted to wrongly eligible"
    # mistake happened for DocumentDB Serverless immediately before this.
    "Amazon Neptune Serverless":      lambda sku: (False, "Neptune Serverless has no Reserved Instance offering - Reserved Instances require a fixed instance class to reserve, and Serverless bills per-NCU-hour instead. Eligible for Database Savings Plans instead."),
    # Added 2026-08-23 alongside Neptune Analytics's new live fetch
    # (aws/connector.py) - a separate product/boto3 client from Neptune
    # Database. Confirmed via botocore's own installed neptune-graph service
    # model: no Reserved*-style operation exists at all.
    "Amazon Neptune Analytics":       lambda sku: (False, "Neptune Analytics has no Reserved Instance offering - confirmed via boto3's neptune-graph service model (no Reserved*-style operation exists). Eligible for Database Savings Plans instead."),
    "AWS DMS Replication Instance":   lambda sku: (False, "AWS DMS has no Reserved Instance offering for replication instances - confirmed via boto3's dms service model (no Reserved*-style operation exists). Eligible for Database Savings Plans instead."),
    # Added 2026-08-23 alongside DMS Serverless's new live fetch
    # (aws/connector.py) - same dms service model, same "no Reserved*-style
    # operation exists" fact applies to ReplicationConfig too.
    "AWS DMS Serverless":             lambda sku: (False, "AWS DMS Serverless has no Reserved Instance offering - Reserved Instances require a fixed instance class to reserve, and Serverless bills per-DCU-hour instead. Eligible for Database Savings Plans instead."),
    "Amazon Keyspaces":               lambda sku: (False, "Amazon Keyspaces is fully serverless (provisioned Read/Write Capacity Units, no instance to reserve) - confirmed via boto3's keyspaces service model, no Reserved*-style operation exists at all. Eligible for Database Savings Plans instead."),
    "AWS Fargate":                    lambda sku: (False, "AWS Fargate has no Reserved Instance concept - you bill your own chosen vCPU/memory directly, not a purchasable instance type. Confirmed via boto3's ecs service model. Eligible for Compute Savings Plans instead."),
    # Added 2026-08-23 - found missing while auditing the full RI/SP
    # coverage picture for the user: SageMaker was added to inventory
    # without an explicit rule here, so it had been silently defaulting to
    # eligible=True (check_eligibility()'s "no rule encoded yet" fallback),
    # the exact same regression class already caught once for DocumentDB
    # Serverless.
    # Corrected 2026-08-30: the sagemaker service model DOES have
    # Reserved*-style operations (DescribeReservedCapacity,
    # ListUltraServersByReservedCapacity) - confirmed live via AWS's own API
    # reference. But their InstanceType is restricted to a fixed enum of
    # large-scale training/inference accelerators only (ml.p4d.24xlarge,
    # ml.p5.48xlarge, ml.trn1.32xlarge, ml.p6-b200.48xlarge, etc.) - this is
    # SageMaker HyperPod's Reserved Capacity for distributed training
    # clusters, an architecturally different resource this app doesn't
    # track, not a reservation for Endpoints or Notebook Instances (same
    # "real reservation, wrong resource type" pattern already found for
    # Azure Data Factory). The eligibility conclusion below is unchanged.
    "Amazon SageMaker Endpoint":          lambda sku: (False, "SageMaker Endpoints have no Reserved Instance offering - SageMaker's Reserved Capacity (HyperPod) only covers large-scale training/inference accelerator instances (e.g. ml.p4d, ml.p5, ml.trn1), not endpoint hosting instances. Eligible for SageMaker AI Savings Plans instead."),
    "Amazon SageMaker Notebook Instance": lambda sku: (False, "SageMaker Notebook Instances have no Reserved Instance offering - SageMaker's Reserved Capacity (HyperPod) only covers large-scale training/inference accelerator instances (e.g. ml.p4d, ml.p5, ml.trn1), not notebook instances. Eligible for SageMaker AI Savings Plans instead."),
    # Added 2026-08-30 - found during the wider AWS RI audit: aws/connector.py's
    # RDS inventory fetch (describe_db_instances) captured Aurora Serverless
    # v2 instances (real DBInstanceClass "db.serverless") completely
    # undetected, unlike the DocumentDB/Neptune Serverless fetches right
    # above it in that file, which explicitly branch on a Serverless
    # signal. Without this rule those rows fell through to the generic
    # "Amazon Aurora (MySQL)"/"(PostgreSQL)" label and defaulted to
    # eligible=True - the exact same regression class already caught for
    # DocumentDB Serverless. Confirmed live: AWS's own Reserved DB
    # Instances docs state Reserved Instances apply to instance-based
    # Aurora only; Serverless v2 bills per-ACU-hour with no fixed instance
    # class to reserve.
    "Amazon Aurora (MySQL Serverless)":      lambda sku: (False, "Aurora Serverless v2 has no Reserved Instance offering - Reserved Instances apply to instance-based Aurora only (confirmed via AWS's own docs); Serverless v2 bills per-ACU-hour with no fixed instance class to reserve. Eligible for Database Savings Plans instead."),
    "Amazon Aurora (PostgreSQL Serverless)": lambda sku: (False, "Aurora Serverless v2 has no Reserved Instance offering - Reserved Instances apply to instance-based Aurora only (confirmed via AWS's own docs); Serverless v2 bills per-ACU-hour with no fixed instance class to reserve. Eligible for Database Savings Plans instead."),
}


def check_eligibility(resource_type: str, sku: str, region: str = None, os_: str = None,
                       redundancy: str = None, prices_df=None) -> Tuple[bool, str]:
    """Returns (is_eligible, reason). Resource types with no rule encoded yet
    default to eligible with a generic note - we'd rather under-flag than
    assert a wrong exclusion for a service we haven't researched.

    region/os_/redundancy/prices_df (2026-08-30, all optional, backward
    compatible with every existing caller that doesn't pass them) - when
    given, a LIVE, per-region signal from the tenant's own synced pricing
    cache is checked FIRST and wins over every static rule below. This
    exists because the static rules in this file are inherently a
    maintenance liability: they're built from research done at some point
    against SOME region, and Azure's real catalog can have regional
    exceptions that research never saw - confirmed live, twice, the same
    day this parameter was added (NP-series and HC-series VMs were both
    hardcoded here as globally ineligible, when they're real, purchasable
    Reservation products just restricted to the specific regions where
    that hardware is deployed - a single-region scan can prove "not
    available THERE," never "not available anywhere," and this file's own
    prior comments had generalized the former into the latter). A live
    signal is authoritative for the EXACT SKU/region it was fetched for;
    it says nothing about other regions, so a False here doesn't get
    written back into the static rules - it only overrides the verdict
    for resources this tenant's own sync has actually confirmed.

    No live data for this combo (prices_df empty/None, region not given,
    or nothing synced yet for this SKU) falls through to the exact same
    static rules as before - this only ever makes an ineligible verdict
    MORE accurate when live data exists, never less accurate when it
    doesn't."""
    if prices_df is not None and region:
        from analysis.commitment_economics import check_live_ri_availability
        live = check_live_ri_availability(prices_df, resource_type, region, sku, os_, redundancy or "N/A")
        if live is True:
            return True, f"Live-confirmed: a real Reserved Instance rate is currently cached for this exact SKU in {region} - overrides any static assumption below."
        if live is False:
            return False, f"Live-confirmed: a live pricing query found zero Reserved Instance offerings for this exact SKU in {region} - real, per-region evidence, not a static guess."
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
    "Azure SQL Managed Instance Pool", "Microsoft Fabric", "Azure Data Explorer",
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
#   Azure Data Explorer: moved OUT of unmeasurable and into capacity-pooled,
#              2026-08-23, once live inventory tracking was built for it
#              (azure_conn/connector.py) - the ORIGINAL "unmeasurable"
#              classification below was based on "this app can't represent
#              it as inventory at all," a premise that's no longer true.
#              A Data Explorer reservation doesn't cover cluster compute/
#              networking/storage at all (those bill as normal VM costs) -
#              it ONLY discounts a separate "markup" fee layered on top,
#              purchased as a real node count (this app's inventory SKU now
#              captures the same unit via sku.capacity - see
#              pricing/sku_mapping.py's _plan_data_explorer), small/granular
#              enough to plausibly correlate with actual cluster node counts,
#              same reasoning as SQL DB's vCore reservations. Microsoft's own
#              docs state the discount "will apply to all deployments of
#              Azure Data Explorer in all regions" once purchased (pooled
#              application) - but verified live (2026-08) against the real
#              Retail Prices API, the reservation is still PRICED per
#              region at purchase time like a normal Reservation (real
#              armRegionName values, e.g. 'westus2'), not "Global" the way
#              Cosmos DB/Databricks reservations literally are - only one
#              region currently has a populated catalog entry, most others
#              (including australiaeast, checked directly) return zero. This
#              app's normal "query live, no data if genuinely absent"
#              pattern already handles that gap safely either way.
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
_UNMEASURABLE_TYPES = {"Azure Blob Storage", "Azure Files", "Azure Data Factory", "Azure Backup Storage", "Azure NetApp Files"}


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
