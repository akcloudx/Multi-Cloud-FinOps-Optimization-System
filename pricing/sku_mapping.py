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
