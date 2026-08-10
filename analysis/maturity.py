"""
analysis/maturity.py — FinOps Foundation Maturity Model (Crawl/Walk/Run)
self-assessment (https://www.finops.org/framework/maturity-model/).

Scores this app's own live/demo data against the model wherever there's a
real, computed number to check - and returns "Not Yet Measurable" for
capabilities the app doesn't have the underlying data for, rather than
inventing a figure just to fill a card. Two capabilities have official
published numeric thresholds this app can actually check (Commitment
Discounts coverage, feeding Rate Optimization; Cost Allocation). Usage
Optimization and Anomaly Management have no published numeric thresholds, so
they're scored qualitatively against the model's own Crawl/Walk/Run stage
definitions instead. Forecasting has published thresholds but no source data
in this app, so it's also "Not Yet Measurable".
"""

from dataclasses import dataclass
from typing import Optional

# Official sample thresholds from the FinOps Foundation Maturity Model.
COMMITMENT_DISCOUNT_THRESHOLDS = {"Crawl": 60.0, "Walk": 75.0, "Run": 80.0}
COST_ALLOCATION_THRESHOLDS = {"Crawl": 70.0, "Walk": 85.0, "Run": 90.0}
FORECASTING_THRESHOLDS_NOTE = "Crawl: <20% variance · Walk: <10% variance · Run: <5% variance (lower is better)"

_STAGE_ORDER = ["Below Crawl", "Crawl", "Walk", "Run"]


@dataclass
class CapabilityAssessment:
    domain: str
    capability: str
    stage: str          # "Below Crawl" | "Crawl" | "Walk" | "Run" | "Not Yet Measurable"
    headline: str
    detail: str
    evidence: str


def _stage_from_pct(pct: Optional[float], thresholds: dict) -> str:
    if pct is None:
        return "Not Yet Measurable"
    if pct >= thresholds["Run"]:
        return "Run"
    if pct >= thresholds["Walk"]:
        return "Walk"
    if pct >= thresholds["Crawl"]:
        return "Crawl"
    return "Below Crawl"


def assess_rate_optimization(sp_result, ri_result) -> CapabilityAssessment:
    sp_pct = None
    if sp_result.baseline_spend_hr > 0:
        sp_pct = min(sp_result.existing_commitment_hr, sp_result.baseline_spend_hr) / sp_result.baseline_spend_hr * 100

    ri_pct = None
    ct = ri_result.coverage_table
    if ct is not None and not ct.empty and "is_eligible" in ct.columns:
        instance_rows = ct[(ct["is_eligible"]) & (ct.get("coverage_model") == "instance")]
        total_running = instance_rows["running_count"].sum() if not instance_rows.empty else 0
        if total_running > 0:
            covered = instance_rows[["running_count", "reserved_qty"]].min(axis=1).sum()
            ri_pct = float(covered) / float(total_running) * 100

    sp_stage = _stage_from_pct(sp_pct, COMMITMENT_DISCOUNT_THRESHOLDS)
    ri_stage = _stage_from_pct(ri_pct, COMMITMENT_DISCOUNT_THRESHOLDS)
    measured = [s for s in (sp_stage, ri_stage) if s != "Not Yet Measurable"]
    overall = min(measured, key=_STAGE_ORDER.index) if measured else "Not Yet Measurable"

    sp_txt = f"{sp_pct:.0f}%" if sp_pct is not None else "no SP-eligible steady-state workloads"
    ri_txt = f"{ri_pct:.0f}%" if ri_pct is not None else "no per-instance RI-eligible workloads"

    return CapabilityAssessment(
        domain="Optimize Usage & Cost",
        capability="Rate Optimization",
        stage=overall,
        headline=f"Savings Plan coverage {sp_txt} · Reserved Instance coverage {ri_txt}",
        detail=(
            "Scored against the official Commitment Discounts thresholds (Crawl ≥60%, Walk ≥75%, "
            "Run ≥80% of eligible steady-state spend covered). Overall stage takes the lower "
            "(more conservative) of the two measured sub-metrics."
        ),
        evidence="Savings Plan Analysis tab (existing vs. eligible baseline) and RI Coverage tab (per-instance running vs. reserved counts).",
    )


