"""
analysis/engine.py
Multi-Cloud FinOps Optimization System — Core 2-Pass Priority Waterfall Reconciliation Engine.

Architecture (from README):
  [Cloud Inventory] ──> [Normalization]
                              │
                    ┌─────────▼──────────┐
                    │  Priority Waterfall │
                    └─────────┬──────────┘
                              │
              ┌───────────────┴────────────────┐
              ▼                                ▼
  [Pass 1: Rigid RI Allocation]    [Pass 2: Flexible SP Allocation]
  (Perfect Region + SKU + OS)      (Global Portfolio Overage Absorption)
              │                                │
              └───────────────┬────────────────┘
                              ▼
              ┌──────────────────────────────────┐
              │  Recommendation & Safety Buffer  │
              └──────────────────────────────────┘

Math (from README):
  Final PAYG Overage(r,h) = max(0, PAYG Rate(r,h) − RI Alloc(r,h) − SP Alloc(r,h))
  Recommended SP = Mean Hourly PAYG Overage × Safety Buffer Multiplier
  Leakage = Total Commitment Potential − Total Utilized
  Efficiency% = (Utilized / Potential) × 100
"""

import pandas as pd
import numpy as np
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Optional

from analysis.ri_eligibility import check_eligibility, get_coverage_model
from analysis.sp_eligibility import check_sp_eligibility
from pricing.commitment_pricing import MONTH_HOURS

# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_SAFETY_BUFFER   = 0.80   # 80% — conservative buffer to avoid over-purchasing
BUSINESS_HOUR_START     = 8      # 08:00 — Dev VMs turn on
BUSINESS_HOUR_END       = 18     # 18:00 — Dev VMs turn off
DEFAULT_SIMULATE_DAYS   = 30     # Rolling billing window
SNAPSHOT_HOUR           = 12     # Mid-day snapshot captured for the reporting matrix


# ── Result Containers ─────────────────────────────────────────────────────────

@dataclass
class WaterfallResult:
    """Complete output from the hourly waterfall reconciliation engine."""
    billing_snapshot:   pd.DataFrame   # Mid-day 12:00 snapshot rows (Date × Resource)
    efficiency_summary: pd.DataFrame   # RI vs SP potential / utilized / leakage / efficiency
    hourly_profile:     pd.DataFrame   # Per-hour absorption profile (hour 0–23 aggregated)
    orphaned_resources: pd.DataFrame   # Stopped VMs draining active RI capacity


@dataclass
class SPAnalysisResult:
    """Savings Plan coverage analysis output."""
    baseline_spend_hr:        float    # Total eligible PAYG run-rate ($/hr, steady state)
    existing_commitment_hr:   float    # Current SP commitment ($/hr)
    recommended_commitment_hr: float   # Recommended SP commitment at safety buffer
    leakage_hr:               float    # $/hr of SP commitment going unused
    uncovered_hr:             float    # $/hr still running at PAYG rates
    safety_buffer:            float    # Safety buffer multiplier used
    biz_hours_warning:        list     # Resources causing off-hour SP waste


@dataclass
class RIAnalysisResult:
    """Reserved Instance coverage gap/excess analysis output."""
    coverage_table:    pd.DataFrame   # Per-SKU/Region/OS: running count vs reserved qty
    orphaned_ri_drain: pd.DataFrame   # RI $ wasted on stopped VMs


# ── Utility ───────────────────────────────────────────────────────────────────

def _is_running_at_hour(resource_state: str, avg_daily_hrs: int, hour: int) -> bool:
    """
    Determines if a resource is running at a given hour of day.

    Rules:
      - Stopped (deallocated) → never running
      - avg_daily_running_hours == 24 → always running
      - avg_daily_running_hours == 10 → running during business hours (08:00–18:00)
      - avg_daily_running_hours == 0  → never running (same as stopped)
    """
    if resource_state != "Running":
        return False
    if avg_daily_hrs == 24:
        return True
    if avg_daily_hrs == 10 and BUSINESS_HOUR_START <= hour < BUSINESS_HOUR_END:
        return True
    return False


# ── Pass 1: Reserved Instance Allocation ──────────────────────────────────────

def _apply_ri_pass(workload_demand: list[dict], ri_df: pd.DataFrame) -> tuple[list[dict], dict]:
    """
    Pass 1 — Rigid Reserved Instance matching.
    Requires exact match on: SKU, Region, OS.

    Each RI contract covers `reserved_qty` instances at `hourly_usd_commitment` per instance.
    Allocates RI cost up to the PAYG rate for each matching resource (cannot exceed PAYG).

    Returns:
      - Updated workload_demand with RI allocations applied
      - ri_pool_remaining: {commitment_id: remaining_capacity_usd_hr}
    """
    # Build pools: each RI covers reserved_qty units, each at hourly_usd_commitment $/hr
    ri_pools: dict[str, dict] = {}
    for _, ri in ri_df.iterrows():
        ri_pools[ri["commitment_id"]] = {
            "scope_sku":    ri["scope_sku"],
            "scope_region": ri["scope_region"],
            "scope_os":     ri["scope_os"],
            "remaining":    ri["hourly_usd_commitment"] * ri["reserved_qty"],  # total pool
            "per_unit_rate": ri["hourly_usd_commitment"],
        }

    for res in workload_demand:
        for cid, pool in ri_pools.items():
            if (res["SKU"]    == pool["scope_sku"]    and
                res["Region"] == pool["scope_region"] and
                res["OS"]     == pool["scope_os"]     and
                res["Remaining PAYG Cost"] > 0         and
                pool["remaining"] > 0):

                # Allocate the RI committed rate (not the full PAYG rate)
                allocated = min(res["Remaining PAYG Cost"], pool["per_unit_rate"], pool["remaining"])
                pool["remaining"]             -= allocated
                res["Remaining PAYG Cost"]    -= allocated
                res["Covered By RI"]          += allocated

    return workload_demand, ri_pools


