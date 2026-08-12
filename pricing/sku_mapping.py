"""
pricing/sku_mapping.py
Maps this app's internal (Resource Type, SKU) to the correct Azure Retail
Prices API query strategy for Reserved Instance / Savings Plan lookups.

Only Compute (VMs) has armSkuName == the resource's SKU directly. Every other
service was individually researched live against the API while building this
(2026-08) - see the per-service notes below. Where Azure genuinely has no
RI/SP offering the API can answer (Cosmos DB's pooled RU/s reservations,
Databricks' marketplace-billed DBCU commits, classic Standard/Basic Redis),
this module says so explicitly with a reason, rather than returning an
empty/zero result that could be mistaken for "just hasn't synced yet."

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
    reservation_multiplier: int = 1      # multiply the matched Reservation unit price by this (e.g. vCore count)
    consumption_multiplier: int = 1      # multiply the matched Consumption retailPrice (PAYG) by this - NOT
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


# ── Compute (VMs) ────────────────────────────────────────────────────────────
def _plan_compute(sku: str) -> SkuQueryPlan:
    # Verified (this is the original, already-working case): armSkuName in
    # the Retail API is exactly the vmSize Resource Graph reports.
    return SkuQueryPlan(
        supported=True, service_name=None, match_field="armSkuName",
        consumption_match_value=sku, reservation_match_value=sku,
        os_license_is_separable=True,
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
_SQL_RESERVATION_PREFIX = {
    ("SQLDB", "GP"): "SQLDB_GP_Compute",            # verified
    ("SQLDB", "BC"): "SQLDB_BC_Compute",             # verified
    ("SQLDB", "HS"): "SQLDB_HyperScale_Compute",     # verified - note the naming shift from the consumption side
    ("SQLMI", "GP"): "SQLMI_GP_Compute",             # verified
    ("SQLMI", "BC"): "SQLMI_BC_Compute",             # inferred
    ("SQLMI", "HS"): "SQLMI_HyperScale_Compute",     # inferred by analogy to SQLDB's confirmed shift
}
_SQL_CONSUMPTION_PREFIX = {
    ("SQLDB", "GP"): "SQLDB_GP_Compute",             # verified
    ("SQLDB", "BC"): "SQLDB_BC_Compute",             # verified
    ("SQLDB", "HS"): "SQLDB_HS_Compute",             # verified - but see _HYPERSCALE_FLAT_CONSUMPTION below,
                                                       # this prefix alone is NOT enough for Hyperscale.
    ("SQLMI", "GP"): "SQLMI_GP_Compute",             # verified
    ("SQLMI", "BC"): "SQLMI_BC_Compute",             # inferred
    ("SQLMI", "HS"): "SQLMI_HS_Compute",             # inferred
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
# Hyperscale hardware generations (matched case/hyphen/underscore-insensitively
# against the SKU's "generation" token) whose Consumption armSkuName is
# IDENTICAL to the Reservation armSkuName (no "_1" or "_N" suffix at all) -
# verified live 2026-08, see _plan_sql_family's Hyperscale branch.
_HYPERSCALE_UNIFIED_ARMSKUNAME = {"PREMIUM", "PREMIUMMEMORYOPTIMIZED"}
# DC-series verified working ONLY for these (family, tier) combos this
# session - see _plan_sql_dc_series. Do not assume it generalizes.
_DC_SERIES_VERIFIED = {("SQLDB", "BC")}


def _plan_sql_dc_series(tier: str, vcores: int, family: str, service_name: str) -> SkuQueryPlan:
    """DC-series (confidential-computing-capable hardware) - a genuinely
    different pricing structure from Gen5, verified live 2026-08 against a
    real user-supplied config (West US, Business Critical, 40 vCore):

    Consumption matches on skuName (e.g. "40 vCore"), NOT armSkuName - and
    BOTH retailPrice and the nested savingsPlan.unitPrice are already
    pre-scaled per the queried vCore count (verified across 10/20/40 vCore:
    6.332/12.664/25.328 - exactly proportional to vCore count), the OPPOSITE
    of Gen5 where savingsPlan stays flat regardless of tier. No multiplier
    needed here.

    Reservation: the Retail API DOES return one catalog entry (armSkuName
    "SQL_Database_Single_Elastic_Pool_Business_Critical_Compute_DC-Series_
    vCore") but it is DELIBERATELY not used. The real Azure pricing
    calculator explicitly states "1 year reserved option is not available
    for your instance selection" (and same for 3 year) for this exact
    configuration, and that catalog entry's own effectiveStartDate
    (2021-07-07) is over 4 years stale compared to a known-current Gen5 BC
    Reservation entry queried the same way (2025-07-01) - strong evidence
    it's a legacy listing Azure never removed from the API rather than
    something actually purchasable today. Trusting a raw catalog price over
    the calculator's explicit "not available" would be a real inaccuracy,
    not just an unmapped gap - so reservation_match_value is left empty,
    letting the RI fetch naturally and correctly resolve to no data.

    Only verified for SQL Database Business Critical - General Purpose/
    Hyperscale DC-series and SQL Managed Instance are NOT covered here."""
    if (family, tier) not in _DC_SERIES_VERIFIED:
        return SkuQueryPlan(supported=False, reason=f"DC-series pricing is only verified for SQL Database Business Critical this session - '{family}' tier '{tier}' is not covered.")
    tier_display = _SQL_TIER_DISPLAY_NAME.get(tier, tier)
    return SkuQueryPlan(
        supported=True, service_name=service_name, match_field="skuName",
        consumption_match_value=f"{vcores} vCore",
        reservation_match_value="",
        product_contains=f"{tier_display} - Compute DC-Series",
        reservation_unsupported_reason=(
            "Azure's pricing calculator explicitly shows Reservations as unavailable for DC-series Business "
            "Critical ('1/3 year reserved option is not available for your instance selection') - the Retail "
            "API's catalog entry for this is a stale legacy listing (effectiveStartDate 2021-07-07 vs a "
            "current Gen5 BC Reservation's 2025-07-01), not something actually purchasable today."
        ),
    )


def _plan_sql_family(sku: str, family: str, service_name: str) -> SkuQueryPlan:
    """sku expected as 'TIER_Generation_vCores' (e.g. 'GP_Gen5_4') - this
    app's existing seed-data convention, and also what Resource Graph's
    properties.currentSku.name + properties.currentSku.capacity are combined
    into by azure_conn/connector.py's query (see the sqlSku extend clause).
    'Generation' of 'Serverless' (e.g. 'GP_Serverless_4') is a real, distinct
    billing model - routed to _plan_sql_serverless instead of the Provisioned
    path below. 'Generation' of 'DC-Series'/'DC' is likewise routed to
    _plan_sql_dc_series."""
    parts = (sku or "").split("_")
    if len(parts) < 3 or not parts[-1].isdigit():
        return SkuQueryPlan(supported=False, reason=f"SKU '{sku}' doesn't match either the expected TIER_Generation_vCores (Provisioned, e.g. GP_Gen5_4) or TIER_Serverless_vCores pattern - can't size a reservation without a known vCore count. If this is a DTU-purchase-model database (e.g. 'S0', 'P1', Basic/Standard/Premium tiers), that's a separate, currently-unmapped naming scheme - verified live that DTU-tier SQL Database has zero Savings Plan and zero Reservation entries in the Retail API at all, so there would be nothing to fetch regardless.")
    tier, generation, vcores = parts[0].upper(), "_".join(parts[1:-1]), int(parts[-1])

    if generation.upper() == "SERVERLESS":
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

    if tier in _HYPERSCALE_FLAT_CONSUMPTION:
        gen_key = generation.upper().replace("-", "").replace("_", "")
        if gen_key in _HYPERSCALE_UNIFIED_ARMSKUNAME:
            # Premium-series / Premium-series-memory-optimized: a THIRD
            # distinct Hyperscale Consumption pattern, verified live 2026-08
            # (real user-supplied config investigation). Unlike Gen5 (which
            # needs a separate "HS_..._1" Consumption armSkuName), these use
            # the exact SAME flat armSkuName for BOTH Consumption and
            # Reservation - no "_1" or "_N" suffix at all. Verified:
            # SQLDB_HyperScale_Compute_Premium (0.18266 PAYG/vCore,
            # 0.146128 SP-1yr/vCore = 20% off) and
            # SQLDB_HyperScale_Compute_Premium_Memory_Optimized (0.255724
            # PAYG/vCore, 0.2045792 SP-1yr/vCore = 20% off), both flat
            # per-vCore on both sides needing the same consumption_multiplier
            # treatment as Gen5's "_1" pattern. An earlier session note
            # claimed Consumption/Savings Plan "does NOT work" for these
            # hardware types - that was based on an incomplete check (only
            # tried the Gen5-style "_1"-suffixed guess); corrected here now
            # that the real pattern has been found and verified.
            unified_armskuname = f"{res_prefix}_{generation}"
            return SkuQueryPlan(
                supported=True, service_name=service_name, match_field="armSkuName",
                consumption_match_value=unified_armskuname,
                reservation_match_value=unified_armskuname,
                reservation_multiplier=vcores,
                consumption_multiplier=vcores,
            )
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


def _plan_sql_database(sku: str) -> SkuQueryPlan:
    return _plan_sql_family(sku, "SQLDB", "SQL Database")


def _plan_sql_managed_instance(sku: str) -> SkuQueryPlan:
    return _plan_sql_family(sku, "SQLMI", "SQL Managed Instance")


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


# ── Azure Cache for Redis ────────────────────────────────────────────────────
def _plan_redis(sku: str) -> SkuQueryPlan:
    # Verified live: classic Standard/Basic (C-series) Redis has ZERO
    # Reservation entries in the Retail Prices API - Azure genuinely doesn't
    # sell them (confirmed empty result set, not a naming mismatch). Premium
    # (P-series) and Enterprise do. No Savings Plan exists for Redis at any
    # tier (verified - 0 of 10 Premium Consumption items carried a
    # savingsPlan array), consistent with this app's sp_eligibility.py
    # already excluding Redis from Savings Plan entirely.
    s = (sku or "").upper()
    if "PREMIUM" not in s and "ENTERPRISE" not in s:
        return SkuQueryPlan(
            supported=False,
            reason="Azure only sells Reserved Instances for Premium/Enterprise Redis tiers - Standard/Basic (C-series) has no reservation offering at all (verified live: zero API results, not a lookup gap).",
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


# ── Explicitly unsupported - Azure genuinely can't price these this way ─────
def _plan_cosmos_db(sku: str) -> SkuQueryPlan:
    # Cosmos DB Reserved Capacity is a subscription-wide RU/s pool, not tied
    # to any one resource's SKU - there's no per-resource armSkuName to look
    # up at all. Already correctly excluded from per-resource $ savings via
    # get_coverage_model() == "capacity" in analysis/ri_eligibility.py.
    return SkuQueryPlan(supported=False, reason="Cosmos DB Reserved Capacity is a subscription-wide RU/s pool, not priced against any single resource's SKU.")


def _plan_databricks(sku: str) -> SkuQueryPlan:
    # Verified live: zero Reservation-type entries exist for Azure Databricks
    # in the Retail Prices API at all. Databricks Commit Units (DBCU) are
    # sold as a separate Marketplace pre-purchase plan, not exposed here.
    return SkuQueryPlan(supported=False, reason="Databricks Commit Units are sold via Azure Marketplace, not the Retail Prices API - verified live, zero Reservation entries exist for this service.")


def _plan_unmeasurable_storage(sku: str) -> SkuQueryPlan:
    # Blob Storage / Files reservations are sold in 100 TB+/10 TiB+ blocks
    # far larger than any single resource - already marked "unmeasurable" in
    # analysis/ri_eligibility.py and excluded from per-resource $ display.
    return SkuQueryPlan(supported=False, reason="Sold in blocks (100 TB+/10 TiB+) far larger than a single resource - already excluded from per-resource pricing as 'Volume-Based' in the RI Coverage tab.")


def _plan_deferred(reason: str):
    return lambda sku: SkuQueryPlan(supported=False, reason=reason)


_PLAN_RESOLVERS = {
    "Compute":                       _plan_compute,
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
    "Azure Cosmos DB":               _plan_cosmos_db,
    "Azure Databricks":              _plan_databricks,
    "Azure Blob Storage":            _plan_unmeasurable_storage,
    "Azure Files":                   _plan_unmeasurable_storage,
    "Azure Database for PostgreSQL": _plan_deferred(
        "PostgreSQL Flexible Server compute is priced per VM-series-family (e.g. 'Ddsv4 Series'), not the "
        "GP_Gen5-style tier naming SQL Database/MI use - needs live-tenant verification of the real ARM SKU "
        "format before mapping, not yet built."
    ),
    "Azure Database for MySQL": _plan_deferred(
        "Same VM-series-family pricing model as PostgreSQL Flexible Server - not yet built, see that note."
    ),
    "Azure Disk Storage": _plan_deferred(
        "Live Resource Graph ingestion doesn't capture standalone Disk resources yet (a separate, pre-existing "
        "gap, not a pricing issue) - mapping logic is ready (armSkuName 'Premium_SSD_Managed_Disks_{tier}', "
        "e.g. P30) but nothing live currently reaches it. Note also: Azure sells Disk reservations in 1-Year "
        "and 10-Year terms, not the 1yr/3yr this app models - 3yr will correctly show as unavailable."
    ),
}


def resolve_sku_query(resource_type: str, sku: str) -> SkuQueryPlan:
    """Entry point: given this app's Resource Type and SKU, returns how to
    query the Retail Prices API for real SP/RI pricing, or an explicit
    'unsupported' plan with a reason. Falls back to the old direct-armSkuName
    strategy for any resource type not explicitly researched yet, so newly
    added resource types degrade to "probably wrong" rather than "crashes" -
    but every type this app currently tracks eligibility rules for for RI/SP
    is listed above."""
    resolver = _PLAN_RESOLVERS.get(resource_type)
    if resolver is None:
        return SkuQueryPlan(
            supported=True, service_name=None, match_field="armSkuName",
            consumption_match_value=sku, reservation_match_value=sku,
        )
    return resolver(sku)
