"""
analysis/maturity.py — FinOps Foundation Maturity Model (Crawl/Walk/Run)
self-assessment (https://www.finops.org/framework/maturity-model/).

Scores this app's own live/demo data against the model wherever there's a
real, computed number to check - and returns "Not Yet Measurable" for
capabilities the app doesn't have the underlying data for, rather than
inventing a figure just to fill a card. Framework content (Domains,
Capabilities, Maturity Model) is FinOps Foundation, licensed CC BY 4.0 -
this is an independent implementation, not a FinOps Foundation-certified
product.

Four capabilities now use real, officially-published FinOps Foundation
KPIs (Rate Optimization: Commitment Discounts coverage thresholds; Usage
Optimization: Cost Optimization Index (COIN); Anomaly Management: Anomaly
Detection Rate). Cost Allocation and Forecasting have published thresholds
but no source data in this app, so they stay "Not Yet Measurable" - an
honest gap, not a fake score.

Real bug fixed 2026-08-30: Usage Optimization and Anomaly Management used
to gate their stage on "does the detection code run at all" (has_detection
/ recs truthy) - both are true for virtually every tenant with any synced
data, so both ALWAYS showed "Crawl" regardless of the tenant's actual
numbers. Neither discriminated real maturity. Fixed by computing each
capability's own real, published KPI (COIN; Anomaly Detection Rate) so the
badge - not just the headline text next to it - responds to real,
tenant-specific data. The Crawl ceiling itself stays capped for both
(Walk/Run require automation this app doesn't have - see each function's
own docstring for the specific, concrete reason) since that ceiling is a
genuine limit of THIS APP's current feature set, not something that
varies by tenant - same honest-gap principle as Cost Allocation/
Forecasting, just with real tenant-specific detail layered underneath it
instead of a bare unchanging badge.
"""

from dataclasses import dataclass
from typing import Optional

from pricing.retail_pricing import fmt_currency as _fmt_currency

# Official sample thresholds from the FinOps Foundation Maturity Model.
COMMITMENT_DISCOUNT_THRESHOLDS = {"Crawl": 60.0, "Walk": 75.0, "Run": 80.0}
COST_ALLOCATION_THRESHOLDS = {"Crawl": 70.0, "Walk": 85.0, "Run": 90.0}
FORECASTING_THRESHOLDS_NOTE = "Crawl: <20% variance · Walk: <10% variance · Run: <5% variance (lower is better)"

# Anomaly Detection Rate - FinOps Foundation's published KPI thresholds
# (cost impact of anomalies as % of total spend). The Foundation does NOT
# publish a rate-to-STAGE mapping - the Crawl/Below Crawl split below is
# this app's own reasonable interpretation, disclosed as such in the
# returned CapabilityAssessment.detail, not presented as official.
ANOMALY_RATE_GREEN_MAX = 2.0
ANOMALY_RATE_YELLOW_MAX = 7.0

# COIN (Cost Optimization Index) - FinOps Foundation's published formula
# (see assess_usage_optimization). No official COIN-to-stage mapping
# exists either; these bands only drive the pill's color, not the stage.
COIN_GOOD_MIN = 97.0
COIN_WARN_MIN = 90.0

_STAGE_ORDER = ["Below Crawl", "Crawl", "Walk", "Run"]


@dataclass
class CapabilityAssessment:
    domain: str
    capability: str
    stage: str          # "Below Crawl" | "Crawl" | "Walk" | "Run" | "Not Yet Measurable"
    headline: str
    detail: str
    evidence: str
    # Populated only for capabilities with a real, tenant-specific KPI
    # number worth showing as its own badge (Usage Optimization, Anomaly
    # Management) - None for the others, which show no pill.
    kpi_label: Optional[str] = None
    kpi_tone: Optional[str] = None   # "good" | "warn" | "bad"


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