# ── Pass 2: Savings Plan Allocation ───────────────────────────────────────────

def _apply_sp_pass(workload_demand: list[dict], sp_df: pd.DataFrame) -> tuple[list[dict], float]:
    """
    Pass 2 — Flexible Savings Plan absorption.
    No SKU or region constraint — absorbs any remaining Compute/DB PAYG overage
    from the global $/hr commitment pool.

    Returns:
      - Updated workload_demand with SP allocations applied
      - sp_remaining: remaining SP pool balance after this hour
    """
    sp_total = float(sp_df["hourly_usd_commitment"].sum()) if not sp_df.empty else 0.0
    sp_remaining = sp_total

    for res in workload_demand:
        if res["Remaining PAYG Cost"] > 0 and sp_remaining > 0:
            allocated = min(res["Remaining PAYG Cost"], sp_remaining)
            sp_remaining               -= allocated
            res["Remaining PAYG Cost"] -= allocated
            res["Covered By SP"]       += allocated

    return workload_demand, sp_remaining


# ── Main Waterfall Engine ─────────────────────────────────────────────────────

def run_waterfall(
    inventory_df:   pd.DataFrame,
    ri_df:          pd.DataFrame,
    sp_df:          pd.DataFrame,
    simulate_days:  int = DEFAULT_SIMULATE_DAYS,
) -> WaterfallResult:
    """
    Executes the full 24-hour × simulate_days priority waterfall reconciliation.

    For each (day, hour):
      1. Determine which resources are running at that hour (business-hours logic)
      2. Pass 1: Apply Rigid RI allocation (SKU + Region + OS match)
      3. Pass 2: Apply Flexible SP absorption (global pool, any compute/DB)
      4. Record mid-day 12:00 snapshot for reporting
      5. Accumulate efficiency metrics

    Returns WaterfallResult with all analysis DataFrames.
    """
    today = date.today()
    simulated_days = [today - timedelta(days=i) for i in range(simulate_days)]

    billing_records     = []
    hourly_profile_data = []   # {hour: 0-23, ri_absorbed, sp_absorbed, payg_overage}

    # Accumulators for efficiency summary
    total_ri_potential  = 0.0
    total_ri_utilized   = 0.0
    total_ri_leakage    = 0.0
    total_sp_potential  = 0.0
    total_sp_utilized   = 0.0

    for current_day in simulated_days:
        for hour in range(24):
            # ── Build hourly workload demand ───────────────────────────────────
            workload_demand = []
            for _, res in inventory_df.iterrows():
                running = _is_running_at_hour(
                    res["Resource State"],
                    res["Avg Daily Running Hours"],
                    hour,
                )
                hourly_cost = res["PAYG Hourly Cost USD"] if running else 0.0
                workload_demand.append({
                    "Resource ID":         res["Resource ID"],
                    "SKU":                 res["SKU"],
                    "Region":              res["Region"],
                    "OS":                  res["OS"],
                    "Resource State":      res["Resource State"],
                    "Avg Running Hrs":     res["Avg Daily Running Hours"],
                    "Remaining PAYG Cost": hourly_cost,
                    "Gross PAYG Cost":     hourly_cost,
                    "Covered By RI":       0.0,
                    "Covered By SP":       0.0,
                    "RI Leakage":          0.0,
                })

            # ── Accumulate potential capacity ───────────────────────────────────
            if not ri_df.empty:
                ri_potential_hr = float(
                    (ri_df["hourly_usd_commitment"] * ri_df["reserved_qty"]).sum()
                )
            else:
                ri_potential_hr = 0.0

            sp_potential_hr = float(sp_df["hourly_usd_commitment"].sum()) if not sp_df.empty else 0.0
            total_ri_potential += ri_potential_hr
            total_sp_potential += sp_potential_hr

            # ── Pass 1: RI Allocation ───────────────────────────────────────────
            workload_demand, ri_pools_remaining = _apply_ri_pass(workload_demand, ri_df)

            # ── Compute RI leakage: capacity remaining in pool after pass 1 ─────
            # Also: RI draining on VMs that are OFF = wasted RI hours
            for res in workload_demand:
                if (res["Resource State"] != "Running" or
                        (res["Avg Running Hrs"] == 10 and
                         not (BUSINESS_HOUR_START <= hour < BUSINESS_HOUR_END))):
                    # RI capacity that could have covered this resource but it's off
                    # Find the matching RI
                    for _, ri in ri_df.iterrows():
                        if (ri["scope_sku"]    == res["SKU"] and
                            ri["scope_region"] == res["Region"] and
                            ri["scope_os"]     == res["OS"]):
                            res["RI Leakage"] += ri["hourly_usd_commitment"]

            # Total RI utilized this hour
            ri_utilized_hr = sum(r["Covered By RI"] for r in workload_demand)
            ri_leakage_hr  = sum(r["RI Leakage"] for r in workload_demand)
            total_ri_utilized += ri_utilized_hr
            total_ri_leakage  += ri_leakage_hr

            # ── Pass 2: SP Allocation ───────────────────────────────────────────
            workload_demand, sp_remaining = _apply_sp_pass(workload_demand, sp_df)

            sp_utilized_hr = sp_potential_hr - sp_remaining
            total_sp_utilized += sp_utilized_hr

            # ── Hourly profile accumulator (aggregate across days) ──────────────
            hourly_profile_data.append({
                "Hour":           hour,
                "RI Absorbed":    ri_utilized_hr,
                "SP Absorbed":    sp_utilized_hr,
                "PAYG Overage":   sum(r["Remaining PAYG Cost"] for r in workload_demand),
                "RI Leakage":     ri_leakage_hr,
            })

            # ── Mid-day snapshot (hour == 12) ───────────────────────────────────
            if hour == SNAPSHOT_HOUR:
                for res in workload_demand:
                    billing_records.append({
                        "Date":                 str(current_day),
                        "Hour":                 f"{hour:02d}:00",
                        "Resource ID":          res["Resource ID"],
                        "SKU":                  res["SKU"],
                        "PAYG Rate/hr (USD)":   round(res["Gross PAYG Cost"], 4),
                        "Covered by RI (USD)":  round(res["Covered By RI"], 4),
                        "Covered by SP (USD)":  round(res["Covered By SP"], 4),
                        "Final PAYG Overage":   round(res["Remaining PAYG Cost"], 4),
                        "RI Leakage (USD)":     round(res["RI Leakage"], 4),
                    })

    # ── Efficiency Summary ─────────────────────────────────────────────────────
    summary_rows = [
        {
            "Commitment Type":       "Reserved Instances",
            "Total Potential USD":   round(total_ri_potential, 2),
            "Utilized USD":          round(total_ri_utilized, 2),
            "Leakage (Wasted USD)":  round(total_ri_potential - total_ri_utilized, 2),
        },
        {
            "Commitment Type":       "Savings Plans",
            "Total Potential USD":   round(total_sp_potential, 2),
            "Utilized USD":          round(total_sp_utilized, 2),
            "Leakage (Wasted USD)":  round(total_sp_potential - total_sp_utilized, 2),
        },
    ]
    efficiency_df = pd.DataFrame(summary_rows)
    efficiency_df["Efficiency %"] = (
        efficiency_df["Utilized USD"] / efficiency_df["Total Potential USD"] * 100
    ).round(1).fillna(0)

    # ── Hourly Profile (averaged across simulated days) ────────────────────────
    hourly_df = pd.DataFrame(hourly_profile_data)
    hourly_profile = (
        hourly_df.groupby("Hour")[["RI Absorbed", "SP Absorbed", "PAYG Overage", "RI Leakage"]]
        .mean()
        .round(4)
        .reset_index()
    )

    # ── Orphaned Resources ─────────────────────────────────────────────────────
    orphaned = inventory_df[inventory_df["Is Orphaned"] == True].copy()
    if not orphaned.empty and not ri_df.empty:
        def _calc_orphan_drain(row):
            match = ri_df[
                (ri_df["scope_sku"]    == row["SKU"]) &
                (ri_df["scope_region"] == row["Region"]) &
                (ri_df["scope_os"]     == row["OS"])
            ]
            return float(match["hourly_usd_commitment"].sum()) if not match.empty else 0.0
        orphaned["RI Drain per Hour (USD)"] = orphaned.apply(_calc_orphan_drain, axis=1)
        orphaned["RI Drain per Day (USD)"]  = orphaned["RI Drain per Hour (USD)"] * 24
        # MONTH_HOURS (730), not a flat "* 30" (720hrs) - an RI bills
        # continuously regardless of the underlying resource's uptime, so
        # this should use the same monthly-hours convention the RI/SP
        # engines annualize with everywhere else, not the 30-day
        # simplification (real inconsistency caught live 2026-08-21).
        orphaned["RI Drain per Month (USD)"] = orphaned["RI Drain per Hour (USD)"] * MONTH_HOURS

    return WaterfallResult(
        billing_snapshot=pd.DataFrame(billing_records),
        efficiency_summary=efficiency_df,
        hourly_profile=hourly_profile,
        orphaned_resources=orphaned,
    )


