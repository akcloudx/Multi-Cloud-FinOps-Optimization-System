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

import re
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


def _azure_scope_matches(commitment_sub, commitment_rg, resource_sub, resource_rg) -> bool:
    """True if an Azure Reservation/Savings Plan's real scope restriction (if
    any) permits it to cover a given resource. Added 2026-08-23 - Azure
    Reservations/Savings Plans default to "Single subscription" scope unless
    the buyer deliberately picks "Shared" (confirmed via Microsoft Learn's
    "Buy an Azure reservation" doc), and can be narrowed further to "Single
    resource group" - this app previously ignored scope entirely and matched
    ANY tenant resource with the right SKU/region, which overstates coverage
    for any Single-scoped commitment in a multi-subscription tenant (a real,
    not hypothetical, gap for a live tenant - db/seed.py's own demo
    Reservation records are all Single-scoped).

    commitment_sub/commitment_rg of None means unrestricted (Shared scope,
    the ManagementGroup-membership-unresolved fallback - see
    pricing/commitment_mapping.py's _derive_scope - or demo/seed data with
    no scope concept at all) - always matches, this app's original behavior
    for the common case. A non-None commitment_rg additionally requires
    resource_group to match (the tightest restriction); a non-None
    commitment_sub with no rg requires only subscription to match.
    Case-insensitive - Azure subscription GUIDs and resource group names are
    both compared case-insensitively by ARM convention.

    Deliberately Azure-only, kept separate from _aws_scope_matches() below
    (2026-08-23) rather than one combined function with optional params for
    both providers - Azure's scope hierarchy nests under Account (Subscription
    -> Resource Group) while AWS's nests under Region (-> Availability Zone),
    genuinely different restriction axes with different real-world defaults
    (see _aws_scope_matches's own docstring) - conflating them into one
    signature made it harder to see which params apply to which provider.

    Real bug fixed 2026-08-25: a missing scope value read off a DataFrame
    row (`.get(...)`/`.iterrows()`) comes through as a genuine float NaN,
    not Python None - and `bool(float('nan'))` is True, so the old bare
    `if commitment_sub:` treated a NaN (unrestricted/Shared-scope) commitment
    as if it were restricted to the literal string "nan", which matches no
    real resource. Confirmed live: 8 of this app's own 10 demo Azure RIs
    have a NULL scope_subscription_id, so this was silently starving
    run_waterfall's RI/SP allocation passes (_apply_ri_pass/_apply_sp_pass,
    which call this function directly per-row with no prior NaN cleanup) -
    reservation_analysis's own coverage-table math happened to dodge it via
    a separate `.notna()` pre-split into scoped/unscoped layers before ever
    calling this function, which is exactly why the bug wasn't visible
    there. pd.notna() (not bare truthiness) is the correct check either
    way."""
    if pd.notna(commitment_rg) and commitment_rg:
        return (str(resource_sub or "").lower() == str(commitment_sub or "").lower() and
                str(resource_rg or "").lower() == str(commitment_rg or "").lower())
    if pd.notna(commitment_sub) and commitment_sub:
        return str(resource_sub or "").lower() == str(commitment_sub or "").lower()
    return True


def _aws_scope_matches(commitment_az, resource_az) -> bool:
    """True if an AWS EC2 Reserved Instance's real scope restriction (if any)
    permits it to cover a given resource. Added 2026-08-23 for AWS's "Zonal"
    scope (confirmed via boto3's DescribeReservedInstances Scope field:
    "Availability Zone" | "Region") - a Zonal RI only covers instances in
    that specific AZ, not the whole region; this app previously ignored it
    entirely and matched any same-region instance (see
    pricing/aws_commitment_mapping.py).

    commitment_az of None means unrestricted at this dimension (a "Regional"-
    scope RI, the more common case, or any non-EC2 AWS service, which has no
    AZ-scope concept at all) - always matches. Case-insensitive.

    Deliberately does NOT check account/subscription-level scope at all -
    unlike Azure (which defaults Reservations/Savings Plans to Single-
    subscription scope unless Shared is deliberately chosen), AWS shares
    unused RI/Savings Plan discount across an Organization's linked accounts
    BY DEFAULT once consolidated billing is active (confirmed via AWS's own
    Savings Plans user guide) - this app has no organizations/ram API access
    to detect the edge cases where that sharing is disabled for a specific
    account or restricted via Group Sharing, so account-level scope is
    deliberately left unrestricted rather than guessed at (see
    db/schema.py's Commitment.scope_availability_zone comment).

    Real bug fixed 2026-08-25, same root cause as _azure_scope_matches
    above: a missing scope value off a DataFrame row is a genuine float
    NaN, and bool(float('nan')) is True, so the old bare `if commitment_az:`
    treated an unrestricted (Regional-scope) RI as if it were Zonal-locked
    to the literal string "nan" - pd.notna() is the correct check."""
    if pd.notna(commitment_az) and commitment_az:
        return str(resource_az or "").lower() == str(commitment_az or "").lower()
    return True


