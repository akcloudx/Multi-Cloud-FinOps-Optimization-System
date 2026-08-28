"""
analysis/commitment_economics.py
Real commitment-discount economics - reads the CommitmentPriceCache rates
(pricing/commitment_pricing.py) instead of assuming a committed dollar buys a
dollar of PAYG. Answers the question the safety-buffer heuristic couldn't:
"what would this pool of resources actually cost under a 1-Year vs 3-Year
Savings Plan or Reserved Instance, and how much would that really save."

Where the cache has no rate for a SKU/region/OS (API had nothing, or the
tenant hasn't synced since a new resource type appeared), that resource is
counted at its PAYG rate with zero assumed discount - never a guessed number.
"""

import pandas as pd

TERMS = ["1yr", "3yr"]
TERM_LABELS = {"1yr": "1-Year", "3yr": "3-Year"}


def _hyperscale_replica_multiplier(resource_type: str, sku: str, ha_replicas) -> float:
    """Azure SQL Database Hyperscale (Single Database only, NOT Elastic Pool
    - the real ARM property highAvailabilityReplicaCount doesn't apply
    within a Hyperscale elastic pool) bills each High Availability secondary
    replica at the SAME per-vCore rate as the primary Compute meter -
    verified live 2026-08 against the real Azure pricing calculator (a
    database with 1 replica costs exactly 2x a database with 0), and
    confirmed no distinct 'replica' meter exists in the Retail Prices API
    (it's a quantity multiplier on the existing rate, not a different
    price). Returns 1.0 (no-op) for anything not Hyperscale Single Database,
    since only that combination's billing relationship for this property
    was verified - Business Critical also exposes this same ARM property,
    but its rate relationship (BC's base price already includes built-in
    HA architecture) wasn't checked, so it's deliberately left at 1.0
    rather than guessed."""
    if resource_type != "Azure SQL Database":
        return 1.0
    tier = (sku or "").split("_")[0].upper()
    if tier != "HS":
        return 1.0
    try:
        replicas = int(ha_replicas) if ha_replicas is not None and str(ha_replicas) != "nan" else 0
    except (TypeError, ValueError):
        replicas = 0
    return 1.0 + max(0, replicas)


def _lookup_rate(prices_df: pd.DataFrame, instrument: str, term: str, resource_type: str, region: str, sku: str, os_: str, redundancy: str = "N/A"):
    if prices_df is None or prices_df.empty:
        return None
    m = prices_df[
        (prices_df["instrument"] == instrument) &
        (prices_df["term"] == term) &
        (prices_df["resource_type"] == resource_type) &
        (prices_df["region"] == region) &
        (prices_df["sku"] == sku) &
        (prices_df["os"] == os_) &
        (prices_df["redundancy"] == (redundancy or "N/A"))
    ]
    if m.empty:
        return None
    return float(m.iloc[0]["effective_hourly_rate_usd"])


def _lookup_payg(prices_df: pd.DataFrame, resource_type: str, region: str, sku: str, os_: str, redundancy: str = "N/A"):
    """The PAYG rate cached alongside the commitment rates, from the exact
    same API snapshot - deliberately preferred over whatever PAYG number
    happens to be sitting on the inventory row. Demo/seed inventory carries
    hand-picked illustrative PAYG figures that can drift from live pricing
    over time; comparing a stale PAYG baseline against a freshly-fetched
    committed rate can produce nonsensical negative 'savings'. Returns None
    if nothing cached yet, so the caller can fall back to the inventory row.

    resource_type is part of the match (not just region/sku/os) because two
    different services can report an identical SKU string (e.g. SQL Managed
    Instance and SQL Elastic Pool both using "GP_Gen5_8") while pricing to
    genuinely different real rates - matching on SKU alone would silently
    return whichever service's row happened to be cached, not necessarily
    this one's (caught live via that exact SQL MI / Elastic Pool collision).

    redundancy is part of the match for the same reason: a Zone-Redundant
    SQL Database/Elastic Pool is priced genuinely differently (often
    cheaper) than the Standard variant of the identical SKU - verified live
    (2026-08, e.g. GP Gen5 4vCore Brazil South: $1.156848/hr Standard vs
    $0.694108/hr Zone Redundant)."""
    if prices_df is None or prices_df.empty:
        return None
    m = prices_df[
        (prices_df["resource_type"] == resource_type) &
        (prices_df["region"] == region) & (prices_df["sku"] == sku) & (prices_df["os"] == os_) &
        (prices_df["redundancy"] == (redundancy or "N/A"))
        & prices_df["payg_hourly_rate_usd"].notna()
    ]
    if m.empty:
        return None
    return float(m.iloc[0]["payg_hourly_rate_usd"])