def assess_usage_optimization(inv_raw, ri_result, currency: str = "USD", inr_rate: float = 84.0) -> CapabilityAssessment:
    """Cost Optimization Index (COIN) - FinOps Foundation's published KPI
    for this capability: COIN = [1 - (Savings Opportunity / Total Cost)] x
    100. Savings Opportunity here = this session's orphaned-RI-drain $
    (ri_result.orphaned_ri_drain) - real usage waste this app already
    detects, distinct from Rate Optimization's RI/SP PURCHASE gaps (a
    different capability). Total Cost = the running fleet's on-demand
    baseline (Resource State == "Running" only - a stopped resource isn't
    accruing charges, same fix already applied to the Home KPI's sibling
    bug and the Recommendations tab's baseline).

    Stage stays capped at Crawl regardless of how good COIN is: the
    Foundation's own Walk criteria require "basic automation for routine
    tasks" and tracking "recommendations consistently from identification
    through resolution" - this app only detects waste, it never acts on
    it. Concretely: auto-fixing a resource needs WRITE access to the
    tenant's cloud account (this app's setup instructions only ever
    request read-tier roles - Reader/Cost Management Reader/Reservations
    Reader on Azure, ReadOnlyAccess on AWS - confirmed via grep, no write
    permission requested anywhere) plus a human-approval workflow, and
    neither exists yet. That's a real limit of this app's current feature
    set, not something that varies by tenant - same reasoning
    Cost Allocation/Forecasting already use for their own honest gaps."""
    fmt_money = lambda x, decimals=2: _fmt_currency(x, decimals=decimals, currency=currency, inr_rate=inr_rate)

    has_detection = (inv_raw is not None and not inv_raw.empty and "Is Orphaned" in inv_raw.columns)
    orphan_count = int(inv_raw["Is Orphaned"].sum()) if has_detection else 0

    baseline_mo = None
    orphan_mo = 0.0
    coin = None
    if has_detection:
        baseline_mo = float(inv_raw.loc[inv_raw["Resource State"] == "Running", "PAYG Hourly Cost USD"].sum()) * 730
        drain_df = ri_result.orphaned_ri_drain if ri_result is not None else None
        orphan_mo = float(drain_df["Monthly RI Drain"].sum()) if drain_df is not None and not drain_df.empty else 0.0
        if baseline_mo > 0:
            coin = (1 - (orphan_mo / baseline_mo)) * 100

    stage = "Crawl" if has_detection else "Not Yet Measurable"

    kpi_label = f"COIN {coin:.1f}" if coin is not None else None
    kpi_tone = None
    if coin is not None:
        kpi_tone = "good" if coin >= COIN_GOOD_MIN else ("warn" if coin >= COIN_WARN_MIN else "bad")

    if coin is not None:
        waste_pct = (orphan_mo / baseline_mo * 100) if baseline_mo > 0 else 0.0
        headline = (
            f"{fmt_money(orphan_mo, 2)}/mo ({waste_pct:.1f}%) in usage waste - "
            f"{orphan_count} orphaned resource{'s' if orphan_count != 1 else ''}"
        )
    elif has_detection:
        headline = f"{orphan_count} orphaned/idle resource(s) actively detected this session"
    else:
        headline = "No idle-resource detection available"

    return CapabilityAssessment(
        domain="Optimize Usage & Cost",
        capability="Usage Optimization",
        stage=stage,
        headline=headline,
        detail=(
            "COIN (FinOps Foundation KPI): [1 - Savings Opportunity / Total Cost] x 100. Here, "
            "Savings Opportunity = this session's orphaned-capacity drain.\n\n"
            "COIN is good, but stage stays at Crawl: Walk/Run need auto-fixing waste, not just "
            "detecting it - this app only detects it today.\n\n"
            "Why: fixing things automatically needs write access to your cloud account plus a "
            "human-approval step - this app only ever asks for read access, and neither exists yet.\n\n"
            "(FinOps doesn't publish a COIN-to-stage rule - this cap is our own choice, not theirs.)"
        ),
        evidence="Orphaned resource flagging (Asset Inventory tab) and orphaned RI drain detection (RI Coverage tab).",
        kpi_label=kpi_label,
        kpi_tone=kpi_tone,
    )