def compute_orphaned_status(inventory_df: pd.DataFrame, ri_df: pd.DataFrame) -> pd.Series:
    """True for a resource that is stopped AND has at least one active
    Reservation whose SKU/Region/OS + scope would otherwise cover it - that
    Reservation's $/hr is going to waste while this specific resource sits
    stopped. Same SKU/Region/OS + _azure_scope_matches/_aws_scope_matches
    matching already used to quantify RI drain elsewhere in this file
    (run_waterfall, reservation_analysis), just applied here to DETECT the
    condition instead of assuming a row is already flagged.

    Real detection, added 2026-08-25 - previously "Is Orphaned" was a
    static, hand-set column populated only in demo seed data; every live
    connector (azure_conn/connector.py, aws/connector.py) hardcoded it to
    False, so a genuinely orphaned resource in a real connected tenant
    would never surface anywhere in the app (Inventory tab, the Maturity
    Assessment's detection-capability score, or this file's own orphan-
    drain calculations) - confirmed live by tracing every write site.

    Savings Plans deliberately excluded from this check - unlike a
    SKU/Region-locked Reservation, an SP's $/hr pool just flows to whatever
    else is running, so one stopped resource doesn't strand it the same way
    a Reservation gets stranded.
    """
    if inventory_df.empty:
        return pd.Series([], dtype=bool)
    if ri_df.empty:
        return pd.Series(False, index=inventory_df.index)

    def _is_orphaned(row) -> bool:
        if row["Resource State"] == "Running":
            return False
        match = ri_df[
            (ri_df["scope_sku"]    == row["SKU"]) &
            (ri_df["scope_region"] == row["Region"]) &
            (ri_df["scope_os"]     == row["OS"])
        ]
        if match.empty:
            return False
        return bool(match.apply(
            lambda r: _azure_scope_matches(r.get("scope_subscription_id"), r.get("scope_resource_group_id"),
                                            row.get("Subscription"), row.get("Resource Group"))
                      and _aws_scope_matches(r.get("scope_availability_zone"), row.get("Availability Zone")),
            axis=1,
        ).any())

    return inventory_df.apply(_is_orphaned, axis=1)


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
            "scope_subscription_id":   ri.get("scope_subscription_id"),
            "scope_resource_group_id": ri.get("scope_resource_group_id"),
            "scope_availability_zone": ri.get("scope_availability_zone"),
            "remaining":    ri["hourly_usd_commitment"] * ri["reserved_qty"],  # total pool
            "per_unit_rate": ri["hourly_usd_commitment"],
        }

    for res in workload_demand:
        for cid, pool in ri_pools.items():
            if (res["SKU"]    == pool["scope_sku"]    and
                res["Region"] == pool["scope_region"] and
                res["OS"]     == pool["scope_os"]     and
                res["Remaining PAYG Cost"] > 0         and
                pool["remaining"] > 0                  and
                _azure_scope_matches(pool["scope_subscription_id"], pool["scope_resource_group_id"],
                                      res.get("Subscription"), res.get("Resource Group")) and
                _aws_scope_matches(pool["scope_availability_zone"], res.get("Availability Zone"))):

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
    from whichever SP pool(s) this resource's real Azure scope makes it
    eligible for (Single subscription / Single resource group / Shared - see
    _azure_scope_matches). Per-commitment pools, same shape as
    _apply_ri_pass's ri_pools - added 2026-08-23; previously a single global
    scalar pool with no scope awareness at all, which let a Single-scoped SP
    absorb PAYG overage from resources outside its real scope.

    Returns:
      - Updated workload_demand with SP allocations applied
      - sp_remaining: remaining SP pool balance (summed across all pools) after this hour
    """
    sp_pools = [
        {
            "scope_subscription_id":   sp.get("scope_subscription_id"),
            "scope_resource_group_id": sp.get("scope_resource_group_id"),
            "remaining":               sp["hourly_usd_commitment"],
        }
        for _, sp in sp_df.iterrows()
    ] if not sp_df.empty else []

    for res in workload_demand:
        if res["Remaining PAYG Cost"] <= 0:
            continue
        for pool in sp_pools:
            if res["Remaining PAYG Cost"] <= 0 or pool["remaining"] <= 0:
                continue
            if not _azure_scope_matches(pool["scope_subscription_id"], pool["scope_resource_group_id"],
                                         res.get("Subscription"), res.get("Resource Group")):
                continue
            allocated = min(res["Remaining PAYG Cost"], pool["remaining"])
            pool["remaining"]          -= allocated
            res["Remaining PAYG Cost"] -= allocated
            res["Covered By SP"]       += allocated

    sp_remaining = sum(p["remaining"] for p in sp_pools)
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
                    "Subscription":        res.get("Subscription"),
                    "Resource Group":      res.get("Resource Group"),
                    "Availability Zone":   res.get("Availability Zone"),
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
                            ri["scope_os"]     == res["OS"]     and
                            _azure_scope_matches(ri.get("scope_subscription_id"), ri.get("scope_resource_group_id"),
                                                  res.get("Subscription"), res.get("Resource Group")) and
                            _aws_scope_matches(ri.get("scope_availability_zone"), res.get("Availability Zone"))):
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
            if not match.empty and ("scope_subscription_id" in match.columns):
                match = match[match.apply(
                    lambda ri: _azure_scope_matches(ri.get("scope_subscription_id"), ri.get("scope_resource_group_id"),
                                                     row.get("Subscription"), row.get("Resource Group"))
                               and _aws_scope_matches(ri.get("scope_availability_zone"), row.get("Availability Zone")),
                    axis=1,
                )]
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

    # leakage_hr/uncovered_hr can't be a simple global net of the two totals
    # above once ANY commitment carries a real scope restriction (Single
    # subscription / Single resource group - see pricing/commitment_mapping.py)
    # - a scoped SP's surplus can't offset an out-of-scope resource's gap,
    # and an out-of-scope resource's overage can't drain a scoped SP's pool.
    # Resolved via the same greedy per-resource/per-pool allocation
    # _apply_sp_pass uses for the hourly waterfall (added 2026-08-23,
    # previously this function ignored scope entirely). baseline_hr/
    # existing_commitment_hr above stay simple global sums - still honest,
    # useful headline totals - only the netted leakage/uncovered figures
    # need scope-aware allocation.
    if not sp_df.empty:
        sp_pools = [
            {
                "scope_subscription_id":   sp.get("scope_subscription_id"),
                "scope_resource_group_id": sp.get("scope_resource_group_id"),
                "remaining":               float(sp["hourly_usd_commitment"]),
            }
            for _, sp in sp_df.iterrows()
        ]
    else:
        sp_pools = []
    total_uncovered = 0.0
    for _, res in steady_state.iterrows():
        remaining_cost = float(res["PAYG Hourly Cost USD"])
        for pool in sp_pools:
            if remaining_cost <= 0 or pool["remaining"] <= 0:
                continue
            if not _azure_scope_matches(pool["scope_subscription_id"], pool["scope_resource_group_id"],
                                         res.get("Subscription"), res.get("Resource Group")):
                continue
            allocated = min(remaining_cost, pool["remaining"])
            pool["remaining"] -= allocated
            remaining_cost    -= allocated
        total_uncovered += remaining_cost

    uncovered_hr = round(max(0.0, total_uncovered), 4)
    leakage_hr   = round(max(0.0, sum(p["remaining"] for p in sp_pools)), 4)
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

def _resolve_missing_resource_type(supply: pd.DataFrame, inventory_df: pd.DataFrame) -> pd.DataFrame:
    """Fills a supply DataFrame's "Resource Type" from a SKU->Resource Type
    inventory lookup wherever it's missing/blank - factored out of the main
    merge's original inline version (2026-08) so the Single subscription/
    resource group/Availability Zone scoped layers (added 2026-08-23) share
    the identical fallback instead of each silently producing an
    unmatched-NaN-Resource-Type row that fails their own merge (a real bug
    caught live: an AWS demo Commitment row with no explicit
    scope_resource_type set produced two separate demand-only/supply-only
    rows here instead of one merged one, until this fallback was applied to
    the AZ-scoped layer too). scope_resource_type is still the authoritative
    source when present - REQUIRED because scope_sku alone is genuinely
    ambiguous across services/SKU collisions (e.g. "GP_Gen5_4" shared by
    Azure SQL Database/PostgreSQL/MySQL Flexible Server) - this SKU-based
    lookup is only a fallback for commitment rows that don't set it."""
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
    return supply