# ── Savings Plan Analysis ─────────────────────────────────────────────────────

def savings_plan_analysis(
    inventory_df: pd.DataFrame,
    sp_df:        pd.DataFrame,
    safety_buffer: float = DEFAULT_SAFETY_BUFFER,
    eligible_types: Optional[list] = None,
) -> SPAnalysisResult:
    """
    Compares existing SP commitment against the current eligible steady-state run-rate.

    Steady-state run-rate = sum of PAYG costs for resources that run 24x7.
    Business-hours-only resources are excluded from the SP baseline because their
    off-hour gaps create a dangerous over-commitment risk.

    Args:
        inventory_df:   Full normalized inventory DataFrame
        sp_df:          Active Savings Plan commitments DataFrame
        safety_buffer:  Safety buffer multiplier (default 0.80 = 80%)
        eligible_types: Resource Type values that carry a Savings Plan SKU
                         (e.g. COMPUTE_SP_ELIGIBLE_TYPES | DATABASE_SP_ELIGIBLE_TYPES
                         from db/seed.py). Defaults to the literal strings
                         "Compute"/"Database", which never match a real
                         resource_type value on their own - callers should
                         always pass the real eligible-type sets.

    Returns SPAnalysisResult with leakage, overage, and recommendation.
    """
    if eligible_types is None:
        eligible_types = ["Compute", "Database"]

    # Steady-state baseline: only 24x7 running resources
    steady_state = inventory_df[
        (inventory_df["Resource Type"].isin(eligible_types)) &
        (inventory_df["Resource State"] == "Running") &
        (inventory_df["Avg Daily Running Hours"] == 24)
    ]
    # Real per-SKU/tier Savings Plan eligibility (analysis/sp_eligibility.py) -
    # e.g. an App Service Basic plan is the right Resource Type but the wrong
    # tier, so it must not count toward the baseline (keeps this in sync with
    # what Tab 2 displays and what generate_recommendations() acts on).
    if not steady_state.empty:
        sp_elig = steady_state.apply(lambda r: check_sp_eligibility(r["Resource Type"], r["SKU"]), axis=1)
        steady_state = steady_state[sp_elig.apply(lambda t: t[0])]
    baseline_hr = float(steady_state["PAYG Hourly Cost USD"].sum())

    existing_commitment_hr = float(sp_df["hourly_usd_commitment"].sum()) if not sp_df.empty else 0.0
    leakage_hr   = max(0.0, existing_commitment_hr - baseline_hr)
    uncovered_hr = max(0.0, baseline_hr - existing_commitment_hr)
    recommended  = round(baseline_hr * safety_buffer, 4)

    # Business-hours anomaly warning: which VMs cause off-hour SP waste?
    biz_hours_vms = inventory_df[
        (inventory_df["Avg Daily Running Hours"] == 10) &
        (inventory_df["Resource State"] == "Running")
    ]
    biz_hours_warning = []
    for _, vm in biz_hours_vms.iterrows():
        off_hours = 24 - 10  # 14 hours per day where SP could be wasted
        biz_hours_warning.append({
            "Resource ID":    vm["Resource ID"],
            "SKU":            vm["SKU"],
            "Region":         vm["Region"],
            "PAYG/hr (USD)":  vm["PAYG Hourly Cost USD"],
            "Off-Hours/day":  off_hours,
            "SP Waste/day (USD)": round(vm["PAYG Hourly Cost USD"] * off_hours, 4),
        })

    return SPAnalysisResult(
        baseline_spend_hr=round(baseline_hr, 4),
        existing_commitment_hr=existing_commitment_hr,
        recommended_commitment_hr=recommended,
        leakage_hr=round(leakage_hr, 4),
        uncovered_hr=round(uncovered_hr, 4),
        safety_buffer=safety_buffer,
        biz_hours_warning=biz_hours_warning,
    )


