"""
pricing/sku_mapping.py
Maps this app's internal (Resource Type, SKU) to the correct Azure Retail
Prices API query strategy for Reserved Instance / Savings Plan lookups.

Only Compute (VMs) has armSkuName == the resource's SKU directly. Every other
service was individually researched live against the API while building this
(2026-08) - see the per-service notes below. Where Azure genuinely has no
RI/SP offering the API can answer (Cosmos DB Reserved Capacity specifically -
real entries exist but are sold as a subscription-wide pool in fixed bucket
sizes, not linearly per-resource; Databricks' marketplace-billed DBCU
commits; classic Standard/Basic Redis), this module says so explicitly with
a reason, rather than returning an empty/zero result that could be mistaken
for "just hasn't synced yet."

Where a mapping below is marked "inferred" rather than "verified", it follows
a naming pattern confirmed for a sibling tier of the same service, but wasn't
individually queried - flagged so it's easy to re-check once real usage
surfaces a case that exercises it.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SkuQueryPlan:
    """How (or whether) to find this resource's real price in the Retail
    Prices API."""
    supported: bool
    reason: str = ""                     # why unsupported, when supported=False
    service_name: Optional[str] = None   # Retail API serviceName to scope the query ($filter)
    match_field: str = "armSkuName"      # "armSkuName" | "skuName" - response field to match in Python after fetching
    consumption_match_value: str = ""    # value to match for Consumption/SavingsPlan lookup
    reservation_match_value: str = ""    # value to match for Reservation lookup (can differ from consumption side)
    product_contains: str = ""           # optional productName substring to disambiguate tiers sharing a skuName
    reservation_multiplier: float = 1    # multiply the matched Reservation unit price by this (e.g. vCore count)
    consumption_multiplier: float = 1    # multiply the matched Consumption retailPrice (PAYG) by this - NOT
                                          # needed for General Purpose/Business Critical Provisioned SQL DB/MI
                                          # (their Consumption armSkuName already encodes the vCore count and
                                          # is pre-scaled), but IS needed for Hyperscale Provisioned and every
                                          # Serverless tier: verified live these only expose a single flat
                                          # per-1-vCore Consumption meter regardless of how many vCores the
                                          # resource is actually configured for - unlike Reservations (which
                                          # were ALREADY known to need multiplying), the Consumption/PAYG side
                                          # silently understated cost by the full vCore-count factor before
                                          # this was caught. See _plan_sql_hyperscale/_plan_sql_serverless.
    savings_plan_multiplier: int = 1     # multiply the matched savingsPlan.unitPrice by this - NOT always the
                                          # same assumption as the top-level Consumption retailPrice already
                                          # being pre-scaled; verified live that SQL DB/MI's nested savingsPlan
                                          # array reports a flat PER-VCORE rate that does NOT vary by the
                                          # queried armSkuName's vCore count, unlike retailPrice which does -
                                          # multiplying by 1 here is a silent ~4-16x cost understatement for
                                          # anything wider than 1 vCore. See _plan_sql_family.
    os_license_is_separable: bool = False
    # True ONLY for Compute (VMs). Verified live + against the Azure pricing
    # calculator (2026-08): a Windows VM's Consumption entry is a license-
    # INCLUSIVE bundle (no savingsPlan array of its own) sitting alongside an
    # untagged base/Linux entry that IS the true compute-only price the
    # discount attaches to - and VM compute hardware cost is OS-independent
    # (a Windows and Linux VM of the same size cost the same to run; only the
    # Windows Server license differs). This app deliberately excludes OS
    # license cost entirely (by user decision) rather than tracking it as a
    # surcharge, so for Compute the base/non-Windows meter is used as PAYG
    # for BOTH Windows and Linux resources of the same SKU.
    # This does NOT generalize to every OS-tagged service: App Service's
    # Windows vs Linux tiers are genuinely different compute costs (not a
    # license line bolted onto an otherwise-identical base price), so
    # App Service keeps normal OS-matching (see _os_matched_items) with this
    # flag left False.
    reservation_unsupported_reason: Optional[str] = None
    # Set (with supported=True, since Consumption/Savings Plan IS still
    # sourced normally) when Reservation pricing must NOT be fetched even
    # though the Retail API happens to return a catalog match - e.g. a
    # stale/legacy entry the real pricing calculator explicitly marks "not
    # available" (see _plan_sql_dc_series). An empty reservation_match_value
    # alone does NOT reliably suppress this: product_contains is a
    # server-side filter applied to BOTH Consumption and Reservation
    # queries, and an empty client-side match_value is treated as "no
    # filter" rather than "match nothing" - so a real API item can still
    # surface even with reservation_match_value="" (caught live: exactly
    # this happened for DC-series). _fetch_azure_ri_rates checks this field
    # FIRST and returns no data immediately, without querying, whenever set.
    consumption_match_value_2: str = ""  # a SECOND, separately-priced meter to ADD to the primary one - added
    consumption_multiplier_2: float = 0  # 2026-08 for resource-based-consumption services (Container Instances,
                                          # Container Apps Dedicated profile) that bill vCPU-hours and GB-hours as
                                          # two genuinely independent meters under the same product, rather than
                                          # one meter scaled by a single quantity like every other service this
                                          # app models. consumption_multiplier_2 = 0 (the default) means "no
                                          # second meter" - _fetch_azure_sp_rates skips this path entirely unless
                                          # a resolver explicitly sets both fields. PAYG and each Savings Plan
                                          # term are computed as (meter1_rate * consumption_multiplier) +
                                          # (meter2_rate * consumption_multiplier_2). Reservation is NOT extended
                                          # to support this - none of the services that need it sell Reservations
                                          # at all (verified live), so reservation_unsupported_reason should
                                          # always be set alongside this.


# ── Compute (VMs) ────────────────────────────────────────────────────────────
def _plan_compute(sku: str) -> SkuQueryPlan:
    # Verified (this is the original, already-working case): armSkuName in
    # the Retail API is exactly the vmSize Resource Graph reports.
    return SkuQueryPlan(
        supported=True, service_name=None, match_field="armSkuName",
        consumption_match_value=sku, reservation_match_value=sku,
        os_license_is_separable=True,
    )


# ── Azure Container Instances ────────────────────────────────────────────────
def _plan_container_instances(sku: str) -> SkuQueryPlan:
    # ACI has no discrete SKU tier at all - it bills continuously for the
    # actual vCPU and memory (GB) a container GROUP requests, as two
    # genuinely separate meters ("Standard vCPU Duration" $/vCPU-hour,
    # "Standard Memory Duration" $/GB-hour), summed together. This app
    # stores the SKU as "vCPU{n}_Mem{m}" (e.g. "vCPU1_Mem1.5"), built from
    # azure_conn/connector.py summing properties.containers[].properties.
    # resources.requests.{cpu,memoryInGB} - see that file's comment for a
    # disclosed, narrow limitation (only the first container in a group is
    # counted, not summed across multiple containers in the same group).
    #
    # Verified live: both meters share the SAME skuName ("Standard") and
    # BOTH have empty armSkuName - only `meterName` ("Standard vCPU
    # Duration" vs "Standard Memory Duration") actually distinguishes them,
    # which is why this needed match_field="meterName" support added to
    # commitment_pricing.py's _query_retail_items (neither armSkuName nor
    # skuName could do this alone). product_contains="Container Instances"
    # also server-side-matches the sibling "Container Instances with GPU"
    # product, but that product's meters use skuName "V100"/"K80"/"P100"
    # (GPU model names, never "Standard"), so there's no real collision -
    # confirmed live, not guessed. GPU-attached instances aren't priced
    # here at all (a real, disclosed, separate dimension not built).
    #
    # Zero Reservation entries exist for this service (verified live,
    # every region checked) - Container Instances is Consumption-only,
    # genuinely can't be reserved. Real Savings Plan data DOES exist on
    # both meters (confirmed live) - a real, current Compute SP-eligible
    # service, unlike this app's earlier false assumption that "no
    # discrete SKU" meant "not modelable at all."
    if not sku or "_Mem" not in sku or not sku.startswith("vCPU"):
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected 'vCPU1_Mem1.5' convention.")
    vcpu_str, mem_str = sku[len("vCPU"):].split("_Mem", 1)
    try:
        vcpu, mem_gb = float(vcpu_str), float(mem_str)
    except ValueError:
        return SkuQueryPlan(supported=False, reason=f"Could not parse vCPU/memory values from SKU '{sku}'.")
    return SkuQueryPlan(
        supported=True, service_name="Container Instances", match_field="meterName",
        consumption_match_value="Standard vCPU Duration", consumption_multiplier=vcpu,
        consumption_match_value_2="Standard Memory Duration", consumption_multiplier_2=mem_gb,
        product_contains="Container Instances",
        reservation_unsupported_reason="Azure Container Instances has no Reservation offering at all - verified live, it's a pure consumption service with no capacity to reserve.",
    )


# ── Azure Container Apps (Dedicated workload profile only) ────────────────────
# Real Dedicated-profile billing model (Microsoft Learn "Workload profiles in
# Azure Container Apps", verified 2026-08): unlike Container Instances, a
# Dedicated Container Apps profile does NOT bill per-container resource
# request - it bills per RUNNING NODE of a fixed, named size ("D4"/"D8"/"E16"/
# etc, exactly like a VM SKU), regardless of how many apps/replicas share that
# node ("Billing is based on the number of running profile instances" - the
# doc's own words). Each name has a fixed, documented vCPU/memory allocation
# (confirmed via `az containerapp env workload-profile list-supported`'s own
# output table, not guessed): General Purpose D4/D8/D16/D32 = 4/8/16/32 vCPU,
# 16/32/64/128 GiB; Memory Optimized E4/E8/E16/E32 = 4/8/16/32 vCPU, 32/64/128/
# 256 GiB. This app stores the SKU as "Dedicated_{ProfileName}" (e.g.
# "Dedicated_D4"). GPU profiles (NC24-A100 etc) and Confidential Compute
# profiles (DC4 etc) are real but niche - not mapped, same as this session's
# other niche-hardware exclusions.
#
# Verified live (australiaeast/eastus, correct pinned api-version - see
# below): "Dedicated vCPU Usage" and "Dedicated Memory Usage" are two
# genuinely separate meters under serviceName "Azure Container Apps" (same
# dual-meter shape as Container Instances), summed via consumption_
# multiplier/_2 set to the profile's fixed vCPU/GiB. ZERO Reservation entries
# exist for this service at all (confirmed live, 0 results, both regions) -
# matches Container Instances' pure-consumption pattern. Real Savings Plan
# data DOES exist on both meters (e.g. australiaeast Dedicated vCPU Usage:
# $0.080859 PAYG -> $0.06873015 1yr/$0.06711297 3yr) - a first manual check
# using an unversioned Retail API request found NO savingsPlan data at all
# and nearly got documented here as a real gap; re-checked with this app's
# actual pinned api-version (2023-01-01-preview, commitment_pricing.py) and
# found it immediately - the SAME api-version pitfall already documented for
# PostgreSQL/MySQL earlier this session (see [[feedback-calculator-scraping-
# methodology]]) caught again, this time before it was written down wrong.
#
# Deliberately EXCLUDED, disclosed not silently dropped: the flat "Dedicated
# Plan Management" $0.1/hour fee - this is a genuinely per-ENVIRONMENT charge
# (shared across every app/node in that environment), not a per-node cost, so
# folding it into a single node's rate would double it whenever >1 node runs
# and understate it whenever <1 (same class of "shared pooled cost, not
# resource-scoped" exclusion as Elastic Pool/Cosmos DB's pooled RI). The
# Consumption profile is NOT mapped here at all - it bills per-second/
# per-request with no $/hr concept, architecturally incompatible with this
# app's PAYG-hourly model (same reasoning as Cosmos DB Serverless) - handled
# by check_sp_eligibility's own explicit "Consumption" branch, not silently
# guessed at here.
_CONTAINER_APPS_DEDICATED_PROFILES = {
    # name: (vCPU, memory_GiB) - from `list-supported`'s own documented table
    "D4": (4, 16), "D8": (8, 32), "D16": (16, 64), "D32": (32, 128),
    "E4": (4, 32), "E8": (8, 64), "E16": (16, 128), "E32": (32, 256),
}


def _plan_container_apps(sku: str) -> SkuQueryPlan:
    if not sku or not sku.startswith("Dedicated_"):
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' isn't a Dedicated-profile SKU this app maps - the Consumption profile bills per-second with no hourly rate, and GPU/Confidential Compute Dedicated profiles aren't mapped.")
    profile_name = sku[len("Dedicated_"):]
    spec = _CONTAINER_APPS_DEDICATED_PROFILES.get(profile_name)
    if spec is None:
        return SkuQueryPlan(supported=False, reason=f"Dedicated profile '{profile_name}' isn't one of the mapped General Purpose (D4/D8/D16/D32) or Memory Optimized (E4/E8/E16/E32) sizes.")
    vcpu, mem_gib = spec
    return SkuQueryPlan(
        supported=True, service_name="Azure Container Apps", match_field="meterName",
        consumption_match_value="Dedicated vCPU Usage", consumption_multiplier=vcpu,
        consumption_match_value_2="Dedicated Memory Usage", consumption_multiplier_2=mem_gib,
        reservation_unsupported_reason="Azure Container Apps has no Reservation offering at all - verified live, zero Reservation entries exist for any workload profile.",
    )


# ── Azure Dedicated Host ─────────────────────────────────────────────────────
def _plan_dedicated_host(sku: str) -> SkuQueryPlan:
    # This app stores the real ARM sku.name directly (e.g. "DSv3-Type3") -
    # Microsoft.Compute/hostGroups/hosts has `sku` as a top-level field with
    # sku.name in exactly this hyphenated form (confirmed via Microsoft's
    # own ARM template reference, 2026-08). Verified live: pricing lives
    # under serviceName "Virtual Machines" (the SAME service as regular
    # Compute VMs, not its own), productName "{Series} Series Dedicated
    # Host" (e.g. "DSv3 Series Dedicated Host"), with real Consumption,
    # Reservation, AND Savings Plan data - genuinely a different pricing
    # dimension from renting the equivalent VM size, priced per physical
    # host, not per vCore/instance. skuName uses a space instead of the
    # real sku.name's hyphen (e.g. "Dsv3 Type3"), so the hyphen is
    # translated to a space before matching (client-side normalization
    # already lowercases and strips spaces, but doesn't touch hyphens).
    # Some newer/niche series (e.g. Confidential Compute's ECasv6) have
    # Consumption but no Reservation/Savings Plan yet - handled safely by
    # the normal "query live, no data if genuinely absent" pattern, not
    # matrix-tested exhaustively given how niche this resource type is.
    #
    # product_contains is REQUIRED here, not optional - a real bug caught
    # while building this (2026-08): _query_retail_items doesn't follow the
    # Retail API's NextPageLink, so an unscoped query against the huge
    # "Virtual Machines" service catalog silently only checks its first
    # page and can miss a real match entirely (a genuine gap in this app's
    # broader pagination handling, not specific to Dedicated Host - every
    # OTHER resolver already happens to scope tightly enough via
    # service_name+product_contains to never hit this in practice).
    if not sku or "-" not in sku:
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected 'DSv3-Type3'-style convention.")
    series = sku.split("-", 1)[0]
    match_value = sku.replace("-", " ")
    return SkuQueryPlan(
        supported=True, service_name="Virtual Machines", match_field="skuName",
        consumption_match_value=match_value, reservation_match_value=match_value,
        product_contains=f"{series} Series Dedicated Host",
    )


# ── App Service ───────────────────────────────────────────────────────────────
def _plan_app_service(sku: str) -> SkuQueryPlan:
    # Verified live: armSkuName is a long descriptive string
    # ("Azure_App_Service_Premium_v4_Plan_P1mv4"), but skuName matches the
    # ARM sku.name tier code directly ("P1mv4", or "P2 v3" WITH a space for
    # the v3 generation specifically) - match on skuName, space-insensitive.
    return SkuQueryPlan(
        supported=True, service_name="Azure App Service", match_field="skuName",
        consumption_match_value=sku, reservation_match_value=sku,
    )


# ── SQL Database / SQL Managed Instance (Provisioned, vCore purchase model) ────
# Verified live: Reservations are priced PER VCORE - armSkuName has NO vCore
# count suffix (e.g. "SQLDB_GP_Compute_Gen5") and must be multiplied by the
# resource's actual vCore count. General Purpose/Business Critical Consumption
# armSkuName DOES include the vCore count (e.g. "SQLDB_GP_Compute_Gen5_4") and
# is already the full-tier rate. Hyperscale is a verified inconsistent
# exception on BOTH sides: Reservation uses the full word "HyperScale" (not
# "HS"), and - caught only after the user pushed to verify every tier live -
# its Consumption side has no per-vCore-count SKU at all, only a single flat
# "_1" (1 vCore) meter that must be multiplied by the actual vCore count, same
# as Reservations (see consumption_multiplier on SkuQueryPlan).
#
# Business Critical was upgraded from "inferred" to verified 2026-08 (live
# request confirmed both SQLDB_BC_Compute_Gen5_4 Consumption, carrying a real
# savingsPlan, and the SQLDB_BC_Compute_Gen5 Reservation prefix).
#
# CORRECTION 2026-08: an earlier pass through this module concluded "SQL
# Managed Instance does not offer a Hyperscale tier at all" and blocked it
# outright - that was WRONG, caused by a case-sensitive search bug (searched
# armSkuName for the literal substring "HyperScale", the full-word form SQL
# Database's Reservation side uses, and found zero matches). MI Hyperscale
# genuinely exists and uses the SAME short "HS" prefix as MI's Consumption
# side convention (not the "HyperScale" full-word shift that's specific to
# SQL DATABASE's Reservation naming) - confirmed live across 3 independent
# regions (austriaeast, chilecentral, and a 5-region broad scan). Its real
# profile, also verified live, is genuinely different from every other SQL
# DB/MI tier: Consumption exists (flat per-vCore rate, IDENTICAL price
# whether querying the bare "SQLMI_HS_Compute_Gen5" armSkuName or ANY sized
# variant "_8" through "_80" - confirmed byte-for-byte identical retailPrice
# across all of them, in 3 separate regions), but ZERO Reservation entries
# exist anywhere (0 results, priceType eq 'Reservation') and ZERO Consumption
# items carry a savingsPlan array (0 of 87 checked) - MI Hyperscale is
# genuinely priceable for PAYG but not eligible for ANY commitment discount,
# unlike SQL Database Hyperscale which has both RI and SP. See
# _plan_sql_family's dedicated SQLMI+HS branch.
_SQL_RESERVATION_PREFIX = {
    ("SQLDB", "GP"): "SQLDB_GP_Compute",            # verified
    ("SQLDB", "BC"): "SQLDB_BC_Compute",             # verified
    ("SQLDB", "HS"): "SQLDB_HyperScale_Compute",     # verified - note the naming shift from the consumption side
    ("SQLMI", "GP"): "SQLMI_GP_Compute",             # verified
    ("SQLMI", "BC"): "SQLMI_BC_Compute",             # verified 2026-08 (Gen5 sized Reservation entries confirmed
                                                       # live across dozens of regions, e.g. 1907.0/3960.0 1yr/3yr)
    ("SQLMI", "HS"): "SQLMI_HS_Compute",             # verified 2026-08 the PREFIX is real (matches Consumption's
                                                       # short-form convention) - but never actually queried, since
                                                       # zero Reservation entries exist for this tier (see above);
                                                       # kept here only so the generic cons_prefix/res_prefix lookup
                                                       # doesn't need a special case just to populate a placeholder.
}
_SQL_CONSUMPTION_PREFIX = {
    ("SQLDB", "GP"): "SQLDB_GP_Compute",             # verified
    ("SQLDB", "BC"): "SQLDB_BC_Compute",             # verified
    ("SQLDB", "HS"): "SQLDB_HS_Compute",             # verified - but see _HYPERSCALE_FLAT_CONSUMPTION below,
                                                       # this prefix alone is NOT enough for Hyperscale.
    ("SQLMI", "GP"): "SQLMI_GP_Compute",             # verified
    ("SQLMI", "BC"): "SQLMI_BC_Compute",             # verified 2026-08 (Gen5 8-vCore Consumption confirmed live:
                                                       # 2.679032 PAYG, carries a real savingsPlan)
    ("SQLMI", "HS"): "SQLMI_HS_Compute",             # verified 2026-08 - flat per-vCore, see _plan_sql_family's
                                                       # dedicated SQLMI+HS branch (queries the BARE unsized
                                                       # armSkuName - no "_1" variant exists for MI, unlike SQL DB).
}
# Hardware generations other than Gen5 (DC-series, Fsv2-series, Premium-series,
# Premium-series memory-optimized - all real, selectable options on the Azure
# pricing calculator) - status is NOT uniform across them:
#
#   - Hyperscale Premium-series and Premium-series-memory-optimized: FULLY
#     WORKING on both sides, verified live with exact math (2026-08). An
#     earlier note here claimed Consumption/Savings Plan "does NOT work" for
#     these - that was wrong, based on an incomplete check (only tried
#     Gen5's "_1"-suffixed armSkuName guess). The real pattern: Consumption
#     uses the exact SAME flat armSkuName as Reservation, no suffix at all
#     ("SQLDB_HyperScale_Compute_Premium" / "..._Premium_Memory_Optimized") -
#     see _HYPERSCALE_UNIFIED_ARMSKUNAME. Verified: Premium 0.18266 PAYG/
#     vCore -> 0.146128 SP-1yr/vCore (20% off); Premium Memory Optimized
#     0.255724 -> 0.2045792 (20% off); Reservation 1yr/3yr both 35%/55% off
#     for both, matching the universal SQL DB discount pattern.
#   - Business Critical DC-series: Consumption/Savings Plan WORKING (see
#     _plan_sql_dc_series), Reservation deliberately NOT sourced (stale
#     catalog entry, calculator confirms unavailable).
#   - Hyperscale DC-series: genuinely NOT covered on either side - verified
#     live its armSkuName has an incompatible structure entirely
#     ("SQL_Database_SingleDB_Hyperscale_Compute_DC-Series_vCore" - doesn't
#     even start with "SQLDB"), so the generic pattern correctly finds
#     nothing (safe failure) rather than accidentally matching.
#   - Fsv2-series: genuinely NOT covered, checked and confirmed absent
#     (2026-08) - unlike DC-series (a real if stale catalog entry exists),
#     ZERO Consumption or Reservation entries exist anywhere in the Retail
#     Prices API for Fsv2-series SQL Database, checked with no region filter
#     at all plus several productName spelling variants. It's a selectable
#     option on the calculator, but this app's data source (the public
#     Retail API) simply has nothing published for it - explicitly marked
#     unsupported with this reason rather than left to the generic fallback
#     to fail silently. NOT marked ineligible in ri_eligibility.py/
#     sp_eligibility.py though - no calculator evidence that Azure considers
#     it policy-ineligible, only that this app can't price it; eligibility
#     and priceability are different questions (see DC-series BC for the
#     contrasting case where real calculator evidence of unavailability did
#     justify an eligibility-layer change).
#   - Premium-series/DC-series do NOT exist for General Purpose or Business
#     Critical (DC-series aside) or General Purpose (any of these) -
#     verified live (2026-08): zero Consumption items for "General Purpose"
#     + "Premium"/"DC-Series", and zero for "Business Critical" + "Premium"
#     (excluding DC) - Azure simply doesn't sell those combinations, not a
#     mapping gap.
#   - None of the above have an established internal SKU-string convention
#     in this app yet (no live/demo inventory exercises them) - the
#     successes above were verified by testing a plausible guessed SKU
#     string against the live API's math, not by confirming a
#     real KQL-scanned resource would actually produce that exact string.
#
# Gen5 remains the default/overwhelmingly common choice and the only
# hardware type fully verified end-to-end (Consumption AND Reservation) this
# session, and the only one this app's current inventory (live or demo)
# exercises.
_SQL_TIER_DISPLAY_NAME = {"GP": "General Purpose", "BC": "Business Critical", "HS": "Hyperscale"}
# Tiers whose Consumption pricing is a flat per-1-vCore meter (needs
# consumption_multiplier) rather than a pre-scaled per-vCore-count SKU.
_HYPERSCALE_FLAT_CONSUMPTION = {"HS"}
# Hardware generations (matched case/hyphen/underscore-insensitively against
# the SKU's "generation" token) whose Consumption armSkuName is IDENTICAL to
# the Reservation armSkuName (no "_1" or "_N" suffix at all) - verified live
# 2026-08 for TWO independent (family, tier) combos, see _plan_sql_family's
# unified-armSkuName branch: SQL Database Hyperscale (SQLDB_HyperScale_
# Compute_Premium[_Memory_Optimized]) AND SQL Managed Instance Business
# Critical (SQLMI_BC_Compute_Premium[_Memory_Optimized]) - despite the name,
# this set is no longer Hyperscale-exclusive.
_HYPERSCALE_UNIFIED_ARMSKUNAME = {"PREMIUM", "PREMIUMMEMORYOPTIMIZED"}
# DC-series verified working for ALL THREE SQL Database tiers, corrected
# 2026-08 via systematic Azure pricing calculator dropdown enumeration
# (browser-automated, not a one-off manual screenshot) after an earlier pass
# wrongly concluded GP/HS didn't offer it at all - see _plan_sql_dc_series
# for the full correction and evidence. Only Business Critical and Hyperscale
# block Reservation specifically; General Purpose has a real, current
# Reservation offering. SQL Managed Instance DC-series remains unchecked.
_DC_SERIES_VERIFIED = {("SQLDB", "GP"), ("SQLDB", "BC"), ("SQLDB", "HS")}
# (family, tier) combos where the calculator explicitly shows Reservations
# as unavailable ("1/3 year reserved option is not available for your
# instance selection") despite a matching Retail API catalog entry existing -
# see _plan_sql_dc_series.
_DC_SERIES_RESERVATION_BLOCKED = {("SQLDB", "BC"), ("SQLDB", "HS")}


def _plan_sql_dc_series(tier: str, vcores: int, family: str, service_name: str) -> SkuQueryPlan:
    """DC-series (confidential-computing-capable hardware) - a genuinely
    different pricing structure from Gen5.

    CORRECTED 2026-08: an earlier pass through this function concluded
    General Purpose and Hyperscale DC-series were real Azure product-catalog
    absences (GP: "zero Consumption items exist"; HS: "structurally
    incompatible armSkuName") and blocked both entirely. That was WRONG on
    both counts - caught by systematically enumerating the real Azure
    pricing calculator's dropdown options via browser automation (added
    Azure SQL Database to the calculator, read every Service
    Tier/Hardware Type combobox directly from the page DOM, cross-checked
    each against the Retail API) instead of continuing to trust the
    original, apparently too-narrow manual check. Real, verified picture for
    all three tiers (East US, matching the calculator's own displayed
    price/discount %, not just a raw catalog row):

      - General Purpose: Consumption real ($0.304/vCore/hr, pre-scaled -
        verified 1/2/4/8/10/12/...40 vCore all scale exactly proportionally),
        Savings Plan real (~22-24% off), Reservation real and CURRENT -
        calculator shows ~35%/~55% off as available, not grayed out.
      - Business Critical: Consumption + Savings Plan real (~23-24% off),
        Reservation explicitly unavailable per the calculator ("1/3 year
        reserved option is not available for your instance selection").
      - Hyperscale: Consumption + Savings Plan real (~20% off, flat
        per-1-vCore meter like every other Hyperscale hardware type),
        Reservation explicitly unavailable per the calculator (shown as
        "(~0% discount)" / "not available for your instance selection").

    A real Retail API Reservation catalog ROW exists for ALL THREE tiers
    (all sharing the identical effectiveStartDate of 2021-07-07, including
    General Purpose's which IS genuinely purchasable) - this proved
    effectiveStartDate staleness is NOT a reliable signal of purchasability
    on its own, contrary to what the original Business Critical
    investigation assumed. The calculator's own displayed availability is
    the only trustworthy signal for this question; that's why
    _DC_SERIES_RESERVATION_BLOCKED is keyed directly off calculator
    observations, not catalog metadata.

    Consumption matches on skuName - General Purpose/Business Critical use a
    sized value (e.g. "40 vCore") with BOTH retailPrice and the nested
    savingsPlan.unitPrice already pre-scaled per the queried vCore count (the
    OPPOSITE of Gen5, where savingsPlan stays flat) - no multiplier needed.
    Hyperscale instead uses the flat per-1-vCore pattern common to every
    other Hyperscale hardware type in this file (skuName "1 vCore",
    consumption_multiplier=vcores) - confirmed live, its Consumption/SP
    prices at 1/1 vCore are byte-identical to what a naive sized-skuName
    query would have wrongly assumed was tier-specific.

    Reservation, where available (General Purpose only), ALSO matches on
    skuName but with the flat, unsized value "vCore" (not "{vcores} vCore" -
    confirmed live, GP's Reservation entries only ever carry a bare "vCore"
    skuName regardless of term) plus reservation_multiplier=vcores - the
    same match_field ("skuName") works for both sides because product_contains
    already scopes the query to the correct tier's productName, so a generic
    "vCore" skuName match can't accidentally cross-match another tier's rows.

    SQL Managed Instance DC-series remains genuinely unchecked - not
    verified either way."""
    if (family, tier) not in _DC_SERIES_VERIFIED:
        return SkuQueryPlan(supported=False, reason=f"DC-series pricing is only verified for SQL Database (General Purpose/Business Critical/Hyperscale) this session - '{family}' tier '{tier}' is not covered.")
    tier_display = _SQL_TIER_DISPLAY_NAME.get(tier, tier)
    reservation_unsupported_reason = None
    reservation_match_value = "vCore"
    if (family, tier) in _DC_SERIES_RESERVATION_BLOCKED:
        reservation_match_value = ""
        reservation_unsupported_reason = (
            f"Azure's pricing calculator explicitly shows Reservations as unavailable for DC-series {tier_display} "
            "('1/3 year reserved option is not available for your instance selection', shown as a ~0% discount) - "
            "a matching Retail API catalog entry exists, but the calculator's own displayed availability is the "
            "authoritative signal here, not the raw catalog row."
        )
    if tier == "HS":
        return SkuQueryPlan(
            supported=True, service_name=service_name, match_field="skuName",
            consumption_match_value="1 vCore",
            reservation_match_value=reservation_match_value,
            consumption_multiplier=vcores,
            reservation_multiplier=vcores,
            product_contains=f"{tier_display} - Compute DC-Series",
            reservation_unsupported_reason=reservation_unsupported_reason,
        )
    return SkuQueryPlan(
        supported=True, service_name=service_name, match_field="skuName",
        consumption_match_value=f"{vcores} vCore",
        reservation_match_value=reservation_match_value,
        reservation_multiplier=vcores,
        product_contains=f"{tier_display} - Compute DC-Series",
        reservation_unsupported_reason=reservation_unsupported_reason,
    )


def _plan_sql_family(sku: str, family: str, service_name: str, redundancy: str = "N/A") -> SkuQueryPlan:
    """sku expected as 'TIER_Generation_vCores' (e.g. 'GP_Gen5_4') - this
    app's existing seed-data convention, and also what Resource Graph's
    properties.currentSku.name + properties.currentSku.capacity are combined
    into by azure_conn/connector.py's query (see the sqlSku extend clause).
    'Generation' of 'Serverless' (e.g. 'GP_Serverless_4') is a real, distinct
    billing model - routed to _plan_sql_serverless instead of the Provisioned
    path below. 'Generation' of 'DC-Series'/'DC' is likewise routed to
    _plan_sql_dc_series. redundancy only matters for the unified-armSkuName
    branch below (SQL MI Business Critical Premium-series splits Zone-
    Redundant pricing into a separate armSkuName) - every other branch is
    already redundancy-agnostic because Zone-Redundant pricing there is a
    meterName-tagged variant of the SAME armSkuName, filtered downstream by
    _filter_by_redundancy in commitment_pricing.py."""
    parts = (sku or "").split("_")
    if len(parts) < 3 or not parts[-1].isdigit():
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match either the expected TIER_Generation_vCores (Provisioned, e.g. GP_Gen5_4) or TIER_Serverless_vCores pattern - can't size a reservation without a known vCore count. If this is a DTU-purchase-model database (e.g. 'S0', 'P1', Basic/Standard/Premium tiers), that's a separate, currently-unmapped naming scheme - verified live that DTU-tier SQL Database has zero Savings Plan and zero Reservation entries in the Retail API at all, so there would be nothing to fetch regardless.")
    tier, generation, vcores = parts[0].upper(), "_".join(parts[1:-1]), int(parts[-1])

    # CORRECTED 2026-08, live-confirmed via the user's own real free-tier SQL
    # Database: two genuinely different Serverless naming conventions exist
    # and BOTH must be recognized. This app's own demo/seed data uses
    # "TIER_Serverless_vCores" (e.g. "GP_Serverless_4", generation =
    # "Serverless" outright) - but a REAL Azure Resource Graph scan reports
    # "TIER_S_Generation_vCores" instead (e.g. "GP_S_Gen5_1" - confirmed both
    # from a real exported ARM template AND a real live inventory scan of
    # the user's own free-tier database). Missing the second form was a real
    # silent-failure bug: the SKU fell through to the generic Provisioned
    # branch below, constructed a non-existent armSkuName
    # ("SQLDB_GP_Compute_S_Gen5_1"), and returned supported=True with a
    # blank PAYG/SP/RI everywhere - no error, just empty cells in the UI.
    # Caught only because the user ran an actual live scan and noticed the
    # blanks - exactly the class of live-tenant-only bug flagged as
    # unverified all session.
    if generation.upper() == "SERVERLESS" or parts[1].upper() == "S":
        return _plan_sql_serverless(tier, vcores, family, service_name)
    if generation.upper().replace("-", "") in ("DC", "DCSERIES"):
        return _plan_sql_dc_series(tier, vcores, family, service_name)
    if generation.upper().replace("-", "").replace("_", "") in ("FSV2", "FSV2SERIES"):
        # Verified live 2026-08 (real user-supplied config: General Purpose,
        # Fsv2-series, East Asia): zero Consumption or Reservation entries
        # exist ANYWHERE in the Retail Prices API for Fsv2-series SQL
        # Database - checked with no region filter at all and multiple
        # productName spelling variants, not just this one region. Unlike
        # DC-series (where a real, if stale, catalog entry exists), there is
        # NO entry at all for Fsv2 - genuinely absent from this API, not
        # just differently named. The generic fallback below would have
        # already failed safely (queries would find nothing), but without
        # an accurate reason - this makes that explicit rather than silent.
        return SkuQueryPlan(supported=False, reason="Fsv2-series is a selectable hardware option on Azure's pricing calculator, but verified live that zero Consumption or Reservation entries exist for it anywhere in the Retail Prices API (checked across regions and multiple naming variants) - no pricing data to fetch regardless of region or vCore count.")

    key = (family, tier)
    cons_prefix = _SQL_CONSUMPTION_PREFIX.get(key)
    res_prefix = _SQL_RESERVATION_PREFIX.get(key)
    if not cons_prefix or not res_prefix:
        return SkuQueryPlan(supported=False, reason=f"No verified pricing pattern yet for {family} tier '{tier}' (only General Purpose/Business Critical/Hyperscale are mapped).")

    gen_key = generation.upper().replace("-", "").replace("_", "")
    # Premium-series / Premium-series-memory-optimized: a distinct Consumption
    # pattern verified live 2026-08 for THREE independent (family, tier)
    # combos - SQL Database Hyperscale, SQL Managed Instance Business
    # Critical, AND SQL Managed Instance General Purpose (confirmed
    # symmetric with BC: same unified armSkuName pattern, same separate
    # "_ZR" split, Memory-Optimized variant confirmed too). Unlike Gen5
    # (which needs a separate "..._1"-suffixed or "..._{vcores}"-suffixed
    # armSkuName), these use the exact SAME flat armSkuName for BOTH
    # Consumption and Reservation - no "_1" or "_N" suffix at all. Verified:
    # SQLDB_HyperScale_Compute_Premium (0.18266 PAYG/vCore, 0.146128
    # SP-1yr/vCore = 20% off); SQLMI_BC_Compute_Premium (0.352 PAYG/vCore,
    # 0.2816 SP-1yr/vCore = 20% off, eastus2); SQLMI_GP_Compute_Premium
    # (0.176 PAYG/vCore, 0.1408 SP-1yr/vCore = 20% off, eastus2, real
    # Reservation 1yr $1002/3yr $2081) - all flat per-vCore on both sides
    # needing the same consumption_multiplier treatment as Gen5 Hyperscale's
    # "_1" pattern. NOT verified for SQL Managed Instance Hyperscale - see
    # the dedicated SQLMI+HS branch below, which is Consumption-only and
    # genuinely has no Premium-series confirmation either way; if a
    # Hyperscale+Premium-series MI SKU is ever seen, it falls through to
    # that branch's generic bare-armSkuName construction, which safely
    # finds nothing rather than guessing wrong if the combination isn't real.
    is_unified_armskuname_case = gen_key in _HYPERSCALE_UNIFIED_ARMSKUNAME and (
        (family == "SQLDB" and tier in _HYPERSCALE_FLAT_CONSUMPTION)
        or (family == "SQLMI" and tier in ("BC", "GP"))
    )
    if is_unified_armskuname_case:
        unified_armskuname = f"{res_prefix}_{generation}"
        is_zone_redundant = (redundancy or "").strip().lower() in ("zone redundant", "zoneredundant", "zr")
        if is_zone_redundant and family == "SQLMI" and tier in ("BC", "GP"):
            # SQL MI Business Critical AND General Purpose Premium-series
            # both split Zone-Redundant pricing into a WHOLLY SEPARATE
            # armSkuName (verified live 2026-08 for both tiers, e.g.
            # "SQLMI_BC_Compute_Premium" / "SQLMI_GP_Compute_Premium" only
            # return non-redundant "vCore" meterName items; the Zone-
            # Redundant price lives under a separate "..._ZR" armSkuName
            # instead - confirmed for Premium AND Premium_Memory_Optimized,
            # both tiers), NOT a meterName-tagged variant of the base SKU
            # the way Gen5 works. The downstream meterName-based
            # _filter_by_redundancy in commitment_pricing.py can't find this
            # - it never queries the "_ZR" SKU string at all - so the
            # correct armSkuName has to be selected up front, here. SQL
            # Database Hyperscale Premium-series was checked for the same
            # pattern and does NOT have it (zero results for a
            # "..._Premium_ZR" armSkuName, and the base armSkuName carries
            # no Zone-Redundant meter either - Azure genuinely doesn't offer
            # Zone-Redundant Hyperscale Premium-series), so this suffix is
            # deliberately scoped to SQL Managed Instance only.
            unified_armskuname = f"{unified_armskuname}_ZR"
        return SkuQueryPlan(
            supported=True, service_name=service_name, match_field="armSkuName",
            consumption_match_value=unified_armskuname,
            reservation_match_value=unified_armskuname,
            reservation_multiplier=vcores,
            consumption_multiplier=vcores,
        )

    if family == "SQLMI" and tier == "HS":
        # SQL Managed Instance Hyperscale - genuinely exists (corrected
        # 2026-08 after wrongly concluding it didn't, see the module-level
        # comment above _SQL_RESERVATION_PREFIX), but has a real pricing
        # profile that's DIFFERENT from every other SQL DB/MI tier, verified
        # live across 3 independent regions: Consumption is a FLAT per-vCore
        # rate, byte-for-byte IDENTICAL whether querying the bare unsized
        # "SQLMI_HS_Compute_Gen5" armSkuName or ANY sized variant ("_8"
        # through "_80") - unlike SQL Database's Hyperscale Gen5, which
        # needs a specific "_1"-suffixed item (MI has NO "_1" variant at
        # all, confirmed 0 results), the bare unsized name is queried
        # directly here since it's confirmed to always carry the flat rate.
        # ZERO Reservation entries exist anywhere for this tier (0 results,
        # priceType eq 'Reservation') and ZERO Consumption items carry a
        # savingsPlan array (0 of 87 checked) - MI Hyperscale is priceable
        # for PAYG but genuinely not eligible for ANY commitment discount.
        return SkuQueryPlan(
            supported=True, service_name=service_name, match_field="armSkuName",
            consumption_match_value=f"{cons_prefix}_{generation}",
            reservation_match_value="",
            consumption_multiplier=vcores,
            reservation_unsupported_reason=(
                "SQL Managed Instance Hyperscale has zero Reservation entries in the Retail Prices API - "
                "verified live, Azure does not sell Reserved Capacity for this tier. Savings Plan is also "
                "unavailable (zero Consumption items carry any savingsPlan pricing) - Hyperscale is a real, "
                "priceable MI tier, but genuinely not eligible for any commitment discount."
            ),
        )

    if tier in _HYPERSCALE_FLAT_CONSUMPTION:
        # Verified live: only a flat "_1" (1 vCore) Consumption meter exists
        # for Hyperscale Gen5 Provisioned - no per-vCore-count SKU the way
        # GP/BC have. Since this queries that flat "_1" item directly,
        # retailPrice AND its nested savingsPlan.unitPrice are BOTH already
        # on the same per-1-vCore basis (verified live: 0.217365 PAYG /
        # 0.173892 SP-1yr for the same "_1" item) - unlike GP/BC where
        # retailPrice is pre-scaled but savingsPlan stays flat (needing the
        # separate savings_plan_multiplier below), here ONE multiplier
        # (consumption_multiplier) correctly scales both; do NOT also set
        # savings_plan_multiplier or this would double-apply the vCore factor.
        return SkuQueryPlan(
            supported=True, service_name=service_name, match_field="armSkuName",
            consumption_match_value=f"{cons_prefix}_{generation}_1",
            reservation_match_value=f"{res_prefix}_{generation}",
            reservation_multiplier=vcores,
            consumption_multiplier=vcores,
        )

    return SkuQueryPlan(
        supported=True, service_name=service_name, match_field="armSkuName",
        consumption_match_value=f"{cons_prefix}_{generation}_{vcores}",
        reservation_match_value=f"{res_prefix}_{generation}",
        reservation_multiplier=vcores,
        savings_plan_multiplier=vcores,
    )


def _plan_sql_serverless(tier: str, vcores: int, family: str, service_name: str) -> SkuQueryPlan:
    """Serverless SQL Database (General Purpose and Hyperscale only in real
    Azure - Business Critical and Managed Instance don't offer it, though
    passing either through here just safely finds zero matching items rather
    than a wrong number). Verified live: billed as a flat per-1-vCore rate
    (skuName '1 vCore', armSkuName often blank) regardless of the resource's
    configured max autoscale vCore ceiling - multiply by that ceiling as the
    best available approximation (this app's static inventory model has no
    concept of actual runtime autoscaled usage). Has real Savings Plan
    support (verified: 13 of 14 General Purpose Serverless Gen5 items carry a
    savingsPlan) but ZERO Reservation entries (verified live, 0 results) -
    architecturally sensible, you can't reserve fixed capacity against an
    auto-scaling resource. Leaving reservation_match_value empty means the RI
    fetch naturally finds nothing and returns None, without needing a
    separate unsupported flag."""
    tier_display = _SQL_TIER_DISPLAY_NAME.get(tier)
    if not tier_display:
        return SkuQueryPlan(supported=False, reason=f"No verified Serverless pricing pattern for tier '{tier}'.")
    return SkuQueryPlan(
        supported=True, service_name=service_name, match_field="skuName",
        consumption_match_value="1 vCore",
        reservation_match_value="",
        product_contains=f"{tier_display} - Serverless",
        consumption_multiplier=vcores,
    )


def _plan_sql_database(sku: str, redundancy: str = "N/A") -> SkuQueryPlan:
    return _plan_sql_family(sku, "SQLDB", "SQL Database", redundancy)


def _plan_sql_managed_instance(sku: str, redundancy: str = "N/A") -> SkuQueryPlan:
    return _plan_sql_family(sku, "SQLMI", "SQL Managed Instance", redundancy)


# ── Azure Synapse Analytics ─────────────────────────────────────────────────
import re

_SYNAPSE_DWU_RE = re.compile(r"^DW(\d+)C$", re.IGNORECASE)
_SYNAPSE_RESERVATION_UNIT_DWU = 100
_SYNAPSE_RESERVATION_UNIT_SKU = "DW100c"


def _plan_synapse(sku: str) -> SkuQueryPlan:
    # Verified live: skuName equals the cDWU code exactly (e.g. "DW500c"),
    # matching this app's SKU convention directly. armSkuName carries an
    # unrelated "SQL_" prefix, so match on skuName instead. Consumption
    # pricing scales perfectly linearly per 100 DWU (DW100c=$1.51/hr,
    # DW500c=$7.55/hr - verified 5x) so the direct skuName match is already
    # the full-tier rate, same as SQL DB's Consumption side. Reservations,
    # however, are ONLY sold at the DW100c unit (verified live: querying
    # priceType eq 'Reservation' for this service returns exactly 2 rows,
    # both armSkuName 'SQL_DW100c', regardless of what skuName is asked for)
    # - a DW500c reservation must be priced as 5x the DW100c reservation and
    # is NOT itself a separate catalog entry. Also verified: zero Consumption
    # items for any Synapse tier carry a savingsPlan array - Azure sells no
    # Savings Plan for this service at all, so the SP side naturally resolves
    # to None without needing a separate unsupported flag.
    match = _SYNAPSE_DWU_RE.match((sku or "").strip())
    if not match:
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected 'DW<n>c' cDWU pattern (e.g. DW500c).")
    dwu = int(match.group(1))
    return SkuQueryPlan(
        supported=True, service_name="Azure Synapse Analytics", match_field="skuName",
        consumption_match_value=sku, reservation_match_value=_SYNAPSE_RESERVATION_UNIT_SKU,
        reservation_multiplier=max(1, dwu // _SYNAPSE_RESERVATION_UNIT_DWU),
    )


# ── Azure Cache for Redis (classic Microsoft.Cache/redis - Basic/Standard/
# Premium only) ──────────────────────────────────────────────────────────────
def _plan_redis(sku: str) -> SkuQueryPlan:
    # Verified live: classic Standard/Basic (C-series) Redis has ZERO
    # Reservation entries in the Retail Prices API - Azure genuinely doesn't
    # sell them (confirmed empty result set, not a naming mismatch). Premium
    # (P-series) does. No Savings Plan exists for Redis at any tier
    # (verified - 0 of 10 Premium Consumption items carried a savingsPlan
    # array), consistent with this app's sp_eligibility.py already excluding
    # Redis from Savings Plan entirely.
    #
    # CORRECTION (2026-08-15): this function used to also check for an
    # "ENTERPRISE" token in the SKU string, but that was dead code - Redis
    # Enterprise (aka Azure Managed Redis) is a genuinely SEPARATE ARM
    # resource type (Microsoft.Cache/redisEnterprise, confirmed via
    # Microsoft's own template reference) with `sku` as a TOP-LEVEL resource
    # field, not nested under `properties.sku` like classic Redis - and a
    # completely different SKU taxonomy (e.g. "Enterprise_E20",
    # "Balanced_B10") that could never have matched this function's
    # "{family}{capacity}_{tier}" parsing anyway even if the string check
    # had passed. See _plan_redis_enterprise below, now built as its own
    # resource type "Azure Cache for Redis Enterprise".
    s = (sku or "").upper()
    if "PREMIUM" not in s:
        return SkuQueryPlan(
            supported=False,
            reason="Azure only sells Reserved Instances for the Premium Redis tier - Standard/Basic (C-series) has no reservation offering at all (verified live: zero API results, not a lookup gap).",
        )
    # Expected format from azure_conn/connector.py: "{family}{capacity}_{tier}",
    # e.g. "P2_Premium" - matches armSkuName's "..._P2_Cache" / skuName "P2".
    tier_code = sku.split("_")[0].upper() if sku else ""
    if not tier_code:
        return SkuQueryPlan(supported=False, reason=f"Could not extract a Premium tier code (e.g. P1-P5) from SKU '{sku}'.")
    return SkuQueryPlan(
        supported=True, service_name="Redis Cache", match_field="skuName",
        consumption_match_value=tier_code, reservation_match_value=tier_code,
        product_contains="Premium",
    )


# ── Azure Cache for Redis Enterprise (Microsoft.Cache/redisEnterprise, aka
# "Azure Managed Redis" - a genuinely separate ARM resource type/SKU
# taxonomy from classic Redis, confirmed via Microsoft's own template
# reference, 2026-08) ────────────────────────────────────────────────────────
# This app stores these SKUs as the real ARM sku.name value directly (e.g.
# "Balanced_B10", "Enterprise_E20", "MemoryOptimized_M50") - no
# transformation needed, azure_conn/connector.py's existing generic
# top-level `sku.name` fallback already captures this correctly.
#
# Verified live across all 6 real families (Balanced/MemoryOptimized/
# ComputeOptimized/FlashOptimized - the newer "Azure Managed Redis"
# branding - and the older Enterprise/EnterpriseFlash naming, which
# coexists in the Retail API under the SAME serviceName "Redis Cache"):
# skuName is always the bare size code (e.g. "B10", "E20", "M700") matching
# the ARM sku.name's suffix exactly, every family has REAL Consumption AND
# Reservation entries (unlike classic Redis, where Reservation is
# Premium-only), and genuinely ZERO Savings Plan anywhere (0 of every
# family's Consumption items carried a savingsPlan array) - matching
# classic Redis's already-established "no Savings Plan for Redis" finding.
_REDIS_ENTERPRISE_FAMILY_PRODUCTS = {
    "BALANCED":         "Azure Managed Redis - Balanced",
    "MEMORYOPTIMIZED":  "Azure Managed Redis - Memory Optimized",
    "COMPUTEOPTIMIZED": "Azure Managed Redis - Compute Optimized",
    "FLASHOPTIMIZED":   "Azure Managed Redis - Flash Optimized",
    "ENTERPRISE":       "Azure Redis Cache Enterprise",
    "ENTERPRISEFLASH":  "Azure Redis Cache Enterprise Flash",
}


def _plan_redis_enterprise(sku: str) -> SkuQueryPlan:
    if not sku or "_" not in sku:
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected 'Balanced_B10'/'Enterprise_E20'-style convention.")
    family, code = sku.split("_", 1)
    product_contains = _REDIS_ENTERPRISE_FAMILY_PRODUCTS.get(family.upper().replace(" ", "").replace("-", ""))
    if not product_contains:
        return SkuQueryPlan(supported=False, reason=f"Unrecognized Redis Enterprise family '{family}' - expected Balanced, MemoryOptimized, ComputeOptimized, FlashOptimized, Enterprise, or EnterpriseFlash.")
    return SkuQueryPlan(
        supported=True, service_name="Redis Cache", match_field="skuName",
        consumption_match_value=code, reservation_match_value=code,
        product_contains=product_contains,
    )


# ── Azure Database for PostgreSQL / MySQL Flexible Server ──────────────────
# This app stores these SKUs as "{tier}_{arm sku.name}" (e.g.
# "GeneralPurpose_Standard_D2ds_v5") - tier and sku.name are the two real,
# top-level ARM fields (Microsoft.DBfor{Postgre,My}SQL/flexibleServers'
# sku.tier/sku.name - confirmed identical schema shape for both services via
# Microsoft's own ARM template reference, 2026-08) needed to build the right
# Retail Prices API query. sku.name alone isn't enough: it's genuinely a
# real, direct VM SKU name (e.g. "Standard_D2ds_v5"), but the Retail API's
# PRICING data for General Purpose/Memory Optimized is published almost
# entirely through one FLAT per-vCore meter per tier+series (armSkuName
# ending "..._Series_Compute_vCore"/skuName literally "vCore") rather than a
# per-size sized meter - verified live this flat meter always exists (even
# for series that ALSO happen to have a sized direct-VM-name Consumption
# meter) and gives byte-identical pricing to the sized meter where both
# exist, so it's used uniformly here rather than needing two lookup
# strategies. Needs consumption_multiplier AND reservation_multiplier = the
# resource's actual vCore count (parsed from sku.name) on BOTH sides -
# unlike SQL DB Gen5 where only the Reservation side needed multiplying,
# neither side is pre-scaled here.
#
# Burstable tier has NO Reservation offering at all (verified live: zero
# Reservation entries for any B-series SKU, either service, in every region
# checked - matches the calculator, which shows no Reservation option for
# Burstable) - Consumption pricing for Burstable uses a direct sized
# armSkuName/skuName match instead, since no flat per-vCore meter exists
# for it.
#
# Savings Plan is a genuinely different story, and caught a real research
# mistake worth recording: an initial pass concluded NO savingsPlan data
# exists anywhere for either service (checked via `curl` with no
# `api-version` pinned) - but this app's own commitment_pricing.py pins
# `api-version=2023-01-01-preview`, and re-checking through that exact
# version (matching what the app actually queries) shows real savingsPlan
# data on the vast majority of Consumption items for BOTH services,
# including Burstable - confirmed with current effectiveStartDate values
# (2023-03-01, same as the confirmed-current General Purpose meters, not an
# old/stale listing). This directly contradicts the pricing calculator,
# which shows no "Savings Options" section at all for Burstable - the same
# kind of calculator-vs-live-API disagreement already seen with SQL MI
# Hyperscale, where the user's explicit call was to trust the live,
# current-dated Retail API data over what the calculator's UI currently
# exposes. Applying that same precedent here: Savings Plan pricing IS
# fetched for Burstable too, not blocked. (Lesson: any manual research
# against this API MUST pin the same api-version this app's
# commitment_pricing.py uses, or "no data" can be a false negative - see
# feedback-calculator-scraping-methodology.)
_FLEX_SERIES_RE = re.compile(r"^(?:Standard_)?([A-Za-z]+?)(\d+)([A-Za-z]*?)(?:_v(\d+))?$")


def _parse_flex_sku(sku: str):
    """Splits this app's "{tier}_{arm sku.name}" convention into
    (tier, arm_sku_name, series_code, vcores), or None if malformed."""
    if not sku or "_" not in sku:
        return None
    tier, arm_sku_name = sku.split("_", 1)
    m = _FLEX_SERIES_RE.match(arm_sku_name.strip())
    if not m:
        return None
    prefix, vcores, suffix, ver = m.groups()
    series_code = f"{prefix}{suffix}v{ver}" if ver else f"{prefix}{suffix}"
    return tier, arm_sku_name, series_code, int(vcores)


def _is_confidential_series(series_code: str) -> bool:
    # Confidential Compute (SGX/AMD SEV-SNP-backed) VM series are prefixed
    # "EC" (Intel) or "DC" (AMD/Intel, older naming) - verified live this is
    # a REAL, SEPARATE Retail Prices API product category
    # ("...Confidential Compute {series} Series Compute") for both
    # PostgreSQL and MySQL, distinct from "Memory Optimized"/"Business
    # Critical", even though there's no separate ARM sku.tier value for it
    # (confirmed via Microsoft's own template reference - the enum is only
    # Burstable/GeneralPurpose/MemoryOptimized) and even though the
    # PostgreSQL pricing calculator's own Tier dropdown lists these EC/DC
    # series under "Memory Optimized" as a UI convenience. A resource
    # running a Confidential Compute series always reports
    # sku.tier="MemoryOptimized" - confidentiality is a property of the VM
    # series choice alone, not a distinct tier value - so this can only be
    # detected from the series code, never from tier.
    return series_code[:2] in ("EC", "DC")


def _plan_postgresql(sku: str) -> SkuQueryPlan:
    parsed = _parse_flex_sku(sku)
    if not parsed:
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected 'GeneralPurpose_Standard_D2ds_v5'-style convention.")
    tier, arm_sku_name, series_code, vcores = parsed

    if tier == "Burstable":
        # Verified live: PostgreSQL's Burstable skuName is the raw,
        # un-prefixed VM size (e.g. "B2ms", "B20ms") even though armSkuName
        # keeps "Standard_" - stripping the prefix here matches skuName
        # directly (case handled by the existing normalize-and-compare in
        # commitment_pricing.py's _query_retail_items).
        vm_size = arm_sku_name.split("_", 1)[-1] if arm_sku_name.lower().startswith("standard_") else arm_sku_name
        return SkuQueryPlan(
            supported=True, service_name="Azure Database for PostgreSQL", match_field="skuName",
            consumption_match_value=vm_size, reservation_match_value=vm_size,
            product_contains="Flexible Server Burstable BS Series Compute",
        )

    if tier not in ("GeneralPurpose", "MemoryOptimized"):
        return SkuQueryPlan(supported=False, reason=f"Unrecognized PostgreSQL Flexible Server tier '{tier}' - expected Burstable, GeneralPurpose, or MemoryOptimized (the real ARM sku.tier enum).")

    if tier == "MemoryOptimized" and series_code == "Mdsv2":
        # Real, confirmed exception found via a systematic full-catalog scan
        # (2026-08-15): the M-series memory-optimized hardware line publishes
        # under its own bare "Azure Database for PostgreSQL Flexible Server
        # Mdsv2 Series Compute" product - no "Memory Optimized" (or any
        # tier) prefix at all, unlike every other series. It's also priced
        # DIFFERENTLY from every other series this app handles: the sized
        # Consumption item itself (skuName e.g. "M64ds_v2") already carries
        # a correctly-scaled `retailPrice` AND its own nested `savingsPlan`
        # array (confirmed live, current effectiveStartDate) - there's no
        # separate flat per-vCore meter, and none is needed. Genuinely
        # Reservation-less (zero entries confirmed live). Not reachable via
        # this app's PostgreSQL calculator scrape (Memory Optimized's
        # Instance Series dropdown didn't list it) - a real, current,
        # substantially-priced ($13-$76/hr range) tier the Retail API sells
        # that the calculator's UI doesn't currently surface, same class of
        # finding as the DC-series Confidential Compute additions above.
        vm_size = arm_sku_name.split("_", 1)[-1] if arm_sku_name.lower().startswith("standard_") else arm_sku_name
        return SkuQueryPlan(
            supported=True, service_name="Azure Database for PostgreSQL", match_field="skuName",
            consumption_match_value=vm_size, reservation_match_value=vm_size,
            product_contains="Flexible Server Mdsv2 Series Compute",
        )

    if tier == "GeneralPurpose":
        tier_display = "General Purpose"
        flat_sku_label = "vCore"
    elif _is_confidential_series(series_code):
        # Confirmed live: unlike General Purpose/Memory Optimized, PostgreSQL's
        # Confidential Compute product publishes only a "1 vCore"-labeled flat
        # meter (no bare "vCore" row) - and has ZERO Reservation entries at
        # all (confirmed live, every Confidential Compute series checked).
        tier_display = "Confidential Compute"
        flat_sku_label = "1 vCore"
    else:
        tier_display = "Memory Optimized"
        flat_sku_label = "vCore"

    return SkuQueryPlan(
        supported=True, service_name="Azure Database for PostgreSQL", match_field="skuName",
        consumption_match_value=flat_sku_label, reservation_match_value=flat_sku_label,
        product_contains=f"Flexible Server {tier_display} {series_code} Series Compute",
        consumption_multiplier=vcores, reservation_multiplier=vcores,
    )


def _plan_mysql(sku: str) -> SkuQueryPlan:
    parsed = _parse_flex_sku(sku)
    if not parsed:
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected 'GeneralPurpose_Standard_D2ds_v5'-style convention.")
    tier, arm_sku_name, series_code, vcores = parsed

    if tier == "Burstable":
        # Verified live: MySQL's Burstable skuName convention is the
        # OPPOSITE of PostgreSQL's - it KEEPS the "Standard_" prefix (e.g.
        # "Standard_B12ms"), so match the full arm_sku_name as-is. One
        # confirmed, disclosed, narrow gap: the smallest size (B1ms)
        # publishes as bare "B1MS" (no prefix on either field) for MySQL
        # specifically, so that one size alone won't match here and will
        # safely resolve to no pricing data rather than a wrong number -
        # not chased further given how narrow it is (smallest possible
        # Burstable size, and Burstable has no RI/SP to lose anyway).
        return SkuQueryPlan(
            supported=True, service_name="Azure Database for MySQL", match_field="skuName",
            consumption_match_value=arm_sku_name, reservation_match_value=arm_sku_name,
            product_contains="Flexible Server Burstable BS Series Compute",
        )

    if tier == "GeneralPurpose":
        if series_code == "Ddsv4":
            # Same class of exception as Memory Optimized's Edsv4 below:
            # confirmed live this older generation has no series-tagged
            # productName of its own - it's folded into the generic
            # "General Purpose Series Compute" product (found via a
            # systematic full-catalog scan, 2026-08-15). Confirmed this
            # generic product also happens to house a "Dasv4"-style AMD
            # series at the IDENTICAL flat per-vCore rate within any given
            # region (spot-checked denmarkeast: $0.11115/vCore for both),
            # so matching by tier alone here is safe - no ambiguity from
            # broadening past the specific series code.
            product_contains = "Flexible Server General Purpose Series Compute"
        else:
            product_contains = f"Flexible Server General Purpose {series_code} Series Compute"
        return SkuQueryPlan(
            supported=True, service_name="Azure Database for MySQL", match_field="skuName",
            consumption_match_value="vCore", reservation_match_value="vCore",
            product_contains=product_contains,
            consumption_multiplier=vcores, reservation_multiplier=vcores,
        )

    if tier == "MemoryOptimized":
        # CORRECTION (2026-08-15): an earlier pass concluded MySQL's
        # ARM MemoryOptimized tier maps ONLY to a "Business Critical Ev3"
        # Retail API product, with no series-specific naming at all - built
        # from a single-region (eastus) manual `curl` scan that (as later
        # discovered) used the WRONG, unversioned api-version, which
        # returns a genuinely SMALLER product catalog than this app's own
        # pinned version. Re-scanning with the correct
        # api-version=2023-01-01-preview across multiple regions shows
        # MySQL's modern series DO use "Memory Optimized {series} Series
        # Compute" naming, identical to PostgreSQL (Eadsv5/Eadsv6/Easv6/
        # Edsv5/Edsv6/Esv6 all confirmed live) - "Business Critical Ev3" is
        # a real but LEGACY/older naming that only applies to that one
        # specific generation, not the tier's general convention. Caught by
        # the user hand-testing a real calculator configuration (MySQL
        # Memory Optimized, Edsv4-series) that returned nothing under the
        # old logic.
        if _is_confidential_series(series_code):
            # Same Confidential Compute split as PostgreSQL - see
            # _is_confidential_series.
            return SkuQueryPlan(
                supported=True, service_name="Azure Database for MySQL", match_field="skuName",
                consumption_match_value="vCore", reservation_match_value="vCore",
                product_contains=f"Flexible Server Confidential Compute {series_code} Series",
                consumption_multiplier=vcores, reservation_multiplier=vcores,
            )
        if series_code == "Esv3":
            # The legacy Ev3 generation genuinely publishes under
            # "Business Critical Ev3 Series Compute" (not "Memory
            # Optimized Esv3", which doesn't exist) - a real, confirmed
            # naming exception for this one generation, spelled "Ev3" (no
            # "s") unlike every other series this app parses. Verified
            # live: every catalog row under this product - regardless of
            # its own nominal size label ("1 vCore", "32 vCore", or a
            # specific "Standard_E16as" AMD variant name) - carries the
            # SAME flat per-vCore rate within a region, so matching the
            # smallest confirmed-present label ("1 vCore") and multiplying
            # by the real vCore count is safe and correct. Genuinely
            # Reservation-less (zero entries across 42 regions checked,
            # still true after the api-version correction) - a real,
            # current product limitation for this one legacy generation,
            # not a lookup gap.
            return SkuQueryPlan(
                supported=True, service_name="Azure Database for MySQL", match_field="skuName",
                consumption_match_value="1 vCore", reservation_match_value="1 vCore",
                product_contains="Flexible Server Business Critical",
                consumption_multiplier=vcores, reservation_multiplier=vcores,
            )
        if series_code == "Edsv4":
            # Real, confirmed exception: unlike every other Memory
            # Optimized series, Edsv4 does NOT get its own
            # "Memory Optimized Edsv4 Series Compute" productName line -
            # it's folded into the generic, series-code-less "Memory
            # Optimized Series Compute" product instead (armSkuName is
            # still Edsv4-specific internally, productName just doesn't
            # say so). Confirmed this generic product is NOT a shared
            # catch-all for other series too (its sized Consumption items'
            # armSkuName explicitly says "..._Edsv4Series_Compute") - safe
            # to match narrowly by tier alone.
            return SkuQueryPlan(
                supported=True, service_name="Azure Database for MySQL", match_field="skuName",
                consumption_match_value="vCore", reservation_match_value="vCore",
                product_contains="Flexible Server Memory Optimized Series Compute",
                consumption_multiplier=vcores, reservation_multiplier=vcores,
            )
        return SkuQueryPlan(
            supported=True, service_name="Azure Database for MySQL", match_field="skuName",
            consumption_match_value="vCore", reservation_match_value="vCore",
            product_contains=f"Flexible Server Memory Optimized {series_code} Series Compute",
            consumption_multiplier=vcores, reservation_multiplier=vcores,
        )

    return SkuQueryPlan(supported=False, reason=f"Unrecognized MySQL Flexible Server tier '{tier}' - expected Burstable, GeneralPurpose, or MemoryOptimized (the real ARM sku.tier enum).")


# ── Azure Cosmos DB (RU/s-based APIs: NoSQL, MongoDB RU, Cassandra, Gremlin,
# Table - NOT the separate vCore-based "Azure DocumentDB"/Cosmos DB for
# PostgreSQL products, which are different ARM resource types this app
# doesn't ingest) ────────────────────────────────────────────────────────────
# This app stores these SKUs as "{CapacityMode}_{ServiceTier}_{RUs}" (e.g.
# "Standard_GeneralPurpose_400") or the bare string "Serverless".
# CapacityMode/ServiceTier are real, top-level ARM properties on
# Microsoft.DocumentDB/databaseAccounts - `properties.capabilities` (an
# array of {name} objects; "EnableServerless" signals Serverless) and
# `properties.enableMultipleWriteLocations` (a bool; Azure's real, current
# pricing page explicitly confirms "Business Critical" is just the current
# branding for what used to be called multi-write-region accounts) -
# verified via Microsoft's own ARM template reference, 2026-08. RU/s
# quantity itself is NOT one of these top-level properties - real Azure
# throughput is set per-database or per-container via a separate
# `throughputSettings` child resource, which azure_conn/connector.py
# doesn't traverse yet (a real, disclosed gap, same class as this app's
# existing "Disk Storage ingestion not reached yet" gap) - so a live
# tenant's Cosmos DB resources can be correctly tagged with capacity
# mode/tier but not yet priced end-to-end without that count.
#
# Verified live (2026-08, via the real pricing page + this app's pinned
# Retail API version) against the calculator's own stated $/100 RU/s
# figures exactly:
# - Standard Provisioned, General Purpose: skuName "RUs" on the base
#   "Azure Cosmos DB" product, $0.008/hr per 100 RU/s (= $5.84/month).
# - Autoscale Provisioned, General Purpose: skuName "AP1"-"AP4" (four
#   near-identical meters, confirmed byte-identical $0.012/hr per 100 RU/s
#   regardless of which one - the difference is a legacy "Entry Price"
#   variant at each AP-number that's always MORE expensive, so the
#   existing min()-picks-cheapest logic in commitment_pricing.py already
#   selects the correct meter without needing to know which AP-number is
#   "right") on the "Azure Cosmos DB autoscale" product ($0.012 x 100 x 730
#   = $8.76/month, matches the calculator's stated 1.5x-of-Standard rate).
# - Business Critical (either capacity mode): skuName "mRUs" ("multi-master
#   RU/s") on the SAME base "Azure Cosmos DB" product, $0.016/hr per 100
#   RU/s (= $11.68/month) - verified this is IDENTICAL for both Standard
#   and Autoscale (matches the pricing page explicitly NOT applying the
#   1.5x autoscale multiplier to Business Critical), so both capacity modes
#   route to the same meter for this tier.
# - Serverless: bills per-request ($0.25 per million RU, real, confirmed),
#   not $/hr - architecturally incompatible with this app's PAYG-hourly-rate
#   model (every other resource type has a meaningful $/hr; Serverless
#   genuinely doesn't), so marked unsupported here rather than forced into
#   a rate that doesn't represent real usage. Already correctly marked RI
#   AND SP ineligible in ri_eligibility.py/sp_eligibility.py.
#
# Reserved Capacity is deliberately NOT fetched (reservation_unsupported_
# reason set) even though real Reservation catalog entries exist - verified
# live these are sold in FIXED bucket sizes (100 RU/s, 1/2/3/5/10/20/30
# Million RU/s - not a linear per-100-RU/s rate) as a subscription-wide pool
# shared "across all regions, APIs, database accounts, and subscriptions
# under a given enrollment" (Microsoft's own words), not tied to any one
# resource's provisioned RU/s - already correctly reflected in this app's
# "capacity" coverage model (get_coverage_model() in ri_eligibility.py),
# which gates off per-resource $ RI-savings display for this exact reason.
# Savings Plan for Databases has genuinely inconsistent regional rollout on
# the "RUs" meter (confirmed live, correct api-version): absent in eastus,
# but present and exact in australiaeast (0.008096/0.0092 = 12% off,
# matching the real pricing page's advertised "12% 1yr" figure exactly) -
# a real per-region gap, not a universal one, so no code path needed beyond
# what already exists: this resolves to None automatically wherever a
# region's meter genuinely lacks the array, via the existing "no
# savingsPlan array found" fallback -
# eligibility (sp_eligibility.py) still correctly says "eligible" for
# provisioned throughput, matching the calculator.
def _plan_cosmos_db(sku: str) -> SkuQueryPlan:
    parts = (sku or "").split("_")
    capacity_mode = parts[0] if parts else ""

    if capacity_mode == "Serverless":
        return SkuQueryPlan(supported=False, reason="Serverless Cosmos DB bills per-request ($0.25 per million RU), not $/hr like every other resource type this app tracks - no meaningful hourly PAYG rate to compute. Also genuinely has no Reservation offering (can't reserve capacity for an account with nothing provisioned).")

    # "Provisioned" is a deliberately neutral third state, distinct from
    # "Standard"/"Autoscale": live Resource Graph capture can reliably tell
    # Serverless apart from everything else (properties.capabilities
    # containing "EnableServerless"), but can NOT tell Standard apart from
    # Autoscale - that distinction lives on a separate child
    # throughputSettings resource this app doesn't traverse (see below).
    # Guessing either one would silently bake in a real ~50% price error
    # for General Purpose (Standard $5.84 vs Autoscale $8.76 per 100 RU/s) -
    # "Provisioned" is honest about not knowing, not a guess.
    if capacity_mode not in ("Standard", "Autoscale", "Provisioned"):
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected 'Standard_GeneralPurpose_400' / 'Autoscale_BusinessCritical_1000' / 'Provisioned_GeneralPurpose' / 'Serverless' convention.")

    if len(parts) == 2 or capacity_mode == "Provisioned":
        # Live Resource Graph capture reaches capacity mode + service tier
        # (both real top-level ARM properties) but not the actual RU/s
        # count (a separate child throughputSettings resource this app
        # doesn't traverse yet - see the module-level comment above) - a
        # real, disclosed gap rather than a malformed SKU.
        return SkuQueryPlan(supported=False, reason="Provisioned throughput RU/s count (and, for General Purpose, whether it's Standard or Autoscale) isn't captured for this resource yet - it's set on a separate throughputSettings child resource, not the Cosmos DB account itself. Capacity mode and service tier are known, but a $ rate needs more than that.")
    if len(parts) != 3:
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected '{capacity_mode}_GeneralPurpose_400' convention.")
    tier, ru_str = parts[1], parts[2]
    if not ru_str.isdigit():
        return SkuQueryPlan(supported=False, reason=f"Could not parse a numeric RU/s count from SKU '{sku}'.")
    ru_count = int(ru_str)

    if tier == "BusinessCritical":
        sku_label, product_contains = "mRUs", ""
    elif tier == "GeneralPurpose":
        sku_label, product_contains = ("AP1", "autoscale") if capacity_mode == "Autoscale" else ("RUs", "")
    else:
        return SkuQueryPlan(supported=False, reason=f"Unrecognized Cosmos DB service tier '{tier}' - expected GeneralPurpose or BusinessCritical.")

    # Reservation side, added 2026-08-23: confirmed live against the real
    # Retail Prices API that Cosmos DB Reservations ARE priced, but only at
    # a fixed set of discrete bucket-size SKUs ("100 RU/s", "1 Million RU/s",
    # "2 Million RU/s", ... up to "30 Million RU/s", plus a parallel
    # "Multi-master" set for multi-region-write accounts) - not a single
    # continuous per-RU/s meter. "100 RU/s" is always purchasable regardless
    # of total size (confirmed: it's the SAME bucket the real "500 quantity
    # of 100 RU/s meter" worked example in Microsoft's own reservation-
    # charges docs uses for a 50,000 RU/s need), so - exactly mirroring
    # Synapse's identical "Reservations only sold at the DW100c unit, scale
    # by reservation_multiplier" precedent just above - this always prices
    # against the 100 RU/s bucket and scales linearly. This is a
    # deliberately conservative choice: if a real tenant's reservation used
    # one of the larger bucket sizes at a better effective rate, this may
    # slightly OVERstate the true cost, never understate it - same
    # "don't guess in the direction that hides real spend" principle used
    # elsewhere in this app (e.g. MemoryDB's engine default).
    return SkuQueryPlan(
        supported=True, service_name="Azure Cosmos DB", match_field="skuName",
        consumption_match_value=sku_label, reservation_match_value="100 RU/s",
        product_contains=product_contains,
        consumption_multiplier=max(1, ru_count // 100),
        reservation_multiplier=max(1, ru_count // 100),
    )


# ── Azure Spring Apps Enterprise ────────────────────────────────────────────
# Real billing model (Microsoft's own pricing page, verified 2026-08, pinned
# api-version): genuinely NOT a linear per-vCPU/per-GB rate like Container
# Instances/Container Apps - it's a flat BUNDLE fee. "For each app instance
# in the Enterprise Plan, Azure Spring Apps charges for one base unit price
# of 'Enterprise vCPU and memory group duration', which includes 12 GB of
# memory and 6 vCPUs" (Microsoft's own words) - one flat $/hr charge per app
# instance covers UP TO 6 vCPU and UP TO 12 GB, regardless of the instance's
# actual configured size within that range. Usage beyond either threshold is
# billed via separate "Enterprise Overage vCPU Duration"/"Enterprise Overage
# Memory Duration" meters - a genuinely different shape (base-bundle-plus-
# overage-above-a-threshold) than this session's existing linear dual-meter
# engine can express (which sums two ALWAYS-linear meters, no threshold/base
# concept). Rather than guess at combining base+overage without a live config
# to verify the math against, this app ONLY maps the common in-bundle case
# (<=6 vCPU AND <=12 GB, true for most real Spring Boot microservice sizing)
# as a flat single-meter rate - the SAME simplicity as Compute/App Service,
# once the real model was understood. Configs exceeding either threshold are
# explicitly unsupported here, disclosed not silently wrong.
#
# product_contains="Azure Spring Apps Enterprise" is REQUIRED - verified live
# a sibling productName "Azure Spring Apps" (the older Standard/Basic tiers,
# SP:False) carries the IDENTICAL meterName "Enterprise vCPU and Memory Group
# Duration" at the same $0.8408/hr, with no savingsPlan data - matching this
# session's existing documented distinction (SP:True only lives on the
# "...Enterprise" productName specifically). Zero Reservation entries exist
# for this service at all (verified live, 0 results) - Consumption + Savings
# Plan only, same pattern as Container Instances/Container Apps.
def _plan_spring_apps_enterprise(sku: str) -> SkuQueryPlan:
    if sku != "Enterprise":
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' isn't mapped - this app only prices an app instance within the base bundle (<=6 vCPU, <=12 GB memory), stored as the literal SKU 'Enterprise'. Instances exceeding either threshold would need Overage-meter pricing this app doesn't build yet.")
    return SkuQueryPlan(
        supported=True, service_name="Azure Spring Cloud", match_field="meterName",
        consumption_match_value="Enterprise vCPU and Memory Group Duration", consumption_multiplier=1,
        product_contains="Azure Spring Apps Enterprise",
        reservation_unsupported_reason="Azure Spring Apps Enterprise has no Reservation offering at all - verified live, zero Reservation entries exist for this service.",
    )


# ── Azure Database Migration Service ────────────────────────────────────────
# Real ARM schema (Microsoft.DataMigration/services, verified 2026-08, via
# Microsoft's own template reference): SKU is a genuinely clean, structured
# top-level `sku` object - `sku.tier` (string) + `sku.capacity` (int, the raw
# vCore count) - no string-parsing needed at all, unlike almost every other
# service this session. This app stores it as "{Tier}_{N}vCores" (e.g.
# "GeneralPurpose_4vCores"), built directly from those two fields.
#
# Verified live (australiaeast, pinned api-version): skuName in the Retail
# API is literally "{N} vCore" (e.g. "4 vCore") for ALL three tiers - tiers
# are only distinguishable via productName ("Azure Database Migration
# Service {Tier} Compute"), so product_contains is REQUIRED (a plain skuName
# match alone would silently pick whichever tier's price the API returns
# first, wrong 2 times out of 3 - confirmed live that "4 vCore" prices
# $0.2025/hr General Purpose vs $0.404/hr Premium, exactly 2x apart). Real
# Savings Plan data confirmed on all three tiers. Zero Reservation entries
# exist anywhere for this service (verified live, 0 results) - Consumption +
# Savings Plan for Databases only.
#
# One real, disclosed naming uncertainty (not guessed past): the generic ARM
# schema reference's own field description says sku.tier examples are
# "'Basic', 'General Purpose', or 'Business Critical'" - but the ACTUAL live
# Retail API billing catalog's third tier is named "Premium", not "Business
# Critical" (confirmed live, productName is literally "...Premium Compute").
# This app treats "Premium" as authoritative (matches real billing, not a
# generic doc description) - if a live tenant's sku.tier literally reports
# "Business Critical" instead, that would be the SAME kind of ARM-vs-billing
# label divergence already confirmed elsewhere this session (MySQL's
# "MemoryOptimized" ARM tier vs "Business Critical" Retail productName) -
# flagged here since live ingestion for this service isn't built yet, so it
# couldn't be directly confirmed either way.
_DMS_TIER_PRODUCT = {
    "Basic": "Azure Database Migration Service Basic Compute",
    "GeneralPurpose": "Azure Database Migration Service General Purpose Compute",
    "Premium": "Azure Database Migration Service Premium Compute",
}


def _plan_database_migration_service(sku: str) -> SkuQueryPlan:
    parts = (sku or "").split("_", 1)
    if len(parts) != 2 or not parts[1].endswith("vCores"):
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected '{{Tier}}_{{N}}vCores' convention (e.g. 'GeneralPurpose_4vCores').")
    tier, vcore_part = parts
    product = _DMS_TIER_PRODUCT.get(tier)
    if product is None:
        return SkuQueryPlan(supported=False, reason=f"Tier '{tier}' isn't one of Basic/GeneralPurpose/Premium.")
    try:
        vcores = int(vcore_part[:-len("vCores")])
    except ValueError:
        return SkuQueryPlan(supported=False, reason=f"Could not parse vCore count from SKU '{sku}'.")
    match_value = f"{vcores} vCore"
    return SkuQueryPlan(
        supported=True, service_name="Azure Database Migration Service", match_field="skuName",
        consumption_match_value=match_value, product_contains=product,
        reservation_unsupported_reason="Azure Database Migration Service has no Reservation offering at all - verified live, zero Reservation entries exist. Consumption + Savings Plan for Databases only.",
    )


# ── Microsoft Fabric ─────────────────────────────────────────────────────────
# Real ARM schema (Microsoft.Fabric/capacities, verified 2026-08 via
# Microsoft's own template reference): `sku` is a clean top-level object -
# `sku.name` is the real F-SKU string (e.g. "F64") and `sku.tier` is always
# the literal constant "Fabric" (no useful info). CU count is derived
# directly from the F-number (F64 = 64 CUs) - Microsoft's own `list-
# supported`-style CLI output and pricing docs confirm this 1:1 mapping.
#
# Verified live (australiaeast, pinned api-version): EVERY Fabric workload
# meter (Power BI, Data Warehouse, OneLake operations, Spark, High Scale
# Dataflow, etc - dozens of them) bills the IDENTICAL flat $0.21/CU-hour
# rate under productName "Fabric Capacity" - these aren't separate additive
# charges, they're different attribution/reporting channels for the SAME
# underlying provisioned capacity (confirmed: a Fabric capacity's real cost
# is CU_count x $0.21/hr regardless of which workload actually ran). This
# app prices off the "Power BI" armSkuName specifically (the one universal,
# always-present meter) rather than summing multiple meters - summing would
# multiply the cost by however many workload types happen to be catalogued,
# which would be wrong.
#
# Reservation is refreshingly simple - exactly ONE Reservation SKU exists
# (armSkuName "Fabric_Capacity_CU_Hour", productName "Fabric Capacity
# Reservation"), quoted per-CU (confirmed: dividing the quoted total-term
# price by term-hours gives ~$0.1249/hr for BOTH 1yr and 3yr - a real,
# slightly unusual finding that this service's 3yr commitment doesn't
# discount any deeper than 1yr, not a bug) - `reservation_multiplier` scales
# it by the real CU count, same pattern as every other per-unit-priced
# reservation this app already handles (SQL DB/MI, Cosmos DB).
def _plan_fabric_capacity(sku: str) -> SkuQueryPlan:
    if not sku or not sku.upper().startswith("F") or not sku[1:].isdigit():
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected 'F64'-style Fabric capacity convention.")
    cu_count = int(sku[1:])
    return SkuQueryPlan(
        supported=True, service_name="Microsoft Fabric", match_field="armSkuName",
        consumption_match_value="Power BI", consumption_multiplier=cu_count,
        reservation_match_value="Fabric_Capacity_CU_Hour", reservation_multiplier=cu_count,
        product_contains="Fabric Capacity",
    )


def _plan_databricks(sku: str) -> SkuQueryPlan:
    # Corrected 2026-08-23 - the original "zero Reservation entries exist"
    # claim below was WRONG, caught the same way the Cosmos DB Global-scope
    # gap was: a real, unfiltered query against the Retail Prices API
    # (serviceName eq 'Azure Databricks' and priceType eq 'Reservation', NO
    # region filter) returns 44 real items - fixed-bucket DBCU commitments
    # (12,500 DBCUs up to 6 Million DBCUs, 1yr/3yr terms), armRegionName
    # "Global" - reproducibly missed by a region-filtered query, the exact
    # same false-negative pattern the earlier Cosmos DB investigation hit.
    # Real pricing DOES exist. supported=False stays correct anyway, but for
    # a different, structural reason: a DBCU commitment is a subscription-
    # wide $ pool that auto-applies to ANY Databricks workload/VM-SKU/tier
    # combination (confirmed both by analysis/ri_eligibility.py's existing
    # "pooled discount... not a per-resource reservation match" note, and by
    # the real Azure Pricing Calculator, which shows the reservation options
    # as a flat ~41%/62% (1yr/3yr) discount identically regardless of which
    # example VM/workload is selected) - there is no per-SKU rate for this
    # module's resolve_sku_query() to return at all, unlike every other
    # Reservation type here. See db/seed.py's RI_COVERAGE_NOTES entry for
    # how this is disclosed to the user instead of silently mismatched.
    return SkuQueryPlan(supported=False, reason="Databricks Commit Unit (DBCU) reservations are real and priced (verified live), but apply as a subscription-wide pooled discount across all workloads/VM SKUs, not a per-resource rate this app's SKU-matching model can represent.")


_ADX_SKU_RE = re.compile(r"^(Basic|Standard)_(.+)_(\d+)$")


def _plan_data_explorer(sku: str) -> SkuQueryPlan:
    # Added 2026-08-23 - Azure Data Explorer (Microsoft.Kusto/clusters) was
    # previously entirely untracked (no live inventory fetch existed at
    # all). This app's SKU convention for it is "{tier}_{vmSize}_{capacity}"
    # (e.g. "Standard_Standard_D13_v2_2" for a 2-node Standard-tier D13_v2
    # cluster) - see azure_conn/connector.py's adxSku extraction. tier and
    # capacity come straight from the real top-level sku.tier/sku.capacity
    # ARM fields (confirmed via Microsoft's own REST API reference, 2026-08:
    # AzureSku = {name, tier: 'Basic'|'Standard', capacity: int}).
    #
    # Verified live against the Retail Prices API: Azure Data Explorer's
    # OWN dedicated meter is a single flat "Standard Engine Cluster Markup"
    # ($0.11/hr in most commercial regions, $0.138/hr in US Gov, confirmed
    # identical across 54+ regions checked) - it does NOT vary by VM size at
    # all (skuName is always literally "Standard", never a per-series rate).
    # The underlying VM compute/storage/networking bills separately under
    # Microsoft.Compute, same "service fee tracked, underlying infra
    # excluded" convention already used for Databricks (see
    # RI_COVERAGE_NOTES). Basic/Dev("No SLA") tier has NO markup meter at
    # all (verified live, zero Consumption items) - genuinely free of this
    # fee, not just untracked, hence supported=False rather than a guess.
    # capacity (node count) directly multiplies the flat per-node rate.
    #
    # Reservation pricing also verified real and live (2 items: 1yr/3yr,
    # skuName "Standard") - but unlike every claim in the pre-existing
    # ri_eligibility.py comment, it is NOT region-independent: a region
    # query for 'westus2' returns real data, 'australiaeast' returns zero -
    # genuinely region-scoped like a normal Reservation, just not yet
    # rolled out everywhere. No special-casing needed here - the normal
    # "query live, no data if genuinely absent in this region" pattern
    # already handles partial rollout correctly (same as newer Dedicated
    # Host series). Multiplied by capacity for the same flat-per-node reason
    # as Consumption above.
    match = _ADX_SKU_RE.match((sku or "").strip())
    if not match:
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match the expected '{{tier}}_{{vmSize}}_{{capacity}}' pattern.")
    tier, _vm_size, capacity_str = match.group(1), match.group(2), match.group(3)
    if tier != "Standard":
        return SkuQueryPlan(supported=False, reason="Basic (Dev/No SLA) tier Data Explorer clusters have no Engine Cluster Markup fee at all - verified live, zero pricing items exist for this tier.")
    capacity = max(1, int(capacity_str))
    return SkuQueryPlan(
        supported=True, service_name="Azure Data Explorer", match_field="skuName",
        consumption_match_value="Standard", consumption_multiplier=capacity,
        reservation_match_value="Standard", reservation_multiplier=capacity,
    )


def _plan_unmeasurable_storage(sku: str) -> SkuQueryPlan:
    # Blob Storage / Files reservations are sold in 100 TB+/10 TiB+ blocks
    # far larger than any single resource - already marked "unmeasurable" in
    # analysis/ri_eligibility.py and excluded from per-resource $ display.
    return SkuQueryPlan(supported=False, reason="Sold in blocks (100 TB+/10 TiB+) far larger than a single resource - already excluded from per-resource pricing as 'Volume-Based' in the RI Coverage tab.")


def _plan_deferred(reason: str):
    return lambda sku: SkuQueryPlan(supported=False, reason=reason)


_PLAN_RESOLVERS = {
    "Compute":                       _plan_compute,
    "Azure Dedicated Host":          _plan_dedicated_host,
    "Azure Container Instances":     _plan_container_instances,
    "Azure Container Apps":          _plan_container_apps,
    "Azure Spring Apps Enterprise":  _plan_spring_apps_enterprise,
    "Azure Database Migration Service": _plan_database_migration_service,
    "Microsoft Fabric": _plan_fabric_capacity,
    "Azure DocumentDB": _plan_deferred(
        "Real ARM resource type confirmed (Microsoft.DocumentDB/mongoClusters, "
        "tier at properties.compute.tier e.g. 'M30') and real Retail API pricing "
        "confirmed live (Worker Node / Coordinator Node / Burstable vCore tiers, "
        "real Savings Plan data present) - but deliberately NOT priced this round. "
        "Two real gaps found, not guessed past: (1) the M-tier name (M30/M40/M50/"
        "M60/M80/M200) to Worker-Node-vCore-count mapping is only confirmed for "
        "M30=2/M40=4/M50=8 vCore via Microsoft's own docs - extrapolating the "
        "rest by pattern would be a guess; (2) whether a non-sharded (default) "
        "cluster bills as a single Worker Node only, or ALSO incurs a Coordinator "
        "Node cost (which has its own real Reservation entry - 'Coordinator Node "
        "1 vCore' - but also a genuine $0 'Coordinator Node Free' tier, so the "
        "threshold isn't obvious) isn't confirmed anywhere in public docs. Same "
        "class of deliberate stop as MI Instance Pools - eligible per Microsoft's "
        "own Savings Plan for Databases list, not guessed at for a number that "
        "could be wrong by a real margin."
    ),
    "App Service":                   _plan_app_service,
    "Azure SQL Database":            _plan_sql_database,
    "Azure SQL Elastic Pool":        _plan_sql_database,   # deliberately identical resolver - verified live
                                                            # (2026-08) that Azure prices vCore Elastic Pools
                                                            # against the EXACT SAME armSkuName as Single
                                                            # Database (e.g. "SQLDB_GP_Compute_Gen5"), and the
                                                            # ReservedResourceType enum has no separate
                                                            # "SqlElasticPools" value - only "SqlDatabases".
                                                            # Same TIER_Generation_vCores internal SKU
                                                            # convention (see connector.py's poolSku).
    "Azure SQL Managed Instance":    _plan_sql_managed_instance,
    "Azure Synapse Analytics":       _plan_synapse,
    "Azure Cache for Redis":         _plan_redis,
    "Azure Cache for Redis Enterprise": _plan_redis_enterprise,
    "Azure Cosmos DB":               _plan_cosmos_db,
    "Azure Databricks":              _plan_databricks,
    "Azure Data Explorer":           _plan_data_explorer,
    "Azure Blob Storage":            _plan_unmeasurable_storage,
    "Azure Files":                   _plan_unmeasurable_storage,
    "Azure Database for PostgreSQL": _plan_postgresql,
    "Azure Database for MySQL":      _plan_mysql,
    "Azure Disk Storage": _plan_deferred(
        "Live Resource Graph ingestion doesn't capture standalone Disk resources yet (a separate, pre-existing "
        "gap, not a pricing issue) - mapping logic is ready (armSkuName 'Premium_SSD_Managed_Disks_{tier}', "
        "e.g. P30) but nothing live currently reaches it. Note also: Azure sells Disk reservations in 1-Year "
        "and 10-Year terms, not the 1yr/3yr this app models - 3yr will correctly show as unavailable."
    ),
    "Azure SQL Managed Instance Pool": _plan_deferred(
        "SQL Managed Instance Pools are now captured by live Resource Graph ingestion (azure_conn/connector.py, "
        "2026-08) and DO have real Reservation/Savings Plan discounts per the Azure pricing calculator "
        "(~35%/~55% Reservation, ~23% Savings Plan confirmed live for a General Purpose/Premium-series/80 "
        "vCore/Norway West test) - this is a real, priceable service, not a policy gap like PostgreSQL/MySQL "
        "above. But it does NOT bill via the same per-vCore meter as a standalone Managed Instance: the "
        "calculator's total for that exact config ($32,003.20/mo) doesn't match a standalone instance's "
        "per-vCore rate scaled to the same vCore count (off by roughly 3x), and no confidently-matching Retail "
        "Prices API meter was found after checking several plausible candidates (the flat per-vCore meter, a "
        "sized-skuName variant, the Gen5 hardware pattern - none landed on the calculator's real number). "
        "Deliberately left unpriced rather than guessing a rate that could be wrong by a large margin - needs "
        "either a live tenant's actual billed rate to reverse-engineer against, or deeper Azure pricing "
        "documentation than the public Retail API surfaces. Eligibility (ri_eligibility.py/sp_eligibility.py) "
        "correctly still marks this eligible, matching the priceability-vs-eligibility split used for "
        "Fsv2-series SQL Database."
    ),
}


_REDUNDANCY_AWARE_RESOLVERS = {_plan_sql_database, _plan_sql_managed_instance}


def resolve_sku_query(resource_type: str, sku: str, redundancy: str = "N/A") -> SkuQueryPlan:
    """Entry point: given this app's Resource Type and SKU, returns how to
    query the Retail Prices API for real SP/RI pricing, or an explicit
    'unsupported' plan with a reason. Falls back to the old direct-armSkuName
    strategy for any resource type not explicitly researched yet, so newly
    added resource types degrade to "probably wrong" rather than "crashes" -
    but every type this app currently tracks eligibility rules for for RI/SP
    is listed above. redundancy only affects SQL Database/Elastic Pool/
    Managed Instance resolvers (see _plan_sql_family) - every other resolver
    ignores it, since no other service's armSkuName splits by redundancy."""
    resolver = _PLAN_RESOLVERS.get(resource_type)
    if resolver is None:
        return SkuQueryPlan(
            supported=True, service_name=None, match_field="armSkuName",
            consumption_match_value=sku, reservation_match_value=sku,
        )
    if resolver in _REDUNDANCY_AWARE_RESOLVERS:
        return resolver(sku, redundancy)
    return resolver(sku)