# ── AWS EC2/RDS RI instance-size-flexibility ────────────────────────────────
# Real AWS Regional EC2 RIs and most RDS RIs auto-apply their discount across
# ANY size within the same instance family/class-type, proportional to a
# normalization-factor table AWS publishes - not just the exact SKU
# purchased (e.g. one m5.xlarge RI fully covers two running m5.large
# instances). Verified directly against AWS's own docs (2026-08-29), not
# guessed:
#   EC2: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/apply_ri.html
#   RDS: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithReservedDBInstances.html
# Applied as a post-processing reconciliation pass over the main tenant-wide
# coverage layer's already-merged gap/excess numbers (see
# _apply_aws_size_flexibility below) - NOT a rewrite of the demand/supply
# merge itself, which stays exact-match for every other case (Azure,
# non-eligible AWS services, Zonal RIs - already isolated into their own
# merge layer before this ever runs - ineligible OS/engine rows).

_EC2_NORM_FACTOR = {
    "nano": 0.25, "micro": 0.5, "small": 1, "medium": 2, "large": 4,
    "xlarge": 8, "2xlarge": 16, "3xlarge": 24, "4xlarge": 32, "6xlarge": 48,
    "8xlarge": 64, "9xlarge": 72, "10xlarge": 80, "12xlarge": 96,
    "16xlarge": 128, "18xlarge": 144, "24xlarge": 192, "32xlarge": 256,
    "48xlarge": 384, "56xlarge": 448, "96xlarge": 768, "112xlarge": 896,
}

# Regional EC2 RI instance size flexibility is explicitly NOT supported for
# these families, regardless of OS/tenancy - confirmed via AWS's own
# "Limitations" list (apply_ri.html), not assumed.
_EC2_FLEX_EXCLUDED_FAMILIES = {
    "g4ad", "g4dn", "g5", "g5g", "g6", "g6e", "g6f", "gr6", "gr6f",
    "hpc7a", "p5", "inf1", "inf2", "u7i-6tb", "u7i-8tb",
}

_EC2_SKU_RE = re.compile(r"^([a-z0-9]+)\.([a-z0-9]+)$")


def _parse_ec2_sku(sku: str):
    """Returns (family, size) e.g. ("m5", "large") for a real EC2 SKU, or
    None if it doesn't match the expected shape/known size ladder. New,
    string-based parser - deliberately NOT a reuse of
    analysis/rightsizing.py's private _parse_ec2_size, which returns a
    ladder INDEX rather than the real size string and was built for
    rightsizing suggestions, not a normalization-factor lookup."""
    m = _EC2_SKU_RE.match((sku or "").strip().lower())
    if not m:
        return None
    family, size = m.groups()
    if size not in _EC2_NORM_FACTOR:
        return None
    return family, size


_RDS_SKU_RE = re.compile(r"^db\.([a-z0-9]+)\.([a-z0-9]+)$")

# Single-AZ / Multi-AZ-instance normalized units per hour, by instance size -
# confirmed via AWS's own RDS RI docs. A row's "Redundancy" value
# ("Zone Redundant" -> Multi-AZ instance, anything else -> Single-AZ)
# selects which table applies - see _rds_norm_factor below. Multi-AZ DB
# CLUSTER (3-instance, a separate real AWS deployment option with its own
# even-higher multiplier) isn't a concept this app's inventory currently
# distinguishes from 2-instance Multi-AZ Redundancy, so only these two
# columns are modeled.
_RDS_NORM_FACTOR_SINGLE_AZ = {
    "micro": 0.5, "small": 1, "medium": 2, "large": 4, "xlarge": 8,
    "2xlarge": 16, "4xlarge": 32, "6xlarge": 48, "8xlarge": 64,
    "10xlarge": 80, "12xlarge": 96, "16xlarge": 128, "24xlarge": 192,
    "32xlarge": 256,
}
_RDS_NORM_FACTOR_MULTI_AZ = {k: v * 2 for k, v in _RDS_NORM_FACTOR_SINGLE_AZ.items()}

# RDS size flexibility is only available for these engines (this app's own
# resource_type labels - both the live-fetch taxonomy, aws/connector.py's
# _RDS_ENGINE_LABELS, and the older demo-seed labels, db/aws_seed.py) -
# confirmed via AWS's own docs. SQL Server is excluded (no BYOL option to
# even consider - see aws/connector.py's _RDS_ENGINE_LABELS). Oracle is
# now split by license model (2026-08-29, aws/connector.py::map_rds_engine)
# - "(BYOL)" is included below, "(License Included)" stays excluded, same
# treatment as SQL Server; previously this app's own _RDS_ENGINE_LABELS
# mapped every Oracle engine variant (BYOL and License Included alike) to
# one flat resource_type with no way to tell them apart, so the whole type
# was excluded conservatively - that's the gap this split closes.
_RDS_FLEX_ELIGIBLE_TYPES = {
    "Amazon RDS for MariaDB", "AWS RDS MariaDB",
    "Amazon RDS for MySQL", "AWS RDS MySQL",
    "Amazon RDS for PostgreSQL", "AWS RDS PostgreSQL",
    "Amazon RDS for Oracle (BYOL)",
    "Amazon Aurora", "Amazon Aurora (MySQL)", "Amazon Aurora (PostgreSQL)",
}


def _parse_rds_sku(sku: str):
    """Returns (class_type, size) e.g. ("r6i", "large") for a real RDS
    DBInstanceClass string. AWS's own docs are explicit that flexibility
    only applies within the same "instance class type" - db.r6i.large
    flexes to db.r6i.xlarge, but NOT to db.r6id.large or db.r7g.large
    despite the superficial similarity, so this deliberately captures the
    full class-type token (e.g. "r6i", "r6id", "r7g"), not just a leading
    letter/family guess."""
    m = _RDS_SKU_RE.match((sku or "").strip().lower())
    if not m:
        return None
    return m.groups()


def _rds_norm_factor(size: str, redundancy: str):
    table = _RDS_NORM_FACTOR_MULTI_AZ if redundancy == "Zone Redundant" else _RDS_NORM_FACTOR_SINGLE_AZ
    return table.get(size)