# ── Reserved Instance Analysis ────────────────────────────────────────────────

def reservation_analysis(
    inventory_df: pd.DataFrame,
    ri_df:        pd.DataFrame,
) -> RIAnalysisResult:
    """
    Gap / excess analysis for ALL Azure Reserved Instance / Reserved Capacity types.

    Covers per Azure docs:
      Compute VMs, Azure SQL DB, SQL MI, Cosmos DB, Blob Storage, Azure Files,
      Azure Cache for Redis, Synapse Analytics, Databricks, Disk Storage,
      PostgreSQL, MySQL, Data Factory, App Service stamp, etc.

    For each (Resource Type, SKU, Region, OS, Redundancy):
      - running_count: active instances of that profile
      - reserved_qty:  RIs/Reserved Capacity held for that profile
      - gap:           running_count - reserved_qty (buy more)
      - excess:        reserved_qty - running_count (cancel/exchange)

    Redundancy is part of the matching profile (added 2026-08) alongside
    Resource Type/SKU/Region/OS: a reservation scoped to Standard pricing
    does not cover a Zone-Redundant resource of the identical SKU (they're
    genuinely different priced meters - see pricing/commitment_pricing.py's
    _filter_by_redundancy) - without this, two resources sharing a SKU but
    different redundancy would be silently pooled into one demand bucket,
    understating a real coverage gap on whichever one the reservation
    doesn't actually apply to.
    """
    # All running resources (any type)
    running_resources = inventory_df[
        inventory_df["Resource State"] == "Running"
    ].copy()
    if "Redundancy" not in running_resources.columns:
        running_resources["Redundancy"] = "N/A"
    running_resources["Redundancy"] = running_resources["Redundancy"].fillna("N/A")
    demand = (
        running_resources.groupby(["Resource Type", "SKU", "Region", "OS", "Redundancy"])
        .size()
        .reset_index(name="running_count")
    )

    # Split off globally-scoped commitments (scope_region == "Global") before
    # building supply - Azure Cosmos DB Reservations are the first (and so
    # far only) commitment type in this app with no region lock at all,
    # confirmed 2026-08-23 directly against Azure's real Retail Prices API
    # (every real Cosmos DB reservation price item carries
    # "armRegionName": "Global", unlike every other Reservation type here,
    # which are all region-scoped meters). The exact-tuple merge below
    # requires a literal Region match, which "Global" can never satisfy
    # against a real inventory row's actual region - handled as a fully
    # separate, additive pooled comparison further down instead of forcing
    # it through this merge, so every existing region-scoped commitment
    # type's behavior here is completely unchanged.
    if ri_df.empty:
        ri_df_regional = ri_df
        ri_df_global = ri_df
    else:
        is_global_scope = ri_df["scope_region"] == "Global"
        ri_df_regional = ri_df[~is_global_scope]
        ri_df_global = ri_df[is_global_scope]

    # Supply side: all RI / Reserved Capacity commitments
    if ri_df_regional.empty:
        supply = pd.DataFrame(columns=[
            "Resource Type", "SKU", "Region", "OS", "Redundancy", "reserved_qty",
            "commitment_id", "hourly_usd_commitment", "term", "expiry_date"
        ])
    else:
        supply = ri_df_regional.rename(columns={
            "scope_sku":            "SKU",
            "scope_resource_type":  "Resource Type",
            "scope_region":         "Region",
            "scope_os":             "OS",
            "scope_redundancy":     "Redundancy",
        })[[
            "commitment_id", "SKU", "Resource Type", "Region", "OS", "Redundancy",
            "reserved_qty", "hourly_usd_commitment", "term", "expiry_date"
        ]].copy()
        supply["Redundancy"] = supply["Redundancy"].fillna("N/A")
        # scope_resource_type is the authoritative source now (added
        # 2026-08) - REQUIRED because scope_sku alone is genuinely
        # ambiguous (e.g. "GP_Gen5_4" is shared by Azure SQL Database,
        # PostgreSQL Flexible Server, and MySQL Flexible Server; inferring
        # Resource Type by looking the SKU up in inventory silently picked
        # an arbitrary, possibly wrong match - caught live testing the
        # redundancy fix below, where a SQL Database reservation's coverage
        # was misattributed to MySQL). The SKU-based lookup below is kept
        # ONLY as a fallback for commitment rows that genuinely predate this
        # field (should be rare/never for this app's demo-only Commitment
        # data, but avoids silently dropping coverage for old rows).
        missing_rtype = supply["Resource Type"].isna() | (supply["Resource Type"] == "")
        if missing_rtype.any():
            sku_to_rtype = (
                inventory_df[["SKU", "Resource Type"]]
                .drop_duplicates(subset=["SKU"])
                .set_index("SKU")["Resource Type"]
                .to_dict()
            )
            supply.loc[missing_rtype, "Resource Type"] = supply.loc[missing_rtype, "SKU"].map(sku_to_rtype)
        supply["Resource Type"] = supply["Resource Type"].fillna("Unknown")

    # Merge demand + supply on all 5 dimensions
    merged = demand.merge(
        supply, on=["Resource Type", "SKU", "Region", "OS", "Redundancy"], how="outer"
    ).fillna(0)
    merged["running_count"] = merged["running_count"].astype(int)
    merged["reserved_qty"]  = merged["reserved_qty"].astype(int)
    merged["gap"]           = (merged["running_count"] - merged["reserved_qty"]).clip(lower=0)
    merged["excess"]        = (merged["reserved_qty"] - merged["running_count"]).clip(lower=0)

    # Real Azure eligibility per service/SKU/tier (analysis/ri_eligibility.py,
    # sourced from official docs) - a profile that can never carry a reservation
    # isn't a "gap", it's simply out of scope for this program. Zeroing gap here
    # also keeps generate_recommendations() from suggesting a purchase that
    # Azure wouldn't actually let you make.
    if merged.empty:
        merged["is_eligible"] = pd.Series(dtype=bool)
        merged["eligibility_reason"] = pd.Series(dtype=str)
        merged["coverage_model"] = pd.Series(dtype=str)
    else:
        elig = merged.apply(lambda r: check_eligibility(r["Resource Type"], r["SKU"]), axis=1)
        merged["is_eligible"]        = elig.apply(lambda t: t[0])
        merged["eligibility_reason"] = elig.apply(lambda t: t[1])
        merged.loc[~merged["is_eligible"], "gap"] = 0
        # "capacity" services (Cosmos DB, SQL DB/MI, Databricks, Synapse) apply
        # a reservation automatically across ALL matching resources in scope by
        # pooled capacity/throughput, not per-instance - see
        # analysis/ri_eligibility.py for sources. Their resource-count gap is a
        # rough signal only, never zeroed, but flagged for the UI.
        # "unmeasurable" services (Storage, Files) are pooled too, but sold in
        # blocks (100 TB+ / 10 TiB+) that dwarf a single resource, and we don't
        # track actual data volume - a resource-count gap would be actively
        # misleading here, so it's zeroed just like an ineligible row.
        merged["coverage_model"] = merged["Resource Type"].apply(get_coverage_model)
        merged.loc[merged["coverage_model"] == "unmeasurable", "gap"] = 0
        merged.loc[merged["coverage_model"] == "unmeasurable", "excess"] = 0

    # Globally-scoped commitments (split off above) - Cosmos DB Reservations
    # have no per-region purchase concept at all (real quantity is a
    # subscription-wide RU/s pool, confirmed via Azure's own reservation
    # discount docs: "the reservation discount automatically applies to
    # another matching resource" anywhere it's needed), so the comparison
    # here is deliberately pooled across every region rather than run
    # through the exact 5-tuple merge above: total running demand for a
    # (Resource Type, SKU, OS, Redundancy) profile, summed across ALL
    # regions, compared against this commitment's own reserved_qty. Region
    # is not part of the join key (there's no real regional split to key
    # on) and is set to the literal "Global" string afterward so the
    # coverage table reads honestly, not as if it were one specific region.
    # Deliberately mirrors the exact-tuple path above's gap/excess/
    # eligibility logic verbatim (not refactored into a shared helper - the
    # join key genuinely differs, and duplicating ~15 lines here is safer
    # than parametrizing the well-exercised path above for a case that, so
    # far, only ever fires for one resource type). Also deliberately does
    # NOT deduplicate/sum multiple commitments sharing one profile beyond
    # what the exact-tuple path above already does (or doesn't) for the
    # identical scenario - consistent with existing behavior, not a new
    # design decision specific to Global scope.
    if not ri_df_global.empty:
        # Scoped to ONLY the Resource Type(s) that actually have a Global
        # commitment - without this, the outer join below would manufacture
        # a spurious "Global" pseudo-row (reserved_qty=0, gap=running_count)
        # for every OTHER resource type in inventory too (e.g. Compute/VMs,
        # which have no Global reservation concept in real Azure at all),
        # duplicating a "gap" that's often already correctly resolved by
        # the exact-tuple regional path above - caught in testing before
        # this shipped, not a hypothetical.
        global_resource_types = set(ri_df_global["scope_resource_type"].dropna().unique())
        global_demand = (
            running_resources[running_resources["Resource Type"].isin(global_resource_types)]
            .groupby(["Resource Type", "SKU", "OS", "Redundancy"])
            .size()
            .reset_index(name="running_count")
        )
        global_supply = ri_df_global.rename(columns={
            "scope_sku":            "SKU",
            "scope_resource_type":  "Resource Type",
            "scope_os":             "OS",
            "scope_redundancy":     "Redundancy",
        })[[
            "commitment_id", "SKU", "Resource Type", "OS", "Redundancy",
            "reserved_qty", "hourly_usd_commitment", "term", "expiry_date",
        ]].copy()
        global_supply["Redundancy"] = global_supply["Redundancy"].fillna("N/A")

        global_merged = global_demand.merge(
            global_supply, on=["Resource Type", "SKU", "OS", "Redundancy"], how="outer"
        ).fillna(0)
        global_merged["Region"] = "Global"
        global_merged["running_count"] = global_merged["running_count"].astype(int)
        global_merged["reserved_qty"]  = global_merged["reserved_qty"].astype(int)
        global_merged["gap"]           = (global_merged["running_count"] - global_merged["reserved_qty"]).clip(lower=0)
        global_merged["excess"]        = (global_merged["reserved_qty"] - global_merged["running_count"]).clip(lower=0)

        if global_merged.empty:
            global_merged["is_eligible"] = pd.Series(dtype=bool)
            global_merged["eligibility_reason"] = pd.Series(dtype=str)
            global_merged["coverage_model"] = pd.Series(dtype=str)
        else:
            gelig = global_merged.apply(lambda r: check_eligibility(r["Resource Type"], r["SKU"]), axis=1)
            global_merged["is_eligible"]        = gelig.apply(lambda t: t[0])
            global_merged["eligibility_reason"] = gelig.apply(lambda t: t[1])
            global_merged.loc[~global_merged["is_eligible"], "gap"] = 0
            global_merged["coverage_model"] = global_merged["Resource Type"].apply(get_coverage_model)
            global_merged.loc[global_merged["coverage_model"] == "unmeasurable", "gap"] = 0
            global_merged.loc[global_merged["coverage_model"] == "unmeasurable", "excess"] = 0

        merged = pd.concat([merged, global_merged], ignore_index=True)

    # Orphaned RI drain: Stopped VMs/resources whose profile is covered by an RI
    stopped = inventory_df[inventory_df["Resource State"] == "Stopped (deallocated)"]
    orphan_rows = []
    if not ri_df.empty:
        for _, sres in stopped.iterrows():
            sres_redundancy = sres.get("Redundancy", "N/A") if hasattr(sres, "get") else (sres["Redundancy"] if "Redundancy" in sres.index else "N/A")
            # Resource Type is part of the match (not just SKU/region/OS) for
            # the same reason as the coverage table above: scope_sku alone is
            # ambiguous across services sharing an identical SKU string. Uses
            # scope_resource_type directly when set (see db/schema.py), with
            # a same-SKU fallback only for rows that predate that field.
            rtype_match = (
                (ri_df["scope_resource_type"] == sres["Resource Type"]) |
                (ri_df["scope_resource_type"].isna() | (ri_df["scope_resource_type"] == ""))
            )
            match = ri_df[
                rtype_match &
                (ri_df["scope_sku"]    == sres["SKU"]) &
                (ri_df["scope_region"] == sres["Region"]) &
                (ri_df["scope_os"]     == sres["OS"]) &
                (ri_df["scope_redundancy"].fillna("N/A") == (sres_redundancy or "N/A"))
            ]
            if not match.empty:
                for _, ri in match.iterrows():
                    orphan_rows.append({
                        "Resource ID":            sres["Resource ID"],
                        "Resource Name":          sres["Resource Name"],
                        "SKU":                    sres["SKU"],
                        "Region":                 sres["Region"],
                        "OS":                     sres["OS"],
                        "Matching RI":            ri["commitment_id"],
                        "RI Rate/hr (USD)":       ri["hourly_usd_commitment"],
                        "Daily RI Drain (USD)":   round(ri["hourly_usd_commitment"] * 24, 4),
                        "Monthly RI Drain (USD)": round(ri["hourly_usd_commitment"] * MONTH_HOURS, 2),
                        "Recommendation":         "CANCEL / EXCHANGE this RI or restart the resource",
                    })

    return RIAnalysisResult(
        coverage_table=merged,
        orphaned_ri_drain=pd.DataFrame(orphan_rows),
    )