def savings_plan_term_comparison(pool_df: pd.DataFrame, prices_df: pd.DataFrame) -> pd.DataFrame:
    """pool_df = SP-eligible, running, 24x7 resource rows for one pool
    (Compute or Database). Returns one row per term with real committed cost."""
    rows = []
    for term in TERMS:
        payg_total = 0.0
        committed_total = 0.0
        priced = 0
        for _, r in pool_df.iterrows():
            redundancy = r["Redundancy"] if "Redundancy" in r.index else "N/A"
            replica_mult = _hyperscale_replica_multiplier(
                r["Resource Type"], r["SKU"], r["HA Replicas"] if "HA Replicas" in r.index else 0
            )
            cached_payg = _lookup_payg(prices_df, r["Resource Type"], r["Region"], r["SKU"], r["OS"], redundancy)
            payg = cached_payg if cached_payg is not None else float(r["PAYG Hourly Cost USD"])
            payg *= replica_mult
            payg_total += payg
            rate = _lookup_rate(prices_df, "SavingsPlan", term, r["Resource Type"], r["Region"], r["SKU"], r["OS"], redundancy)
            if rate is not None:
                committed_total += rate * replica_mult
                priced += 1
            else:
                committed_total += payg
        savings_hr = payg_total - committed_total
        rows.append({
            "Term": TERM_LABELS[term],
            "term_key": term,
            "PAYG $/hr": payg_total,
            "Committed $/hr": committed_total,
            "Savings $/hr": savings_hr,
            "Discount %": (savings_hr / payg_total * 100) if payg_total > 0 else 0.0,
            "Monthly Savings": savings_hr * 730,
            "Priced Resources": priced,
            "Total Resources": len(pool_df),
        })
    return pd.DataFrame(rows)


def aws_savings_plan_term_comparison(pool_df: pd.DataFrame, tenant_row, sp_type: str):
    """AWS equivalent of savings_plan_term_comparison() - same output
    shape (one row per term: Term, term_key, PAYG $/hr, Committed $/hr,
    Savings $/hr, Discount %, Monthly Savings, Priced Resources, Total
    Resources), so every downstream consumer (headline sentence, bridge
    visual, has_real_pricing check) works unmodified. Reads a real,
    cached AWS discount % per term (aws/connector.py's
    fetch_savings_plans_recommendation, cached on the CloudTenant row by
    the sync pipeline) instead of a per-resource rate lookup the way the
    Azure version does - AWS Savings Plans apply one flat account-level
    discount across any eligible instance type for a given plan type +
    term + payment option (verified directly against botocore's own
    GetSavingsPlansPurchaseRecommendation service definition), so there's
    no per-SKU rate table to look up the way Azure's CommitmentPriceCache
    stores.

    sp_type is "compute" or "sagemaker" - the two pools with a clean
    one-to-one mapping onto a single real AWS SavingsPlansType value (see
    fetch_savings_plans_recommendation's own docstring for why the
    Database pool isn't covered here). Returns None if pool_df is empty
    or tenant_row is None, matching savings_plan_term_comparison's own
    "nothing to compare" behavior.

    A term with no cached discount yet (sync hasn't run, or that one
    fetch failed) falls back to 0% discount for just that term
    (Committed $/hr = PAYG $/hr, Priced Resources = 0) - the same
    fallback-to-PAYG-with-zero-discount the Azure version already uses
    per-resource when a specific SKU has no cached rate, just applied at
    the whole-term level since AWS's rate isn't per-resource."""
    if pool_df is None or pool_df.empty or tenant_row is None:
        return None

    payg_total = float(pool_df["PAYG Hourly Cost USD"].sum())
    total_resources = len(pool_df)

    rows = []
    for term in TERMS:
        discount_pct = getattr(tenant_row, f"aws_sp_{sp_type}_discount_{term}", None)
        if discount_pct is not None:
            committed_total = payg_total * (1 - discount_pct / 100.0)
            priced = total_resources
        else:
            committed_total = payg_total
            priced = 0
        savings_hr = payg_total - committed_total
        rows.append({
            "Term": TERM_LABELS[term],
            "term_key": term,
            "PAYG $/hr": payg_total,
            "Committed $/hr": committed_total,
            "Savings $/hr": savings_hr,
            "Discount %": (savings_hr / payg_total * 100) if payg_total > 0 else 0.0,
            "Monthly Savings": savings_hr * 730,
            "Priced Resources": priced,
            "Total Resources": total_resources,
        })
    return pd.DataFrame(rows)