def assess_usage_optimization(inv_raw, ri_result) -> CapabilityAssessment:
    has_detection = (inv_raw is not None and not inv_raw.empty and "Is Orphaned" in inv_raw.columns)
    orphan_count = int(inv_raw["Is Orphaned"].sum()) if has_detection else 0
    stage = "Crawl" if has_detection else "Not Yet Measurable"
    return CapabilityAssessment(
        domain="Optimize Usage & Cost",
        capability="Usage Optimization",
        stage=stage,
        headline=(
            f"{orphan_count} orphaned/idle resource(s) actively detected this session"
            if has_detection else "No idle-resource detection available"
        ),
        detail=(
            "No official numeric threshold is published for this capability, so it's scored "
            "qualitatively against the model's own stage definitions: idle-resource detection and "
            "reporting exist (Crawl), but there's no automated scheduling, right-sizing, or "
            "auto-remediation yet, which Walk/Run would require."
        ),
        evidence="Orphaned resource flagging (Asset Inventory tab) and orphaned RI drain detection (RI Coverage tab).",
    )


def assess_anomaly_management(recs) -> CapabilityAssessment:
    recs = recs or []
    high_severity = [r for r in recs if r.get("severity") == "HIGH"]
    stage = "Crawl" if recs else "Not Yet Measurable"
    return CapabilityAssessment(
        domain="Understand Usage & Cost",
        capability="Anomaly Management",
        stage=stage,
        headline=f"{len(high_severity)} high-severity cost anomaly alert(s) surfaced this session",
        detail=(
            "No official numeric threshold is published for this capability either. Rule-based "
            "detection and in-dashboard reporting exist (Crawl), but there's no automated "
            "notification pipeline (email/Slack/webhook) or statistical/ML-based anomaly detection "
            "yet, which Walk/Run would require."
        ),
        evidence="Recommendation engine - RI leakage, orphaned RI drain, and SP leakage checks (Recommendations tab).",
    )


def assess_cost_allocation() -> CapabilityAssessment:
    return CapabilityAssessment(
        domain="Understand Usage & Cost",
        capability="Cost Allocation",
        stage="Not Yet Measurable",
        headline="No resource tagging or ownership metadata is ingested yet",
        detail=(
            "Official thresholds: Crawl ≥70%, Walk ≥85%, Run >90% of cost allocated to a known "
            "owner/cost-center. This app's inventory schema doesn't capture tags, owners, or cost "
            "centers yet - a roadmap item, not a false 0%."
        ),
        evidence="No source data yet - db/schema.py's CloudInventory table has no tag/owner columns.",
    )


def assess_forecasting() -> CapabilityAssessment:
    return CapabilityAssessment(
        domain="Quantify Business Value",
        capability="Forecasting",
        stage="Not Yet Measurable",
        headline="No budget-vs-actual variance tracking exists yet",
        detail=(
            f"Official thresholds: {FORECASTING_THRESHOLDS_NOTE}. This app analyzes current "
            "commitments against the current run-rate, but doesn't yet produce a forward-looking "
            "spend forecast to compare against actuals - a roadmap item, not a false 0%."
        ),
        evidence="No source data yet - no forecasting model implemented.",
    )


def run_maturity_assessment(sp_result, ri_result, inv_raw, recs) -> list[CapabilityAssessment]:
    """Runs every capability assessment this app can compute. Order matches
    how they're presented in the UI, not framework document order."""
    return [
        assess_rate_optimization(sp_result, ri_result),
        assess_usage_optimization(inv_raw, ri_result),
        assess_anomaly_management(recs),
        assess_cost_allocation(),
        assess_forecasting(),
    ]
