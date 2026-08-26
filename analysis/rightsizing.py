"""
analysis/rightsizing.py
Azure VM Rightsizing - configurable, threshold-based utilization
classification (Underutilized / Overutilized / Optimal / Unknown).

Every default below is grounded in a real, cited source, not invented -
see the approved plan (VM Rightsizing, 2026-08-26) for the full research:
Azure Advisor's own published cost-recommendation thresholds, AWS Compute
Optimizer's real "Recommendation Preferences" feature (percentile,
headroom, lookback, presets), and FinOps Foundation's current framework
taxonomy. Azure Advisor itself does NOT expose threshold configuration to
customers (confirmed via Microsoft's own docs) - this app's own
configurable engine, mirroring AWS Compute Optimizer's shape instead, is
what actually gives a user control here.

Mirrors the shape of analysis/ri_eligibility.py and sp_eligibility.py -
one small, focused module, not folded into engine.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Defaults ("Balanced" preset) ────────────────────────────────────────
DEFAULT_PERCENTILE = 95            # matches Azure Advisor's own methodology
DEFAULT_CPU_UNDER_PCT = 40.0       # Azure Advisor's user-facing-workload resize ceiling
DEFAULT_CPU_OVER_PCT = 80.0        # Azure Advisor's user-facing-workload resize ceiling
DEFAULT_MEM_UNDER_PCT = 40.0       # mirrors DEFAULT_CPU_UNDER_PCT - AWS Compute Optimizer
                                    # doesn't publish a separate numeric default for memory,
                                    # it applies the same percentile/preference framework to
                                    # every monitored metric, so this app does the same here.
DEFAULT_MEM_AVAILABLE_PCT = 15.0   # inverse of Advisor's ~85% used ceiling (non-user-facing)
DEFAULT_LOOKBACK_DAYS = 14         # AWS Compute Optimizer's own default
DEFAULT_HEADROOM_PCT = 20.0        # AWS Compute Optimizer's own default "headroom"
DEFAULT_MIN_DAYS = 7               # conservative middle ground (shortest Advisor lookback option)

# Named presets, same convention AWS Compute Optimizer itself uses
# (Maximum savings / Balanced / Default / Maximum performance) - each is
# just a bundled set of the 7 tunable values below, not a separate
# mechanism. "Conservative" narrows the trigger bands and raises the
# evidence bar (fewer, more confident flags); "Aggressive" widens the
# bands and lowers it (more flags, more risk, chasing more savings).
# mem_under_pct mirrors cpu_under_pct within each preset for the same
# reason as DEFAULT_MEM_UNDER_PCT above - no separate published number.
PRESETS = {
    "Conservative": {
        "percentile": 95, "cpu_under_pct": 20.0, "cpu_over_pct": 90.0,
        "mem_under_pct": 20.0, "mem_available_pct": 10.0, "lookback_days": 30,
        "headroom_pct": 30.0, "min_days": 14,
    },
    "Balanced": {
        "percentile": DEFAULT_PERCENTILE, "cpu_under_pct": DEFAULT_CPU_UNDER_PCT,
        "cpu_over_pct": DEFAULT_CPU_OVER_PCT, "mem_under_pct": DEFAULT_MEM_UNDER_PCT,
        "mem_available_pct": DEFAULT_MEM_AVAILABLE_PCT,
        "lookback_days": DEFAULT_LOOKBACK_DAYS, "headroom_pct": DEFAULT_HEADROOM_PCT,
        "min_days": DEFAULT_MIN_DAYS,
    },
    "Aggressive": {
        "percentile": 90, "cpu_under_pct": 50.0, "cpu_over_pct": 70.0,
        "mem_under_pct": 50.0, "mem_available_pct": 25.0, "lookback_days": 7,
        "headroom_pct": 10.0, "min_days": 3,
    },
}

SETTINGS_FIELDS = ["percentile", "cpu_under_pct", "cpu_over_pct", "mem_under_pct", "mem_available_pct",
                    "lookback_days", "headroom_pct", "min_days"]


def get_rightsizing_settings(tenant_row) -> dict:
    """Returns the EFFECTIVE settings for a tenant: its own saved override
    per field where set, else the "Balanced" default for that field.
    tenant_row may be None (no tenant context yet) - returns pure defaults.
    Uses getattr, not direct attribute access, so a plain dict/namedtuple
    works here too (e.g. in a test), not only a real CloudTenant ORM row.

    Deliberately `is not None` per field, not `or` - a saved 0 (a real,
    legitimate value; AWS's own headroom preset includes 0%) would be
    wrongly treated as "unset" by a bare `or` fallback.
    """
    defaults = PRESETS["Balanced"]
    if tenant_row is None:
        return dict(defaults)
    return {
        field: (lambda v: v if v is not None else defaults[field])(
            getattr(tenant_row, f"rightsizing_{field}", None)
        )
        for field in SETTINGS_FIELDS
    }


@dataclass
class ClassificationResult:
    label: str              # "Underutilized" | "Overutilized" | "Optimal" | "Unknown"
    memory_available: bool  # whether a real memory reading existed for this check
    reason: str              # short human-readable explanation, shown as a UI tooltip


def classify_vm_utilization(
    resource_state: str,
    cpu_at_percentile,               # float | None
    mem_available_at_percentile,     # float | None - *available* (free) %, not used % - see below
    days_of_data,                     # int | None
    settings: dict,
) -> ClassificationResult:
    """Classifies a single VM. `settings` must be the *effective* dict from
    get_rightsizing_settings - this function never reads the module-level
    defaults directly, only what it's handed, so it's trivially testable
    against demo data without touching the DB or a tenant row at all.

    mem_available_at_percentile is *available* (free) memory %, matching
    the real Azure Monitor metric name (`Available Memory Percentage`),
    not inverted to "used %" - a VM with high available memory has
    headroom, not pressure. Getting this backwards was caught and fixed
    during planning; kept as a comment here so it can't quietly regress.

    Overutilized/Underutilized both roll up CPU and memory the same way
    AWS Compute Optimizer's real multi-metric findings do (2026-08-26,
    added after a user question about the original CPU-only-Underutilized
    design reading as an unexplained asymmetry): Overutilized fires if
    EITHER monitored metric shows high pressure, but Underutilized only
    fires if EVERY monitored metric is low - a VM idle on CPU but heavily
    using its memory is correctly held at Optimal, not flagged for a
    downsize that would starve it on the dimension CPU alone can't see.
    A missing memory reading never blocks Underutilized - it just means
    CPU alone decides for that VM, same fallback as the Overutilized side
    already had.
    """
    if resource_state != "Running":
        return ClassificationResult("Unknown", memory_available=False, reason="Resource is not running.")

    memory_available = mem_available_at_percentile is not None
    if cpu_at_percentile is None or days_of_data is None or days_of_data < settings["min_days"]:
        return ClassificationResult(
            "Unknown", memory_available,
            f"Fewer than {settings['min_days']} days of utilization data available."
        )

    p = settings["percentile"]
    # Appended whenever memory_available is False and the verdict didn't
    # already say so itself (the Underutilized-no-memory branch below
    # phrases its own version inline) - so a reader never has to infer
    # "no memory column value" from a blank table cell alone; the reason
    # text always names why memory didn't factor into this VM's verdict.
    mem_caveat = "" if memory_available else " Memory data unavailable — classified by CPU alone."

    # Overutilized: ANY monitored metric high.
    if cpu_at_percentile > settings["cpu_over_pct"]:
        return ClassificationResult(
            "Overutilized", memory_available, f"High CPU (P{p}={cpu_at_percentile:.0f}%).{mem_caveat}"
        )
    if memory_available and mem_available_at_percentile < settings["mem_available_pct"]:
        return ClassificationResult(
            "Overutilized", memory_available,
            f"Low available memory (P{p}={mem_available_at_percentile:.0f}% free)."
        )

    # Underutilized: EVERY monitored metric low. mem_usage_at_p is the
    # usage-% mirror of the stored available-% reading (100 - available) -
    # kept local to this branch so the rest of the module still only ever
    # stores/passes *available* %, per the comment above.
    cpu_low = cpu_at_percentile < settings["cpu_under_pct"]
    mem_usage_at_p = (100.0 - mem_available_at_percentile) if memory_available else None
    mem_low = (not memory_available) or (mem_usage_at_p < settings["mem_under_pct"])

    if cpu_low and mem_low:
        if memory_available:
            reason = f"Low CPU (P{p}={cpu_at_percentile:.0f}%) and low memory usage (P{p}={mem_usage_at_p:.0f}%)."
        else:
            reason = f"Low CPU (P{p}={cpu_at_percentile:.0f}%); memory data unavailable."
        return ClassificationResult("Underutilized", memory_available, reason)

    return ClassificationResult(
        "Optimal", memory_available, f"Utilization within configured thresholds.{mem_caveat}"
    )


# ── SKU size-ladder (explicitly scoped, not a real Azure SKU catalog) ──
# Heuristic, not validated against Azure's real Resource SKUs API: Azure's
# own VM naming convention embeds vCPU count as the number right after the
# family letter (e.g. "Standard_D4ds_v5" -> family "D", 4 vCPU, suffix
# "ds_v5"), and the common D/E/B-series sizes double at each step (2, 4,
# 8, 16, 32...) - this holds for every SKU this app's demo data actually
# uses, but is NOT guaranteed for every real Azure VM family/region.
# Production should query the real Resource SKUs API instead of this
# regex heuristic - disclosed gap, same pattern as this app's existing
# AWS Lambda/Timestream disclosures, not silently pretended complete.
_SKU_SIZE_PATTERN = re.compile(r"^(Standard_[A-Za-z]+?)(\d+)(.*)$")
# Suffix group is `.*` (any character), not `[A-Za-z_]*` - real bug caught
# in review before shipping: the version suffix itself contains a digit
# ("_v5", "_v4"), which a letters-only class can never match, so the
# original pattern silently rejected every real SKU this app actually
# uses (Standard_D4ds_v5 included) and suggest_target_sku() returned None
# for all of them. `\d+` (greedy) still only consumes the FIRST digit run
# right after the family letters - it stops at the first non-digit, so it
# can't accidentally swallow the later "5" in "_v5" too.


def _parse_sku_size(sku: str):
    m = _SKU_SIZE_PATTERN.match(sku or "")
    if not m:
        return None
    prefix, size_str, suffix = m.groups()
    return prefix, int(size_str), suffix


def suggest_target_sku(sku: str, direction: str, headroom_pct: float,
                        cpu_utilization_pct=None, mem_available_pct=None):
    """direction: "down" (Underutilized) or "up" (Overutilized). Returns
    (target_sku, projected_cpu_pct, projected_mem_available_pct) for one
    step in that direction, or None if the SKU doesn't match the known
    naming pattern, the resulting vCPU count isn't valid, or (downsize
    only) EITHER provided metric's projection would exceed its safe
    (100% - headroom) bound - i.e. AWS's real "headroom" concept actually
    gating candidate selection, not left as an unused number.

    cpu_utilization_pct and mem_available_pct are each optional
    independently - whichever is passed gets projected and safety-gated;
    the other's projection comes back None. Both are always passed by
    this app's one caller regardless of which metric actually drove the
    Under/Overutilized verdict (2026-08-26, extended after a user
    question): CPU alone used to decide *and* gate every suggestion, so a
    VM flagged Overutilized purely by low available memory got a
    CPU-only "after resize" number that never confirmed the resize
    actually relieved the memory pressure that caused the flag, and a
    downsize's memory impact was never safety-checked at all. Same
    "every provided metric must clear the bar" rule classify_vm_
    utilization already applies to the Underutilized verdict itself is
    now applied here too, to suggestion safety.

    Both metrics scale by the same vCPU ratio as the resize - assumes
    total memory scales with vCPU count within a family the same way this
    module's whole SKU-ladder heuristic already does (disclosed
    simplification, see the module docstring above), not confirmed via
    Azure's real per-SKU memory specs.
    """
def _project_metrics(ratio: float, headroom_pct: float, cpu_utilization_pct, mem_available_pct):
    """Shared by both providers' suggestion functions below - projects
    CPU/memory onto a candidate whose capacity differs from the current
    one by `ratio` (current_capacity / target_capacity: >1 for a
    downsize, <1 for an upsize), and safety-gates on whichever metric(s)
    are actually provided. Returns (projected_cpu, projected_mem_
    available) or None if either provided metric's projection would
    exceed the safe (100% - headroom) bound - i.e. AWS Compute
    Optimizer's real "headroom" concept actually gating candidate
    selection, not left as an unused number.

    Both metrics are independently optional - whichever is passed gets
    projected and safety-gated; the other's projection comes back None.
    Both are always passed by this app's callers regardless of which
    metric actually drove the Under/Overutilized verdict (2026-08-26,
    extended after a user question): CPU alone used to decide *and* gate
    every suggestion, so a VM flagged Overutilized purely by low
    available memory got a CPU-only "after resize" number that never
    confirmed the resize actually relieved the memory pressure that
    caused the flag, and a downsize's memory impact was never
    safety-checked at all. Same "every provided metric must clear the
    bar" rule classify_vm_utilization already applies to the
    Underutilized verdict itself is applied here too, to suggestion
    safety.
    """
    safe_floor = 100.0 - headroom_pct

    projected_cpu = None
    if cpu_utilization_pct is not None:
        projected_cpu = cpu_utilization_pct * ratio
        if projected_cpu > safe_floor:
            return None

    projected_mem_available = None
    if mem_available_pct is not None:
        # Available % -> usage %, scaled, back to available % - mirrors
        # classify_vm_utilization's own available/usage conversion, kept
        # local to this branch for the same reason (module only ever
        # stores/passes *available* % elsewhere, for both providers - see
        # db/aws_seed.py's conversion comment for why AWS is no exception
        # despite CloudWatch's own metric being usage-based).
        projected_mem_usage = (100.0 - mem_available_pct) * ratio
        if projected_mem_usage > safe_floor:
            return None
        projected_mem_available = 100.0 - projected_mem_usage

    return (
        round(projected_cpu, 1) if projected_cpu is not None else None,
        round(projected_mem_available, 1) if projected_mem_available is not None else None,
    )


def suggest_target_sku(sku: str, direction: str, headroom_pct: float,
                        cpu_utilization_pct=None, mem_available_pct=None):
    """Azure VM version - direction: "down" (Underutilized) or "up"
    (Overutilized). Returns (target_sku, projected_cpu_pct, projected_
    mem_available_pct), or None if the SKU doesn't match the known naming
    pattern, the resulting vCPU count isn't valid, or either provided
    metric's projection would be unsafe (see _project_metrics above).

    Both metrics scale by the same vCPU ratio as the resize - assumes
    total memory scales with vCPU count within a family the same way this
    module's whole SKU-ladder heuristic already does (disclosed
    simplification, see the module docstring above), not confirmed via
    Azure's real per-SKU memory specs.
    """
    parsed = _parse_sku_size(sku)
    if not parsed:
        return None
    prefix, vcpu, suffix = parsed
    target_vcpu = vcpu // 2 if direction == "down" else vcpu * 2
    if target_vcpu < 1:
        return None
    projected = _project_metrics(vcpu / target_vcpu, headroom_pct, cpu_utilization_pct, mem_available_pct)
    if projected is None:
        return None
    return (f"{prefix}{target_vcpu}{suffix}", *projected)


# ── AWS EC2 instance-type ladder (explicitly scoped, not a real AWS
# instance catalog) ──────────────────────────────────────────────────
# Real AWS naming: "{family}.{size}" (e.g. "m5.large", "c5.xlarge") - a
# completely different scheme from Azure's, so it needs its own parser,
# not a variant of _SKU_SIZE_PATTERN. Named-size ladder, stepped by
# position (not a numeric vCPU regex): AWS's real vCPU counts double at
# each step from "large" upward within a family (m5.large=2, xlarge=4,
# 2xlarge=8, ...) - this module assumes that same 2x-per-step ratio
# uniformly across the WHOLE ladder for the suggestion math below, which
# is accurate for large-and-up resizes but is NOT actually true for every
# real AWS family below "large" (burstable T-family instances hold a
# flat 2 vCPUs across nano/micro/small/medium/large before doubling from
# xlarge on) - disclosed simplification, same pattern as this app's
# existing Azure SKU-ladder and AWS Lambda/Timestream gaps, not silently
# pretended to be verified against AWS's real EC2 instance-type catalog.
_EC2_SIZE_LADDER = ["nano", "micro", "small", "medium", "large", "xlarge",
                    "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge", "24xlarge"]
_EC2_SKU_PATTERN = re.compile(r"^([a-z][a-z0-9]*)\.([a-z0-9]+)$")


def _parse_ec2_size(sku: str):
    m = _EC2_SKU_PATTERN.match(sku or "")
    if not m:
        return None
    family, size = m.groups()
    if size not in _EC2_SIZE_LADDER:
        return None
    return family, _EC2_SIZE_LADDER.index(size)


def suggest_target_ec2_type(sku: str, direction: str, headroom_pct: float,
                             cpu_utilization_pct=None, mem_available_pct=None):
    """AWS EC2 version of suggest_target_sku - same contract, same return
    shape, one ladder step at a time instead of a vCPU-doubling regex.
    See _EC2_SIZE_LADDER's comment for the shared 2x-per-step assumption
    this relies on.
    """
    parsed = _parse_ec2_size(sku)
    if not parsed:
        return None
    family, idx = parsed
    target_idx = idx - 1 if direction == "down" else idx + 1
    if not (0 <= target_idx < len(_EC2_SIZE_LADDER)):
        return None
    ratio = 2.0 if direction == "down" else 0.5
    projected = _project_metrics(ratio, headroom_pct, cpu_utilization_pct, mem_available_pct)
    if projected is None:
        return None
    return (f"{family}.{_EC2_SIZE_LADDER[target_idx]}", *projected)


def suggest_target_instance_type(provider: str, sku: str, direction: str, headroom_pct: float,
                                  cpu_utilization_pct=None, mem_available_pct=None):
    """Single entry point app.py calls regardless of provider - keeps the
    Azure-vs-AWS SKU-naming dispatch inside this module rather than
    leaking an if/else into the UI layer."""
    fn = suggest_target_sku if provider == "Azure" else suggest_target_ec2_type
    return fn(sku, direction, headroom_pct,
              cpu_utilization_pct=cpu_utilization_pct, mem_available_pct=mem_available_pct)


def estimate_resize_monthly_impact(provider: str, current_sku: str, target_sku: str,
                                    current_hourly_rate: float, avg_daily_running_hours: float):
    """Estimated $/month impact of moving from current_sku to target_sku:
    positive = savings (downsize), negative = added cost (upsize). None if
    either SKU doesn't match the known naming pattern for `provider`.

    Demo/v1 approximation, disclosed not hidden: scales current_hourly_rate
    by the same capacity ratio the suggestion itself used (vCPU ratio for
    Azure, 2x-per-ladder-step for AWS - see suggest_target_sku/
    suggest_target_ec2_type), rather than a live Retail Prices/Pricing API
    call per row on every render. A real per-SKU lookup already exists for
    production sync (pricing/azure_retail_api.py's `_fetch_from_api` for
    Azure; pricing/aws_price_list.py's own `_fetch_from_api` for AWS, both
    called once per owned resource during a live sync) - reusing either
    here for an arbitrary *unowned* candidate on every interactive table
    render would mean a live network call per row per rerun, which
    doesn't fit this app's "demo mode works fully offline" design.
    Production phase can swap this for the real per-provider lookup once
    rightsizing suggestions need to be this precise; not built now.
    """
    if provider == "Azure":
        current_parsed = _parse_sku_size(current_sku)
        target_parsed = _parse_sku_size(target_sku)
        if not current_parsed or not target_parsed:
            return None
        ratio = target_parsed[1] / current_parsed[1]
    else:
        current_parsed = _parse_ec2_size(current_sku)
        target_parsed = _parse_ec2_size(target_sku)
        if not current_parsed or not target_parsed:
            return None
        ratio = 2.0 ** (target_parsed[1] - current_parsed[1])
    target_hourly_rate = current_hourly_rate * ratio
    return round((current_hourly_rate - target_hourly_rate) * avg_daily_running_hours * 30, 2)