def ri_gap_pricing(coverage_table: pd.DataFrame, inv_raw: pd.DataFrame, prices_df: pd.DataFrame) -> pd.DataFrame:
    """Augments the RI coverage table's under-covered (gap > 0, eligible,
    per-instance) rows with real 1yr/3yr purchase cost and monthly savings,
    so 'buy N more RIs' comes with an actual price instead of just a count."""
    out = coverage_table.copy()
    if out.empty:
        return out

    inv_for_lookup = inv_raw.copy()
    if "Redundancy" not in inv_for_lookup.columns:
        inv_for_lookup["Redundancy"] = "N/A"
    inv_for_lookup["Redundancy"] = inv_for_lookup["Redundancy"].fillna("N/A")
    payg_lookup = (
        inv_for_lookup.drop_duplicates(subset=["SKU", "Region", "OS", "Redundancy"])
        .set_index(["SKU", "Region", "OS", "Redundancy"])["PAYG Hourly Cost USD"]
        .to_dict()
    )

    for term in TERMS:
        rate_col = f"RI Rate {TERM_LABELS[term]} ($/hr)"
        savings_col = f"Monthly Savings if Purchased ({TERM_LABELS[term]})"
        rates, savings = [], []
        for _, row in out.iterrows():
            redundancy = row.get("Redundancy", "N/A") or "N/A"
            key = (row.get("SKU"), row.get("Region"), row.get("OS"), redundancy)
            cached_payg = _lookup_payg(prices_df, row.get("Resource Type"), row.get("Region"), row.get("SKU"), row.get("OS"), redundancy)
            payg = cached_payg if cached_payg is not None else payg_lookup.get(key)
            rate = _lookup_rate(prices_df, "ReservedInstance", term, row.get("Resource Type"), row.get("Region"), row.get("SKU"), row.get("OS"), redundancy)
            rates.append(rate)
            gap = row.get("gap", 0) or 0
            if rate is not None and payg is not None and gap > 0 and row.get("is_eligible", True) and row.get("coverage_model") == "instance":
                savings.append(gap * (payg - rate) * 730)
            else:
                savings.append(None)
        out[rate_col] = rates
        out[savings_col] = savings
    return out


def combined_monthly_savings(sp_pool_comparisons: list, ri_priced: pd.DataFrame, sp_term: str, ri_term: str) -> dict:
    """Rolls SP (chosen term) + RI (chosen term) into one combined monthly
    figure for the Cost Analysis tab - the 'if you implement this, here's
    what you actually get' number.

    sp_pool_comparisons: list of DataFrames from savings_plan_term_comparison()
                          (one per pool - Compute, Database, ...).
    ri_priced:            DataFrame from ri_gap_pricing().
    """
    sp_total = 0.0
    for df in sp_pool_comparisons:
        if df is None or df.empty:
            continue
        row = df[df["term_key"] == sp_term]
        if not row.empty:
            sp_total += float(row.iloc[0]["Monthly Savings"])

    ri_col = f"Monthly Savings if Purchased ({TERM_LABELS[ri_term]})"
    ri_total = 0.0
    if ri_priced is not None and not ri_priced.empty and ri_col in ri_priced.columns:
        ri_total = float(ri_priced[ri_col].dropna().sum())

    return {
        "sp_term": TERM_LABELS[sp_term],
        "ri_term": TERM_LABELS[ri_term],
        "sp_monthly_savings": sp_total,
        "ri_monthly_savings": ri_total,
        "total_monthly_savings": sp_total + ri_total,
    }