def _allocate_size_flexible_group(rows: list) -> float:
    """Mutates `rows` (list of dicts with 'running_count'/'reserved_qty'/
    'norm_units') in place, setting 'covered_count' on each - greedily
    allocates the group's total RI normalized-units smallest-to-largest,
    AWS's own documented allocation order ("applied from the smallest to
    the largest instance size within the family"). Returns the group's
    leftover (unconsumed) normalized-units after every row's demand is
    satisfied or supply runs out.

    Also sets 'partial_fraction' (2026-08-29) on the one row, if any,
    where a genuine PARTIAL credit applies - AWS's own worked example
    (one t2.large running against a t2.medium RI) is a real 50%-off
    discount, not a binary covered/not, but this app's gap/excess stay
    integer instance counts (see _apply_aws_size_flexibility's own
    docstring for why a fractional gap/excess display is out of scope).
    A partial credit can only ever apply to the CURRENT row being
    processed when there's leftover supply too small to cover one more
    whole unit of it: sorted ascending means that leftover is smaller
    than every later (larger) row's norm_units too, so it can never
    fully cover another whole unit of a bigger row either - it's
    genuinely "spent" here, not carryable forward, hence remaining_units
    is zeroed once recorded."""
    remaining_units = sum(r["reserved_qty"] * r["norm_units"] for r in rows)
    for r in sorted(rows, key=lambda x: x["norm_units"]):
        full_units_coverable = int(remaining_units // r["norm_units"]) if r["norm_units"] else 0
        covered = min(r["running_count"], full_units_coverable)
        r["covered_count"] = covered
        remaining_units -= covered * r["norm_units"]
        if covered < r["running_count"] and remaining_units > 0:
            r["partial_fraction"] = remaining_units / r["norm_units"]
            remaining_units = 0
    return remaining_units


def _apply_aws_size_flexibility(merged: pd.DataFrame) -> pd.DataFrame:
    """Reconciles gap/excess for Regional EC2 (Linux, non-excluded family)
    and flexibility-eligible RDS rows to reflect AWS's real instance-size
    flexibility, instead of the exact-SKU-match numbers
    _finalize_coverage_layer already computed - see this module's own
    top-of-file comment block above for the full research trail. Every
    other row (Azure, non-eligible AWS services/engines/OS, rows whose SKU
    doesn't parse) passes through completely unchanged - this never makes a
    gap/excess number worse, only resolves false cross-SKU signals for the
    eligible subset.

    Known, disclosed simplification: gap/excess stay integer instance
    counts (this app's existing coverage-table model), so a genuinely
    PARTIAL coverage case (AWS's own worked example: one t2.large running
    against a t2.medium RI is really 50% covered, a real fractional
    billing discount) still reports as gap=1 here - same direction of
    imprecision the exact-match code already had, just now correctly
    resolved to 0 for every FULL-coverage cross-SKU case instead of every
    cross-SKU case unconditionally. The new "partial_ri_credit_fraction"
    column (2026-08-29) surfaces exactly that gap=1-but-partially-covered
    case as a readable fraction (0.0-1.0) for app.py's Status column to
    show - a deliberately display-only fix (gap/excess numbers themselves
    are unchanged), not an attempt at fully fractional gap/excess.

    Only called on the main tenant-wide layer, before it's concatenated
    with any other scope layer - Zonal EC2 RIs are already excluded from
    this layer's supply entirely (see ri_df_unscoped in
    reservation_analysis()), and every Azure-scoped layer never reaches
    this function at all.
    """
    if merged.empty:
        return merged

    # Set uniformly for EVERY row (not just size-flexibility-touched ones) -
    # 2026-08-29 - so downstream code (app.py's Status column) always finds
    # a real 0.0 rather than a missing column/NaN on Azure rows, non-
    # eligible AWS rows, or rows never grouped below.
    merged["partial_ri_credit_fraction"] = 0.0

    is_ec2 = merged["Resource Type"] == "Amazon EC2"
    is_rds = merged["Resource Type"].isin(_RDS_FLEX_ELIGIBLE_TYPES)
    candidate_mask = is_ec2 | is_rds
    if not candidate_mask.any():
        return merged

    groups: dict = {}
    norm_units: dict = {}
    for idx in merged.index[candidate_mask]:
        row = merged.loc[idx]
        if row["Resource Type"] == "Amazon EC2":
            if row["OS"] != "Linux":
                continue
            parsed = _parse_ec2_sku(row["SKU"])
            if parsed is None:
                continue
            family, size = parsed
            if family in _EC2_FLEX_EXCLUDED_FAMILIES:
                continue
            key = ("EC2", row["Region"], family)
            units = _EC2_NORM_FACTOR[size]
        else:
            parsed = _parse_rds_sku(row["SKU"])
            if parsed is None:
                continue
            class_type, size = parsed
            units = _rds_norm_factor(size, row["Redundancy"])
            if units is None:
                continue
            key = ("RDS", row["Region"], row["Resource Type"], class_type)
        groups.setdefault(key, []).append(idx)
        norm_units[idx] = units

    for idx_group in groups.values():
        if len(idx_group) < 2:
            continue  # only one SKU present in this family/class-type - nothing to reconcile
        rows = [
            {"idx": i, "running_count": int(merged.at[i, "running_count"]),
             "reserved_qty": int(merged.at[i, "reserved_qty"]), "norm_units": norm_units[i]}
            for i in idx_group
        ]
        leftover = _allocate_size_flexible_group(rows)
        largest = max(rows, key=lambda r: r["norm_units"])
        for r in rows:
            merged.at[r["idx"], "gap"] = max(0, r["running_count"] - r["covered_count"])
            merged.at[r["idx"], "excess"] = 0
            merged.at[r["idx"], "partial_ri_credit_fraction"] = r.get("partial_fraction", 0.0)
        merged.at[largest["idx"], "excess"] = int(leftover // largest["norm_units"]) if largest["norm_units"] else 0

    return merged


def _apply_azure_vm_size_flexibility(merged: pd.DataFrame, flex_groups_df: pd.DataFrame) -> pd.DataFrame:
    """Reconciles gap/excess for Azure VM ("Compute") rows to reflect real
    Azure Reserved VM Instance instance-size-flexibility, using the live
    group/ratio cache fetched by azure_conn/connector.py::
    fetch_vm_flexibility_groups and stored in AzureVmFlexibilityGroup (see
    pricing/azure_vm_flexibility.py). Reuses the same provider-agnostic
    _allocate_size_flexible_group() greedy allocator as
    _apply_aws_size_flexibility above - genuinely different architecture
    from AWS in two ways, both confirmed directly against Microsoft's own
    docs (2026-08-29), not assumed similar to AWS:

    1. Opt-in, not automatic. A reservation's own 'instance_flexibility'
       field ("On"/"Off", real API value already captured on
       ReservationPurchase and threaded through to this row via
       Commitment.instance_flexibility, see db/schema.py) gates
       participation - "Off" (Capacity Priority, locked to one exact
       size+AZ) is architecturally closer to AWS's Zonal RIs than to a
       flexible Regional RI. A row whose ONLY matched reservation is "Off"
       (or whose flexibility state is simply unknown - can't safely
       discard an already-resolved exact match without knowing whether
       it's actually flexible, same "can't determine, don't guess"
       discipline as an unparseable AWS SKU) is excluded from grouping
       entirely and keeps whatever gap/excess the exact-match merge above
       already computed. A row with reserved_qty==0 (pure unmet demand, no
       reservation attached at all) always joins as candidate demand -
       it has no existing exact-match coverage to lose.
    2. No hardcodable ratio table - flexibility groups and ratios are
       looked up per (region, SKU) from the live-fetched cache, not a
       generic size-name formula the way AWS's normalization factors are
       (Microsoft's own docs: ratios don't uniformly start at 1 or double
       per step, e.g. "BS Series" starts at 0.25, "Ddsv5 Series" starts at
       2). A SKU absent from the cache (never fetched, or genuinely has no
       flexibility group) passes through completely unchanged.

    Reuses the exact same 'partial_ri_credit_fraction' display-only column
    _apply_aws_size_flexibility already populates (already provider-
    agnostic, no new UI plumbing needed - see app.py's _status() in the RI
    Coverage tab).

    flex_groups_df is empty for AWS tenants (no VM flexibility cache ever
    populated there) and for any Azure tenant/demo scope that hasn't been
    synced/seeded yet - both cases are safe no-ops, identical in shape to
    every other "SKU not in cache" fallback in this module.
    """
    if merged.empty or flex_groups_df is None or flex_groups_df.empty:
        return merged

    # is_eligible (set by _finalize_coverage_layer, already run before this
    # point) already zeroed gap for any SKU/family real Azure sells no RI
    # for at all (analysis/ri_eligibility.py, live-verified against the
    # Retail Prices API - e.g. the classic "DS"-series has zero Reservation
    # entries in the real catalog, confirmed 2026-08-29 while testing this
    # exact function). Reconciliation must respect that: an ineligible row
    # has no real RI product to be flexible about, so it's excluded from
    # grouping entirely rather than having this function overwrite
    # _finalize_coverage_layer's deliberate zero with a recomputed nonzero
    # gap - same bug shape "can't determine, don't guess" already guards
    # against elsewhere in this module, just for eligibility instead of a
    # missing cache entry.
    is_compute = (merged["Resource Type"] == "Compute") & merged.get("is_eligible", True)
    if not is_compute.any():
        return merged

    if "instance_flexibility" not in merged.columns:
        flex_value = pd.Series("", index=merged.index)
    else:
        flex_value = merged["instance_flexibility"].fillna("")
    has_own_reservation = merged["reserved_qty"] > 0
    excluded_locked = has_own_reservation & (flex_value != "On")
    candidate_mask = is_compute & ~excluded_locked
    if not candidate_mask.any():
        return merged

    group_lookup = {
        (row.region, row.sku): (row.flexibility_group, row.ratio)
        for row in flex_groups_df.itertuples()
    }

    groups: dict = {}
    norm_units: dict = {}
    for idx in merged.index[candidate_mask]:
        row = merged.loc[idx]
        found = group_lookup.get((row["Region"], row["SKU"]))
        if found is None:
            continue
        group_name, ratio = found
        key = ("VM", row["Region"], group_name)
        groups.setdefault(key, []).append(idx)
        norm_units[idx] = ratio

    for idx_group in groups.values():
        if len(idx_group) < 2:
            continue  # only one SKU present in this flexibility group - nothing to reconcile
        rows = [
            {"idx": i, "running_count": int(merged.at[i, "running_count"]),
             "reserved_qty": int(merged.at[i, "reserved_qty"]) if flex_value.at[i] == "On" else 0,
             "norm_units": norm_units[i]}
            for i in idx_group
        ]
        leftover = _allocate_size_flexible_group(rows)
        largest = max(rows, key=lambda r: r["norm_units"])
        for r in rows:
            merged.at[r["idx"], "gap"] = max(0, r["running_count"] - r["covered_count"])
            merged.at[r["idx"], "excess"] = 0
            merged.at[r["idx"], "partial_ri_credit_fraction"] = r.get("partial_fraction", 0.0)
        merged.at[largest["idx"], "excess"] = int(leftover // largest["norm_units"]) if largest["norm_units"] else 0

    return merged


def _finalize_coverage_layer(merged: pd.DataFrame) -> pd.DataFrame:
    """Shared eligibility/coverage-model/gap-zeroing finalization applied to
    a demand-supply merged DataFrame, regardless of which layer produced it
    (main tenant-wide, Global-scope, or the Single subscription/resource
    group scoped layers added 2026-08-23) - factored out so each layer's own
    join logic (which genuinely differs - different key columns per layer)
    doesn't have to duplicate this identical ~20-line finalization block."""
    if merged.empty:
        merged["is_eligible"] = pd.Series(dtype=bool)
        merged["eligibility_reason"] = pd.Series(dtype=str)
        merged["coverage_model"] = pd.Series(dtype=str)
        return merged
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
    return merged


def reservation_analysis(
    inventory_df: pd.DataFrame,
    ri_df:        pd.DataFrame,
    flex_groups_df: pd.DataFrame = None,
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

    flex_groups_df (2026-08-29): the AzureVmFlexibilityGroup cache, read
    via pricing/azure_vm_flexibility.py::get_vm_flexibility_groups(engine)
    - engine.py itself never touches SQL DB directly (pure computation over
    DataFrames the caller loads), same discipline as inventory_df/ri_df.
    None/empty is a safe no-op (AWS tenants, or an Azure tenant/demo scope
    that hasn't synced/seeded this cache yet) - see
    _apply_azure_vm_size_flexibility's own docstring.
    """
    # All running resources (any type)
    running_resources = inventory_df[
        inventory_df["Resource State"] == "Running"
    ].copy()
    if "Redundancy" not in running_resources.columns:
        running_resources["Redundancy"] = "N/A"
    running_resources["Redundancy"] = running_resources["Redundancy"].fillna("N/A")
    for _col in ("Subscription", "Resource Group", "Availability Zone"):
        if _col not in running_resources.columns:
            running_resources[_col] = ""
        running_resources[_col] = running_resources[_col].fillna("")

    # Split off globally-scoped commitments (scope_region == "Global") before
    # building demand/supply - Azure Cosmos DB Reservations are the first
    # (and so far only) commitment type in this app with no region lock at
    # all, confirmed 2026-08-23 directly against Azure's real Retail Prices
    # API (every real Cosmos DB reservation price item carries
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

    # Regional commitments split further by real Azure scope restriction
    # (Single subscription / Single resource group vs Shared - see
    # pricing/commitment_mapping.py's _derive_scope) - added 2026-08-23. A
    # scoped commitment's reserved_qty must NOT be pooled into the main
    # tenant-wide merge below, or it would silently cover demand from
    # subscriptions/resource groups it doesn't actually apply to (the exact
    # gap this scope work closes) - it gets its own dedicated layer further
    # down instead, same "split off, handle separately, concat back"
    # pattern already used for Global scope above. Unscoped (scope_subscription_id
    # null - Shared, or the ManagementGroup-membership-unresolved fallback)
    # commitments keep pooling against ALL matching demand tenant-wide
    # exactly as before.
    # Also split off AWS "Zonal" EC2 Reserved Instances (scope_availability_zone
    # set - see pricing/aws_commitment_mapping.py) - the SAME kind of gap as
    # Azure's Single-subscription scope, just nested under Region instead of
    # Account (a Zonal RI only covers instances in that specific AZ, not the
    # whole region). Gets its own dedicated layer below, keyed by AZ rather
    # than Subscription/Resource Group.
    if ri_df_regional.empty:
        ri_df_unscoped = ri_df_regional
        ri_df_scoped = ri_df_regional
        ri_df_az_scoped = ri_df_regional
    else:
        # Resolve scope_resource_type BEFORE splitting/excluding - the
        # exclusion mask below and every scoped layer's own merge need a
        # real Resource Type to match on, and a commitment row that doesn't
        # set it explicitly (same "SKU-based fallback" case the main merge
        # already handles) would otherwise never match anything here either
        # (a real bug caught live: an AWS demo Zonal RI with no explicit
        # scope_resource_type silently failed to exclude its covered
        # instances from the main pool, double-counting them).
        ri_df_regional = ri_df_regional.copy()
        ri_df_regional["scope_resource_type"] = _resolve_missing_resource_type(
            ri_df_regional.rename(columns={"scope_resource_type": "Resource Type", "scope_sku": "SKU"}), inventory_df
        )["Resource Type"]
        has_scope = ri_df_regional["scope_subscription_id"].notna() & (ri_df_regional["scope_subscription_id"] != "")
        has_az_scope = ri_df_regional["scope_availability_zone"].notna() & (ri_df_regional["scope_availability_zone"] != "")
        ri_df_scoped = ri_df_regional[has_scope]
        ri_df_az_scoped = ri_df_regional[has_az_scope & ~has_scope]
        ri_df_unscoped = ri_df_regional[~has_scope & ~has_az_scope]

    # Resources whose exact profile (Resource Type/SKU/Region/OS/Redundancy
    # AND Subscription[/Resource Group]) is targeted by a scope-restricted
    # commitment are excluded from the MAIN tenant-wide demand pool below and
    # handled exclusively in their own dedicated scoped layer further down -
    # otherwise the same physical resource's demand would be double-counted
    # (once as unmet in the pooled tenant-wide gap, once - correctly - as met
    # in its scoped layer), overstating total gap. A resource NOT targeted by
    # any scoped commitment is unaffected and still pools normally below.
    _scoped_for_exclusion = pd.concat([ri_df_scoped, ri_df_az_scoped]) if not ri_df_az_scoped.empty else ri_df_scoped
    if not _scoped_for_exclusion.empty:
        excluded = running_resources.apply(
            lambda row: any(
                row["Resource Type"] == ri["scope_resource_type"] and
                row["SKU"]           == ri["scope_sku"] and
                row["Region"]        == ri["scope_region"] and
                row["OS"]            == ri["scope_os"] and
                row["Redundancy"]    == (ri["scope_redundancy"] or "N/A") and
                _azure_scope_matches(ri["scope_subscription_id"], ri["scope_resource_group_id"],
                                      row["Subscription"], row["Resource Group"]) and
                _aws_scope_matches(ri["scope_availability_zone"], row["Availability Zone"])
                for _, ri in _scoped_for_exclusion.iterrows()
            ),
            axis=1,
        )
        main_pool_resources = running_resources[~excluded]
    else:
        main_pool_resources = running_resources

    demand = (
        main_pool_resources.groupby(["Resource Type", "SKU", "Region", "OS", "Redundancy"])
        .size()
        .reset_index(name="running_count")
    )

    # Supply side: all RI / Reserved Capacity commitments
    if ri_df_unscoped.empty:
        supply = pd.DataFrame(columns=[
            "Resource Type", "SKU", "Region", "OS", "Redundancy", "reserved_qty",
            "commitment_id", "hourly_usd_commitment", "term", "expiry_date", "instance_flexibility"
        ])
    else:
        supply = ri_df_unscoped.rename(columns={
            "scope_sku":            "SKU",
            "scope_resource_type":  "Resource Type",
            "scope_region":         "Region",
            "scope_os":             "OS",
            "scope_redundancy":     "Redundancy",
        })[[
            "commitment_id", "SKU", "Resource Type", "Region", "OS", "Redundancy",
            "reserved_qty", "hourly_usd_commitment", "term", "expiry_date", "instance_flexibility"
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
        supply = _resolve_missing_resource_type(supply, inventory_df)

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
    merged = _finalize_coverage_layer(merged)

    # AWS EC2/RDS RI instance-size-flexibility reconciliation (2026-08-29) -
    # see _apply_aws_size_flexibility's own docstring and this module's
    # top-of-file comment block above _finalize_coverage_layer for the full
    # research trail. Deliberately placed HERE, only on the main tenant-wide
    # layer - Zonal EC2 RIs (not size-flexible in real AWS) are already
    # isolated out of ri_df_unscoped before this point, and every
    # Azure-scoped layer below never passes through this function at all.
    merged = _apply_aws_size_flexibility(merged)

    # Azure VM RI instance-size-flexibility reconciliation (2026-08-29) -
    # see _apply_azure_vm_size_flexibility's own docstring. Same placement
    # rationale as the AWS pass above: only the main tenant-wide layer,
    # after the exact-match merge/finalize has already run.
    merged = _apply_azure_vm_size_flexibility(merged, flex_groups_df)

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
    # The join key genuinely differs from the exact-tuple path above (no
    # Region dimension), so the merge itself is duplicated rather than
    # parametrized - only the post-merge eligibility/coverage-model
    # finalization is shared, via _finalize_coverage_layer(). Also deliberately does
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
        global_supply = _resolve_missing_resource_type(global_supply, inventory_df)

        global_merged = global_demand.merge(
            global_supply, on=["Resource Type", "SKU", "OS", "Redundancy"], how="outer"
        ).fillna(0)
        global_merged["Region"] = "Global"
        global_merged["running_count"] = global_merged["running_count"].astype(int)
        global_merged["reserved_qty"]  = global_merged["reserved_qty"].astype(int)
        global_merged["gap"]           = (global_merged["running_count"] - global_merged["reserved_qty"]).clip(lower=0)
        global_merged["excess"]        = (global_merged["reserved_qty"] - global_merged["running_count"]).clip(lower=0)
        global_merged = _finalize_coverage_layer(global_merged)

        merged = pd.concat([merged, global_merged], ignore_index=True)

    # Single subscription / Single resource group scoped commitments (split
    # off as ri_df_scoped above) - added 2026-08-23, same "split off, handle
    # separately, concat back" pattern as Global scope above, but keyed on
    # Subscription (and Resource Group, for the tighter restriction) instead
    # of dropping Region. A scoped commitment only covers demand from
    # resources actually inside its scope - pooling it into the main
    # tenant-wide merge above would silently cover out-of-scope resources
    # too (the real, previously-unhandled gap this closes; see
    # analysis/engine.py's _azure_scope_matches and
    # pricing/commitment_mapping.py's _derive_scope for the full context).
    # This table is already documented (see "capacity"-model coverage_model
    # comment above) as a rough per-profile signal, not a strict ledger, for
    # pooled-capacity services - the same discipline applies here: a
    # resource simultaneously in reach of BOTH an unscoped AND a scoped
    # commitment for the identical profile may show up in two separate rows
    # (one per layer) rather than one perfectly netted number, same
    # "disclosed imprecision over a bigger allocation-engine rewrite"
    # trade-off already made for Global scope.
    if not ri_df_scoped.empty:
        no_rg_scope = ri_df_scoped["scope_resource_group_id"].isna() | (ri_df_scoped["scope_resource_group_id"] == "")
        ri_df_sub_scoped = ri_df_scoped[no_rg_scope]
        ri_df_rg_scoped  = ri_df_scoped[~no_rg_scope]

        if not ri_df_sub_scoped.empty:
            # Demand must be restricted to the EXACT profile(s) this layer's
            # own commitments target, not just "any resource in the same
            # subscription" - a real, severe bug caught live 2026-08-23: an
            # early version filtered only by Subscription membership, which
            # pulled in every OTHER resource type/SKU sharing that
            # subscription too (e.g. Cosmos DB, SQL DB, ...) and produced a
            # spurious extra "gap = full demand, reserved=0" row for every
            # one of them via the outer join below, even though they were
            # already correctly accounted for in the main tenant-wide layer
            # above - roughly doubled this table's total gap in demo data.
            # Mirrors the Global-scope layer's existing Resource-Type
            # restriction (see global_resource_types above), generalized to
            # the full profile since scope_sku is more specific.
            sub_profiles = ri_df_sub_scoped[["scope_resource_type", "scope_sku", "scope_region", "scope_os"]].rename(columns={
                "scope_resource_type": "Resource Type", "scope_sku": "SKU", "scope_region": "Region", "scope_os": "OS",
            }).drop_duplicates()
            sub_candidates = running_resources.merge(sub_profiles, on=["Resource Type", "SKU", "Region", "OS"], how="inner")
            sub_demand = (
                sub_candidates[sub_candidates["Subscription"].isin(ri_df_sub_scoped["scope_subscription_id"].unique())]
                .groupby(["Resource Type", "SKU", "Region", "OS", "Redundancy", "Subscription"])
                .size()
                .reset_index(name="running_count")
            )
            sub_supply = ri_df_sub_scoped.rename(columns={
                "scope_sku":              "SKU",
                "scope_resource_type":    "Resource Type",
                "scope_region":           "Region",
                "scope_os":               "OS",
                "scope_redundancy":       "Redundancy",
                "scope_subscription_id":  "Subscription",
            })[[
                "commitment_id", "SKU", "Resource Type", "Region", "OS", "Redundancy", "Subscription",
                "reserved_qty", "hourly_usd_commitment", "term", "expiry_date",
            ]].copy()
            sub_supply["Redundancy"] = sub_supply["Redundancy"].fillna("N/A")
            sub_supply = _resolve_missing_resource_type(sub_supply, inventory_df)

            sub_merged = sub_demand.merge(
                sub_supply, on=["Resource Type", "SKU", "Region", "OS", "Redundancy", "Subscription"], how="outer"
            ).fillna(0)
            sub_merged["running_count"] = sub_merged["running_count"].astype(int)
            sub_merged["reserved_qty"]  = sub_merged["reserved_qty"].astype(int)
            sub_merged["gap"]           = (sub_merged["running_count"] - sub_merged["reserved_qty"]).clip(lower=0)
            sub_merged["excess"]        = (sub_merged["reserved_qty"] - sub_merged["running_count"]).clip(lower=0)
            sub_merged = _finalize_coverage_layer(sub_merged)
            merged = pd.concat([merged, sub_merged], ignore_index=True)

        if not ri_df_rg_scoped.empty:
            # Same profile restriction as sub_demand above - not just
            # "any resource in the same resource group."
            rg_profiles = ri_df_rg_scoped[["scope_resource_type", "scope_sku", "scope_region", "scope_os"]].rename(columns={
                "scope_resource_type": "Resource Type", "scope_sku": "SKU", "scope_region": "Region", "scope_os": "OS",
            }).drop_duplicates()
            rg_candidates = running_resources.merge(rg_profiles, on=["Resource Type", "SKU", "Region", "OS"], how="inner")
            rg_demand = (
                rg_candidates[rg_candidates["Resource Group"].isin(ri_df_rg_scoped["scope_resource_group_id"].unique())]
                .groupby(["Resource Type", "SKU", "Region", "OS", "Redundancy", "Subscription", "Resource Group"])
                .size()
                .reset_index(name="running_count")
            )
            rg_supply = ri_df_rg_scoped.rename(columns={
                "scope_sku":               "SKU",
                "scope_resource_type":     "Resource Type",
                "scope_region":            "Region",
                "scope_os":                "OS",
                "scope_redundancy":        "Redundancy",
                "scope_subscription_id":   "Subscription",
                "scope_resource_group_id": "Resource Group",
            })[[
                "commitment_id", "SKU", "Resource Type", "Region", "OS", "Redundancy", "Subscription", "Resource Group",
                "reserved_qty", "hourly_usd_commitment", "term", "expiry_date",
            ]].copy()
            rg_supply["Redundancy"] = rg_supply["Redundancy"].fillna("N/A")
            rg_supply = _resolve_missing_resource_type(rg_supply, inventory_df)

            rg_merged = rg_demand.merge(
                rg_supply, on=["Resource Type", "SKU", "Region", "OS", "Redundancy", "Subscription", "Resource Group"], how="outer"
            ).fillna(0)
            rg_merged["running_count"] = rg_merged["running_count"].astype(int)
            rg_merged["reserved_qty"]  = rg_merged["reserved_qty"].astype(int)
            rg_merged["gap"]           = (rg_merged["running_count"] - rg_merged["reserved_qty"]).clip(lower=0)
            rg_merged["excess"]        = (rg_merged["reserved_qty"] - rg_merged["running_count"]).clip(lower=0)
            rg_merged = _finalize_coverage_layer(rg_merged)
            merged = pd.concat([merged, rg_merged], ignore_index=True)

    # AWS "Zonal" EC2 Reserved Instances (split off as ri_df_az_scoped above)
    # - added 2026-08-23, same pattern as the Single subscription/resource
    # group layers above, but keyed on Availability Zone (nested under
    # Region, not Account - see pricing/aws_commitment_mapping.py). A Zonal
    # RI only covers instances in that specific AZ; pooling it into the main
    # tenant-wide merge would silently cover same-instance-type demand in a
    # DIFFERENT AZ of the same region too.
    if not ri_df_az_scoped.empty:
        # Same profile restriction as sub_demand/rg_demand above - not just
        # "any resource in the same Availability Zone."
        az_profiles = ri_df_az_scoped[["scope_resource_type", "scope_sku", "scope_region", "scope_os"]].rename(columns={
            "scope_resource_type": "Resource Type", "scope_sku": "SKU", "scope_region": "Region", "scope_os": "OS",
        }).drop_duplicates()
        az_candidates = running_resources.merge(az_profiles, on=["Resource Type", "SKU", "Region", "OS"], how="inner")
        az_demand = (
            az_candidates[az_candidates["Availability Zone"].isin(ri_df_az_scoped["scope_availability_zone"].unique())]
            .groupby(["Resource Type", "SKU", "Region", "OS", "Redundancy", "Availability Zone"])
            .size()
            .reset_index(name="running_count")
        )
        az_supply = ri_df_az_scoped.rename(columns={
            "scope_sku":                "SKU",
            "scope_resource_type":      "Resource Type",
            "scope_region":             "Region",
            "scope_os":                 "OS",
            "scope_redundancy":         "Redundancy",
            "scope_availability_zone":  "Availability Zone",
        })[[
            "commitment_id", "SKU", "Resource Type", "Region", "OS", "Redundancy", "Availability Zone",
            "reserved_qty", "hourly_usd_commitment", "term", "expiry_date",
        ]].copy()
        az_supply["Redundancy"] = az_supply["Redundancy"].fillna("N/A")
        az_supply = _resolve_missing_resource_type(az_supply, inventory_df)

        az_merged = az_demand.merge(
            az_supply, on=["Resource Type", "SKU", "Region", "OS", "Redundancy", "Availability Zone"], how="outer"
        ).fillna(0)
        az_merged["running_count"] = az_merged["running_count"].astype(int)
        az_merged["reserved_qty"]  = az_merged["reserved_qty"].astype(int)
        az_merged["gap"]           = (az_merged["running_count"] - az_merged["reserved_qty"]).clip(lower=0)
        az_merged["excess"]        = (az_merged["reserved_qty"] - az_merged["running_count"]).clip(lower=0)
        az_merged = _finalize_coverage_layer(az_merged)
        merged = pd.concat([merged, az_merged], ignore_index=True)

    # Orphaned RI drain: Stopped VMs/resources whose profile is covered by an RI.
    # != "Running" (not a literal "Stopped (deallocated)" match) - that literal
    # string is Azure-specific VM terminology; AWS's real stopped-resource
    # wording is "Stopped" (see aws/connector.py), and a literal-string check
    # here would have silently never matched any AWS resource.
    stopped = inventory_df[inventory_df["Resource State"] != "Running"]
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
            if not match.empty and "scope_subscription_id" in match.columns:
                match = match[match.apply(
                    lambda ri: _azure_scope_matches(ri.get("scope_subscription_id"), ri.get("scope_resource_group_id"),
                                                     sres.get("Subscription"), sres.get("Resource Group"))
                               and _aws_scope_matches(ri.get("scope_availability_zone"), sres.get("Availability Zone")),
                    axis=1,
                )]
            if not match.empty:
                for _, ri in match.iterrows():
                    orphan_rows.append({
                        "Resource ID":     sres["Resource ID"],
                        "Resource Name":   sres["Resource Name"],
                        "SKU":             sres["SKU"],
                        "Region":          sres["Region"],
                        "OS":              sres["OS"],
                        "Matching RI":     ri["commitment_id"],
                        # No "RI Rate/hr" column (removed 2026-08-29, real
                        # feedback) - an RI isn't billed hour-by-hour, and
                        # it was a redundant restatement of the same
                        # commitment rate "Daily"/"Monthly RI Drain" below
                        # already express (a plain multiple, no new
                        # information). "(USD)" also dropped from these two
                        # column NAMES - raw numeric here, currency symbol
                        # applied only at display time (app.py's fmt()),
                        # same "never bake a currency into a column name"
                        # rule already established elsewhere in this app
                        # (see app.py's own "Monthly Savings" column
                        # comment) - a hardcoded "(USD)" here would lie the
                        # moment a user switches the display currency to
                        # INR, same bug class as the header mislabeling
                        # that rule was written to prevent.
                        "Daily RI Drain":   round(ri["hourly_usd_commitment"] * 24, 4),
                        "Monthly RI Drain": round(ri["hourly_usd_commitment"] * MONTH_HOURS, 2),
                        "Recommendation":   "CANCEL / EXCHANGE this RI or restart the resource",
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
        total_monthly_drain = float(drain_df["Monthly RI Drain"].sum())
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