# ── Recommendation Engine ─────────────────────────────────────────────────────

def generate_recommendations(
    sp_result:    SPAnalysisResult,
    ri_result:    RIAnalysisResult,
    waterfall:    WaterfallResult,
    safety_buffer: float = DEFAULT_SAFETY_BUFFER,
    extra_sp_hr:  float = 0.0,    # What-If: additional SP commitment to model
) -> list[dict]:
    """
    Generates prioritized, actionable FinOps recommendations - one card per
    ISSUE CATEGORY, not one per affected resource. A tenant with 12 orphaned
    VMs gets one "12 orphaned resources" card with a drill-down table
    (`items`), not 12 separate cards - the flood of near-duplicate cards was
    exactly what made this list easy to ignore.

    Recommendation types:
      - ACTION_REQUIRED : HIGH severity — significant financial waste detected
      - PURCHASE        : MEDIUM — buy more SP/RI capacity
      - REVIEW          : MEDIUM — pooled-capacity sizing needs a human look, not an auto count
      - EXCHANGE        : MEDIUM — swap over-reserved RI to under-reserved profile
      - OPTIMAL         : LOW — commitment well-matched to current run-rate

    Each dict carries a `category` (for chart grouping/consolidation) and an
    `items` list (empty when there's nothing to drill into) alongside the
    existing title/detail/financial_impact_hr fields, so callers that only
    knew the old shape still work.
    """
    recommendations = []

    # ── RI Leakage Alert ──────────────────────────────────────────────────────
    ri_row = waterfall.efficiency_summary[
        waterfall.efficiency_summary["Commitment Type"] == "Reserved Instances"
    ]
    ri_leakage = float(ri_row["Leakage (Wasted USD)"].values[0]) if not ri_row.empty else 0.0
    ri_efficiency = float(ri_row["Efficiency %"].values[0]) if not ri_row.empty else 100.0
    ri_potential = float(ri_row["Total Potential USD"].values[0]) if not ri_row.empty else 0.0

    # With zero RI commitment, Utilized/Potential is 0/0 -> efficiency_df's
    # fillna(0) turns that NaN into a misleading 0.0%, which would otherwise
    # always trip the efficiency < 70% check below even though there's
    # nothing to be "underutilizing" - require an actual RI commitment to exist.
    if ri_potential > 0 and (ri_leakage > 50.0 or ri_efficiency < 70.0):
        recommendations.append({
            "type":     "ACTION_REQUIRED",
            "severity": "HIGH",
            "icon":     "🔴",
            "category": "RI Leakage",
            "title":    "Cancel or modify underutilized Reserved Instances",
            "detail": (
                f"${ri_leakage:,.2f}/month of RI commitment is going unutilized "
                f"(efficiency: {ri_efficiency:.1f}%). Intermittent workloads (Dev VMs shut "
                f"down after business hours) leave rigid RI hours idle overnight."
            ),
            "action": "Move intermittent workloads off RI onto a flexible Savings Plan, or schedule an RI exchange for right-sized VMs.",
            "financial_impact_hr": round(ri_leakage / (DEFAULT_SIMULATE_DAYS * 24), 4),
            "items": [],
        })

    # ── Orphaned RI Drain Alert — one card, N resources ──────────────────────
    if not ri_result.orphaned_ri_drain.empty:
        drain_df = ri_result.orphaned_ri_drain
        total_monthly_drain = float(drain_df["Monthly RI Drain (USD)"].sum())
        n = len(drain_df)
        recommendations.append({
            "type":     "ACTION_REQUIRED",
            "severity": "HIGH",
            "icon":     "⚠️",
            "category": "Orphaned Capacity",
            "title":    f"{n} stopped resource{'s' if n != 1 else ''} draining active RI capacity",
            "detail": (
                f"{n} resource{'s are' if n != 1 else ' is'} STOPPED (deallocated) but still "
                f"covered by an active Reserved Instance, wasting ${total_monthly_drain:,.2f}/month "
                f"in unused RI capacity."
            ),
            "action": "Restart each resource to use the RI it's paying for, or cancel/exchange the RI if it's staying off.",
            "financial_impact_hr": round(total_monthly_drain / 730, 4),
            "items": drain_df.to_dict(orient="records"),
        })

    # ── RI Gap — Need to Purchase More (per-instance vs pooled, each rolled up) ─
    gap_rows = ri_result.coverage_table[ri_result.coverage_table["gap"] > 0]
    instance_gaps = gap_rows[gap_rows.get("coverage_model") != "capacity"]
    pooled_gaps = gap_rows[gap_rows.get("coverage_model") == "capacity"]

    if not instance_gaps.empty:
        total_gap_units = int(instance_gaps["gap"].sum())
        n_profiles = len(instance_gaps)
        recommendations.append({
            "type":     "PURCHASE",
            "severity": "MEDIUM",
            "icon":     "🟡",
            "category": "RI Purchase Gap",
            "title":    f"Purchase {total_gap_units} more Reserved Instance{'s' if total_gap_units != 1 else ''} across {n_profiles} SKU profile{'s' if n_profiles != 1 else ''}",
            "detail": (
                f"{total_gap_units} running instance{'s are' if total_gap_units != 1 else ' is'} "
                f"on-demand with no matching reservation - see the breakdown for exact SKU/region/OS profiles."
            ),
            "action": "Purchase 1-year or 3-year RIs for the listed profiles - typically ~30-40% cheaper than PAYG for compute.",
            "financial_impact_hr": 0.0,
            "items": instance_gaps.to_dict(orient="records"),
        })

    if not pooled_gaps.empty:
        n_profiles = len(pooled_gaps)
        recommendations.append({
            "type":     "REVIEW",
            "severity": "MEDIUM",
            "icon":     "🟡",
            "category": "RI Pooled Capacity Review",
            "title":    f"Review pooled reservation sizing for {n_profiles} service profile{'s' if n_profiles != 1 else ''}",
            "detail": (
                "These services' reservations are pooled capacity/throughput (TB, RU/s, DBCU, "
                "cDWU, or vCore-hours) applied automatically across all matching resources - "
                "not bought per resource, so a resource-count gap isn't a literal purchase instruction."
            ),
            "action": "Check actual usage in Azure Cost Management before resizing any of the listed reservations.",
            "financial_impact_hr": 0.0,
            "items": pooled_gaps.to_dict(orient="records"),
        })

    # ── RI Excess — Cancel or Exchange (rolled up) ───────────────────────────
    excess_rows = ri_result.coverage_table[ri_result.coverage_table["excess"] > 0]
    if not excess_rows.empty:
        total_excess = int(excess_rows["excess"].sum())
        n_profiles = len(excess_rows)
        recommendations.append({
            "type":     "EXCHANGE",
            "severity": "MEDIUM",
            "icon":     "🔵",
            "category": "RI Rebalance",
            "title":    f"Exchange or cancel {total_excess} idle Reserved Instance{'s' if total_excess != 1 else ''}",
            "detail": (
                f"{total_excess} reserved instance{'s are' if total_excess != 1 else ' is'} held "
                f"across {n_profiles} SKU profile{'s' if n_profiles != 1 else ''} with no running "
                f"resource to cover."
            ),
            "action": "Exchange for a SKU/region profile with an active gap, or cancel if the commitment term allows.",
            "financial_impact_hr": 0.0,
            "items": excess_rows.to_dict(orient="records"),
        })

    # ── SP Purchase Recommendation ────────────────────────────────────────────
    avg_hourly_overage = float(
        waterfall.billing_snapshot["Final PAYG Overage"].mean()
    ) if not waterfall.billing_snapshot.empty else 0.0

    effective_avg = avg_hourly_overage - extra_sp_hr
    if effective_avg > 0.05:
        recommended_purchase = round(effective_avg * safety_buffer, 4)
        recommendations.append({
            "type":     "PURCHASE",
            "severity": "MEDIUM",
            "icon":     "🟡",
            "category": "Savings Plan Purchase",
            "title":    f"Purchase ${recommended_purchase:.4f}/hr of additional Savings Plan",
            "detail": (
                f"Average hourly PAYG overage of ${avg_hourly_overage:.4f}/hr detected. "
                f"At the {int(safety_buffer * 100)}% safety buffer (a conservative anchor to "
                f"steady-state baseline, excluding business-hours peak spikes), this protects "
                f"the financial baseline when Dev VMs go offline at night."
            ),
            "action": f"Purchase ${recommended_purchase:.4f}/hr of additional Savings Plan commitment.",
            "financial_impact_hr": recommended_purchase,
            "items": [],
        })
    elif sp_result.leakage_hr > 0.10:
        recommendations.append({
            "type":     "ACTION_REQUIRED",
            "severity": "MEDIUM",
            "icon":     "🔴",
            "category": "Savings Plan Leakage",
            "title":    "Reduce Savings Plan commitment — leakage detected",
            "detail": (
                f"Current SP commitment (${sp_result.existing_commitment_hr:.4f}/hr) exceeds "
                f"the steady-state baseline (${sp_result.baseline_spend_hr:.4f}/hr) by "
                f"${sp_result.leakage_hr:.4f}/hr - you're paying for unused SP commitment."
            ),
            "action": f"Reduce commitment to the recommended ${sp_result.recommended_commitment_hr:.4f}/hr ({int(safety_buffer*100)}% safety buffer applied).",
            "financial_impact_hr": sp_result.leakage_hr,
            "items": [],
        })

    # ── Optimal State ──────────────────────────────────────────────────────────
    if not recommendations:
        recommendations.append({
            "type":     "OPTIMAL",
            "severity": "OK",
            "icon":     "🟢",
            "category": "Optimal",
            "title":    "Commitment portfolio is optimally configured",
            "detail": (
                "No significant leakage, gaps, or orphaned resources detected. Your Savings "
                "Plan and Reserved Instance commitments are well-matched to the current "
                "infrastructure run-rate."
            ),
            "action": "Continue monitoring as the environment changes.",
            "financial_impact_hr": 0.0,
            "items": [],
        })

    return recommendations