def assess_anomaly_management(recs, inv_raw, currency: str = "USD", inr_rate: float = 84.0) -> CapabilityAssessment:
    """Anomaly Detection Rate - FinOps Foundation's published KPI for this
    capability: cost impact of anomalies as % of total spend (Green <2%,
    Yellow 2-7%, Red >7%). "Anomalies" here = this session's HIGH-severity
    recommendation findings (RI Leakage, Orphaned Capacity) - the only two
    categories with real, non-zero dollar figures; this app's rule-based
    checks are the closest proxy it has to true anomaly detection.

    Green/Yellow stays capped at Crawl: the Foundation's own Walk criteria
    require alerts that "automatically route to responsible teams" - this
    app has no notification pipeline (email/Slack/webhook) and no
    ownership/tagging data to route to, so routing isn't possible yet
    regardless of how low the anomaly rate is. Red (>7%) drops to Below
    Crawl - letting that much spend go undetected-and-unrouted doesn't
    meet even the Crawl bar of "basic visibility.\""""
    fmt_money = lambda x, decimals=2: _fmt_currency(x, decimals=decimals, currency=currency, inr_rate=inr_rate)

    recs = recs or []
    has_data = inv_raw is not None and not inv_raw.empty
    high_severity = [r for r in recs if r.get("severity") == "HIGH"]
    high_impact_mo = sum(r.get("financial_impact_hr", 0.0) for r in high_severity) * 730

    baseline_mo = None
    rate = None
    zone = None
    if has_data:
        baseline_mo = float(inv_raw.loc[inv_raw["Resource State"] == "Running", "PAYG Hourly Cost USD"].sum()) * 730
        if baseline_mo > 0:
            rate = high_impact_mo / baseline_mo * 100
            if rate > ANOMALY_RATE_YELLOW_MAX:
                zone = "Red"
            elif rate >= ANOMALY_RATE_GREEN_MAX:
                zone = "Yellow"
            else:
                zone = "Green"

    if zone == "Red":
        stage = "Below Crawl"
    elif has_data:
        stage = "Crawl"
    else:
        stage = "Not Yet Measurable"

    kpi_label = f"{rate:.2f}% · {zone}" if rate is not None else None
    kpi_tone = {"Green": "good", "Yellow": "warn", "Red": "bad"}.get(zone)

    if rate is not None:
        headline = f"{fmt_money(high_impact_mo, 2)}/mo in flagged HIGH-severity issues"
    elif has_data:
        headline = f"{len(high_severity)} high-severity cost anomaly alert(s) surfaced this session"
    else:
        headline = "No anomaly detection available"

    return CapabilityAssessment(
        domain="Understand Usage & Cost",
        capability="Anomaly Management",
        stage=stage,
        headline=headline,
        detail=(
            "Anomaly Detection Rate (FinOps Foundation KPI): cost impact of anomalies as % of "
            f"spend. Green <{ANOMALY_RATE_GREEN_MAX:.0f}% · Yellow {ANOMALY_RATE_GREEN_MAX:.0f}-"
            f"{ANOMALY_RATE_YELLOW_MAX:.0f}% · Red >{ANOMALY_RATE_YELLOW_MAX:.0f}%.\n\n"
            "Green/Yellow stays at Crawl: alerts aren't auto-routed to teams yet, which Walk "
            "needs. Red drops to Below Crawl - too much cost is going undetected.\n\n"
            "Why: routing alerts needs a notification pipeline (email/Slack/webhook) and a way "
            "to know who owns what - neither exists yet.\n\n"
            "(FinOps doesn't publish a rate-to-stage rule either - this mapping is our own "
            "choice, not theirs.)"
        ),
        evidence="Recommendation engine - RI leakage and orphaned RI drain checks (the only two categories with real $ figures).",
        kpi_label=kpi_label,
        kpi_tone=kpi_tone,
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


def run_maturity_assessment(sp_result, ri_result, inv_raw, recs,
                             currency: str = "USD", inr_rate: float = 84.0) -> list[CapabilityAssessment]:
    """Runs every capability assessment this app can compute. Order matches
    how they're presented in the UI, not framework document order.

    currency/inr_rate (2026-08-30, same reasoning as generate_recommendations()'s
    own currency params): Usage Optimization/Anomaly Management now embed
    real $ amounts in their headline text - plain hashable params so a
    caller wrapping this in @st.cache_data busts its cache correctly when
    Display Currency changes, rather than silently returning stale-currency
    text."""
    return [
        assess_rate_optimization(sp_result, ri_result),
        assess_usage_optimization(inv_raw, ri_result, currency=currency, inr_rate=inr_rate),
        assess_anomaly_management(recs, inv_raw, currency=currency, inr_rate=inr_rate),
        assess_cost_allocation(),
        assess_forecasting(),
    ]