# ── What-If Simulation ────────────────────────────────────────────────────────

def what_if_simulation(
    baseline_overage_hr: float,
    extra_sp_range: list[float],
    safety_buffer:  float = DEFAULT_SAFETY_BUFFER,
) -> pd.DataFrame:
    """
    Models the impact of purchasing varying amounts of additional SP commitment.

    Args:
        baseline_overage_hr: Current average PAYG overage per hour
        extra_sp_range:      List of additional $/hr SP values to model
        safety_buffer:       Safety buffer multiplier applied to calculations

    Returns DataFrame with columns:
      - Extra SP ($/hr):         additional commitment modeled
      - Remaining Overage ($/hr): PAYG overage after new SP absorbs it
      - Monthly Savings (USD):   projected monthly savings vs current
      - Monthly New Cost (USD):  cost of the new commitment
      - Net Benefit (USD/month): savings minus new cost
    """
    rows = []
    for extra in extra_sp_range:
        remaining_overage = max(0.0, baseline_overage_hr - extra)
        monthly_savings   = (baseline_overage_hr - remaining_overage) * 730 * safety_buffer
        monthly_new_cost  = extra * 730
        net_benefit       = monthly_savings - monthly_new_cost
        rows.append({
            "Extra SP ($/hr)":          round(extra, 4),
            "Remaining Overage ($/hr)": round(remaining_overage, 4),
            "Monthly Savings (USD)":    round(monthly_savings, 2),
            "Monthly New Cost (USD)":   round(monthly_new_cost, 2),
            "Net Benefit (USD/month)":  round(net_benefit, 2),
        })
    return pd.DataFrame(rows)
