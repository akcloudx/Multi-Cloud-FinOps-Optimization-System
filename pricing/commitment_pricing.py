"""
pricing/commitment_pricing.py
Real commitment-discount pricing (Reserved Instances / Savings Plans), cached
in the CommitmentPriceCache SQL table - the real $/hr rate you'd actually get
by committing, not the flat "safety-buffer fraction of PAYG" assumption the
engine used before this existed.

Called ONLY from the ingestion pipeline (data/sync_pipeline.py) and the demo
data loader, same architecture as pricing/azure_retail_api.py: API -> ingest
-> SQL DB -> UI/analysis engine reads the DB only, never the live API
directly. Fetched narrowly for the SKU/region/OS combos actually present in
inventory - not a full regional catalog dump.

Azure Retail Prices API docs: https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices
The api-version=2023-01-01-preview is required to get:
  - type eq 'Consumption' items' nested `savingsPlan: [{term, unitPrice}]` array
  - type eq 'Reservation' items' `reservationTerm` field

Per-resource-type query strategy (which Retail API field to match, what
scope to search, whether a Reservation price needs multiplying by a unit
count) lives in pricing/sku_mapping.py - only VM SKUs have a direct
armSkuName match; every other service was individually researched live
against the API (see that module's per-service notes) since Resource Graph's
reported SKU name frequently doesn't match the Retail API's naming for
non-VM services at all.

AWS is NOT implemented yet (Live AWS inventory ingestion itself doesn't exist
yet either - see aws/connector.py).
"""

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
from sqlalchemy.orm import Session

from db.schema import CommitmentPriceCache
from pricing.sku_mapping import resolve_sku_query, SkuQueryPlan

RETAIL_API_URL = "https://prices.azure.com/api/retail/prices"
_API_VERSION = "2023-01-01-preview"
_REQUEST_TIMEOUT = 8
CACHE_MAX_AGE_HOURS = 24
MONTH_HOURS = 730

_TERM_MONTHS = {"1yr": 12, "3yr": 36}
_AZURE_TERM_LABELS = {"1yr": "1 Year", "3yr": "3 Years"}


def _split_by_os(items: list) -> tuple:
    """Returns (windows_items, other_items). Which OS signal actually
    appears in productName is service-dependent and NOT consistent: VMs
    explicitly tag the Windows variant ("...Ddsv5 Series Windows") and leave
    the Linux/base variant untagged, while App Service does the opposite -
    it explicitly tags the Linux variant ("...Premium v3 Plan - Linux") and
    leaves the Windows/base variant untagged. Treating "no windows tag" as
    "wants non-Windows" (the original heuristic) silently matched an App
    Service Windows item as if it were Linux, comparing a Linux PAYG price
    against a Windows committed rate - verified live, this produced a
    nonsensical 'Savings Plan costs more than PAYG' result. Detect whichever
    tag is actually present in this item set and split on that; if neither
    tag appears (databases, storage, ...), there's no OS distinction to make
    at all and every item is returned as "other"."""
    has_linux_tag = any("linux" in i.get("productName", "").lower() for i in items)
    has_windows_tag = any("windows" in i.get("productName", "").lower() for i in items)
    if has_windows_tag:
        windows_items = [i for i in items if "windows" in i.get("productName", "").lower()]
        other_items = [i for i in items if "windows" not in i.get("productName", "").lower()]
    elif has_linux_tag:
        other_items = [i for i in items if "linux" in i.get("productName", "").lower()]
        windows_items = [i for i in items if "linux" not in i.get("productName", "").lower()]
    else:
        windows_items, other_items = [], list(items)
    return windows_items, other_items


def _os_matched_items(items: list, os_: str) -> list:
    windows_items, other_items = _split_by_os(items)
    if os_ == "Windows":
        return windows_items or items
    return other_items or items


def _exclude_noise_meters(items: list) -> list:
    """Excludes meter variants that should NEVER be picked as a 'baseline'
    price by a plain min()/first-match heuristic, regardless of the
    resource's actual configuration: Spot and Low Priority are
    opportunistic/interruptible pricing, not a real committable baseline.
    "Free" meters (verified live: SQL Database Serverless publishes decoy
    meters like "8 vCore - Free" priced at exactly $0.00 alongside the real
    "vCore" rate) are a sharper version of the same problem - a naive min()
    would ALWAYS pick a $0 meter over any real price. Zone Redundancy is
    deliberately NOT handled here - see _filter_by_redundancy, since
    whether to include or exclude it depends on the resource's actual
    redundancy setting, not a blanket rule."""
    noise = ("spot", "low priority", "free")
    return [
        i for i in items
        if not any(n in i.get("meterName", "").lower() for n in noise)
    ]


def _filter_by_redundancy(items: list, redundancy: str) -> list:
    """Selects the meter subset matching the resource's ACTUAL redundancy
    configuration - Zone Redundant is NOT a small surcharge over Standard,
    it's a genuinely different (and, verified live 2026-08, often CHEAPER)
    price entirely for SQL Database/Elastic Pool (e.g. GP Gen5 4vCore in
    Brazil South: $1.156848/hr Standard vs $0.694108/hr Zone Redundant).
    Blindly excluding it always (the original fix for the opposite problem -
    it being wrongly picked as a fake-cheap baseline for non-ZR resources)
    would silently mis-price a resource that IS actually Zone Redundant.
    Resources reporting "Zone Redundant" get the ZR subset if this SKU
    offers one, falling back to the standard subset if it doesn't (some
    tiers have no ZR variant at all). Everything else (Locally Redundant,
    N/A, unset) gets the standard subset - excluding ZR, same as before."""
    zr_items = [i for i in items if "zone redundan" in i.get("meterName", "").lower()]
    other_items = [i for i in items if "zone redundan" not in i.get("meterName", "").lower()]
    if (redundancy or "").strip().lower() in ("zone redundant", "zoneredundant", "zr"):
        return zr_items or other_items
    return other_items or items


def _normalize_name(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


_HOURLY_UNITS = {"1hour", "1/hour", "1gbhour"}


def _is_hourly(item: dict) -> bool:
    """Azure spells the hourly unit inconsistently across services - VMs/App
    Service/SQL Database use '1 Hour', but Synapse Analytics uses '1/Hour'
    for the exact same per-hour billing (verified live: DW500c's only
    Consumption item is tagged '1/Hour'). A strict '== \"1 Hour\"' check
    silently dropped every Synapse price, making it look unpriceable when
    it isn't. '1 GB Hour' (2026-08, Container Instances' memory meter) is
    ALSO a genuine hourly-equivalent rate - it's $/GB per hour, still
    billed continuously per hour, just with an extra per-GB dimension this
    app already multiplies out via consumption_multiplier_2 - excluding it
    made every memory-based resource-consumption lookup silently return
    None."""
    return _normalize_name(item.get("unitOfMeasure", "")) in _HOURLY_UNITS


def _query_retail_items(price_type: str, region: str, plan: SkuQueryPlan, match_value: str) -> list:
    """Fetches Retail Prices API items for one price_type ('Consumption' or
    'Reservation'), scoped by the query plan. armSkuName matches are pushed
    server-side (exact, efficient); skuName/meterName matches are filtered
    client-side after a serviceName-scoped fetch, since both have
    inconsistent spacing across tiers (e.g. App Service's "P2 v3" vs
    "P1mv4") that OData `eq` can't reliably normalize.
    match_field == "meterName" (2026-08, added for resource-based-
    consumption services like Container Instances) exists because skuName
    alone sometimes can't distinguish two genuinely different meters under
    the same product - e.g. Container Instances' "Standard vCPU Duration"
    and "Standard Memory Duration" both have skuName "Standard" and empty
    armSkuName, and are ONLY distinguishable by meterName."""
    filter_parts = [
        f"armRegionName eq '{region}'",
        f"priceType eq '{price_type}'",
        "currencyCode eq 'USD'",
    ]
    if plan.service_name:
        filter_parts.append(f"serviceName eq '{plan.service_name}'")
    if plan.match_field == "armSkuName" and match_value:
        filter_parts.append(f"armSkuName eq '{match_value}'")
    if plan.product_contains:
        filter_parts.append(f"contains(productName, '{plan.product_contains}')")
    filter_q = " and ".join(filter_parts)

    resp = requests.get(
        RETAIL_API_URL, params={"$filter": filter_q, "api-version": _API_VERSION}, timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("Items", [])

    if plan.match_field == "skuName" and match_value:
        target = _normalize_name(match_value)
        items = [i for i in items if _normalize_name(i.get("skuName", "")) == target]
    elif plan.match_field == "meterName" and match_value:
        target = _normalize_name(match_value)
        items = [i for i in items if _normalize_name(i.get("meterName", "")) == target]
    return items


def _compute_only_items(items: list, plan: SkuQueryPlan, os_: str) -> list:
    """Returns the item subset to treat as this resource's real compute
    cost. For Compute (VMs - plan.os_license_is_separable=True), this is
    ALWAYS the base/non-Windows-tagged meter regardless of the resource's
    actual OS: verified live + against the Azure pricing calculator that a
    Windows VM's tagged Consumption entry is a license-INCLUSIVE bundle
    carrying no savingsPlan array of its own, sitting alongside an untagged
    base entry that both (a) is the true compute-only price the discount
    attaches to and (b) is numerically identical to what a same-size Linux
    VM pays - VM compute hardware cost doesn't vary by OS, only the license
    does. By explicit decision this app excludes OS license cost entirely
    rather than tracking it as a surcharge, so Windows and Linux resources
    of the same VM SKU intentionally resolve to the same $/hr here.
    Every other service keeps genuine OS-matching (_os_matched_items) -
    e.g. App Service's Windows vs Linux tiers are genuinely different
    compute costs, not a license line bolted onto an identical base price,
    so there's nothing to strip out there."""
    if plan.os_license_is_separable:
        _, base_items = _split_by_os(items)
        return base_items or items
    return _os_matched_items(items, os_)


def _fetch_one_meter_rates(plan: SkuQueryPlan, region: str, os_: str, redundancy: str, match_value: str, multiplier: float) -> dict:
    """Core single-meter lookup shared by the primary meter and (for
    resource-based-consumption services) the secondary meter - see
    SkuQueryPlan.consumption_match_value_2. Returns
    {"payg": float|None, "1yr": float|None, "3yr": float|None}."""
    result = {"payg": None, "1yr": None, "3yr": None}
    try:
        items = _query_retail_items("Consumption", region, plan, match_value)
    except Exception:
        return result
    items = [i for i in items if _is_hourly(i)]
    items = _exclude_noise_meters(items)
    items = _filter_by_redundancy(items, redundancy)
    if not items:
        return result

    # PAYG baseline: the compute-only price (see _compute_only_items) -
    # falls back to the full set only when this service has no OS/meter
    # distinction at all (databases, storage, ...).
    os_matched = _compute_only_items(items, plan, os_)
    payg_item = min(os_matched, key=lambda i: float(i["retailPrice"]))
    result["payg"] = float(payg_item["retailPrice"]) * multiplier

    # Savings Plan rates attach to a specific meter - search within the SAME
    # compute-only subset first, so PAYG and the committed rate always come
    # from the same meter. Some non-VM services (e.g. PostgreSQL Flexible
    # Server) also reuse VM-style armSkuName values for their underlying
    # compute tier and can carry a savingsPlan entry of their own, so prefer
    # an actual "Compute" serviceFamily match when more than one candidate
    # exists, to avoid picking the wrong service's entry.
    sp_candidates = [i for i in os_matched if i.get("savingsPlan")] or [i for i in items if i.get("savingsPlan")]
    sp_item = next((i for i in sp_candidates if i.get("serviceFamily") == "Compute"), None) \
        or (sp_candidates[0] if sp_candidates else None)
    if not sp_item:
        return result

    for sp in sp_item.get("savingsPlan", []):
        term, rate = sp.get("term"), sp.get("unitPrice")
        if rate is None:
            continue
        scaled_rate = float(rate) * plan.savings_plan_multiplier * multiplier
        if term == "1 Year":
            result["1yr"] = scaled_rate
        elif term == "3 Years":
            result["3yr"] = scaled_rate
    return result


def _fetch_azure_sp_rates(resource_type: str, sku: str, region: str, os_: str, redundancy: str = "N/A") -> dict:
    """Returns {"payg": float|None, "1yr": float|None, "3yr": float|None},
    all already $/hr, COMPUTE COST ONLY - OS license (e.g. Windows Server)
    is deliberately excluded everywhere, not folded into either side, per
    explicit product decision. Savings Plan rates are natively hourly in
    the API. redundancy ("Zone Redundant"/"Locally Redundant"/"N/A")
    selects the matching meter for services where that changes the real
    price (SQL Database/Elastic Pool) - see _filter_by_redundancy."""
    result = {"payg": None, "1yr": None, "3yr": None}
    if not sku or sku == "N/A" or not region:
        return result

    plan = resolve_sku_query(resource_type, sku, redundancy)
    if not plan.supported:
        return result

    result = _fetch_one_meter_rates(plan, region, os_, redundancy, plan.consumption_match_value, plan.consumption_multiplier)

    # Resource-based-consumption services (Container Instances, Container
    # Apps Dedicated profile - see SkuQueryPlan.consumption_multiplier_2)
    # bill vCPU-hours and GB-hours as two genuinely separate meters that
    # must both be priced and ADDED together, not one meter scaled by a
    # single quantity. If the primary meter came back empty, or the second
    # meter (when one is configured) can't be priced, the WHOLE result is
    # unreliable - a half-priced resource is worse than an honestly blank
    # one, so this returns all-None rather than silently under-reporting.
    if plan.consumption_multiplier_2 and result["payg"] is not None:
        secondary = _fetch_one_meter_rates(plan, region, os_, redundancy, plan.consumption_match_value_2, plan.consumption_multiplier_2)
        if secondary["payg"] is None:
            return {"payg": None, "1yr": None, "3yr": None}
        result["payg"] += secondary["payg"]
        for term in ("1yr", "3yr"):
            if result[term] is not None and secondary[term] is not None:
                result[term] += secondary[term]
            else:
                result[term] = None
    return result


def _fetch_azure_ri_rates(resource_type: str, sku: str, region: str, os_: str, redundancy: str = "N/A") -> dict:
    """Returns {"1yr": float|None, "3yr": float|None}, normalized to $/hr:
    total-term price / (term_months * 730), multiplied first by
    plan.reservation_multiplier for services priced per-unit rather than
    per-instance (e.g. SQL Database/MI are priced per vCore, not per
    database - see pricing/sku_mapping.py). COMPUTE COST ONLY - see
    _fetch_azure_sp_rates / _compute_only_items for why OS license is
    excluded rather than added back as a surcharge. redundancy selects the
    matching meter the same way as the Consumption side - see
    _filter_by_redundancy."""
    result = {"1yr": None, "3yr": None}
    if not sku or sku == "N/A" or not region:
        return result

    plan = resolve_sku_query(resource_type, sku, redundancy)
    if not plan.supported or plan.reservation_unsupported_reason:
        return result

    # Azure Cosmos DB Reservations are purchased globally, not per-region -
    # confirmed 2026-08-23 directly against the real Retail Prices API
    # (every real Cosmos DB reservation price item carries
    # "armRegionName": "Global"), unlike every other Reservation type this
    # app prices, which are genuinely region-scoped meters. Querying with
    # the tenant's real region here would always return zero rows. Narrow,
    # resource-type-scoped override rather than a general "sometimes query
    # Global" parameter, since so far this is the only service that needs it.
    ri_query_region = "Global" if resource_type == "Azure Cosmos DB" else region

    try:
        ri_items = _query_retail_items("Reservation", ri_query_region, plan, plan.reservation_match_value)
    except Exception:
        return result
    ri_items = _exclude_noise_meters(ri_items)
    ri_items = _filter_by_redundancy(ri_items, redundancy)
    if not ri_items:
        return result

    ri_os_matched = _compute_only_items(ri_items, plan, os_)

    for term_key, months in _TERM_MONTHS.items():
        label = _AZURE_TERM_LABELS[term_key]
        matches = [c for c in ri_os_matched if c.get("reservationTerm") == label]
        if matches:
            unit_price = min(float(c["retailPrice"]) for c in matches)
            total_price = unit_price * plan.reservation_multiplier
            result[term_key] = total_price / (months * MONTH_HOURS)
    return result


def _fetch_aws_sp_rates(resource_type: str, sku: str, region: str, os_: str, redundancy: str = "N/A") -> dict:
    """AWS Savings Plans (Compute SP / EC2 Instance SP) pricing - not
    implemented yet. Live AWS inventory ingestion doesn't exist yet either
    (aws/connector.py), so there's nothing to look this up against in
    practice; returns all-None so callers degrade gracefully rather than
    crash."""
    return {"payg": None, "1yr": None, "3yr": None}


def _fetch_aws_ri_rates(resource_type: str, sku: str, region: str, os_: str, redundancy: str = "N/A") -> dict:
    """AWS Reserved Instances (EC2 Standard/Convertible, RDS) pricing - not
    implemented yet, same reasoning as _fetch_aws_sp_rates."""
    return {"1yr": None, "3yr": None}


def _upsert(session: Session, cached: dict, provider: str, instrument: str,
            resource_type: str, region: str, sku: str, os_: str, redundancy: str, term: str,
            hourly_rate: Optional[float], payg_rate: Optional[float], now_iso: str):
    if hourly_rate is None:
        return
    key = (provider, instrument, resource_type, region, sku, os_, redundancy, term)
    row = cached.get(key)
    if row:
        row.effective_hourly_rate_usd = hourly_rate
        row.payg_hourly_rate_usd = payg_rate
        row.fetched_at = now_iso
    else:
        session.add(CommitmentPriceCache(
            provider=provider, instrument=instrument, resource_type=resource_type, region=region, sku=sku, os=os_,
            redundancy=redundancy, term=term, effective_hourly_rate_usd=hourly_rate, payg_hourly_rate_usd=payg_rate,
            fetched_at=now_iso,
        ))


def refresh_commitment_prices(engine, resource_rows: list[dict], provider: str = "Azure") -> None:
    """
    For each unique (resource_type, sku, region, os, redundancy) in
    resource_rows, fetches real 1yr/3yr Savings Plan and Reserved Instance
    rates and upserts them into CommitmentPriceCache. Reuses any row fetched
    within CACHE_MAX_AGE_HOURS; only hits the live API for stale or missing
    combos. Resource type is needed (not just the SKU string) because the
    correct Retail API query strategy differs per service - see
    sku_mapping.py. Redundancy is needed because Zone-Redundant SQL
    Database/Elastic Pool meters are genuinely different (often cheaper)
    prices than Standard, not a surcharge - see _filter_by_redundancy.
    """
    combos = {
        (
            r.get("Resource Type") or r.get("resource_type"),
            r.get("SKU") or r.get("sku"),
            r.get("Region") or r.get("region"),
            r.get("OS") or r.get("os"),
            r.get("Redundancy") or r.get("redundancy") or "N/A",
        )
        for r in resource_rows
    }
    combos = {c for c in combos if c[1] and c[1] != "N/A"}
    if not combos:
        return

    cutoff = (datetime.utcnow() - timedelta(hours=CACHE_MAX_AGE_HOURS)).strftime("%Y-%m-%d %H:%M:%S UTC")
    sp_fetch = _fetch_azure_sp_rates if provider == "Azure" else _fetch_aws_sp_rates
    ri_fetch = _fetch_azure_ri_rates if provider == "Azure" else _fetch_aws_ri_rates

    with Session(engine) as session:
        cached = {
            (row.provider, row.instrument, row.resource_type, row.region, row.sku, row.os, row.redundancy, row.term): row
            for row in session.query(CommitmentPriceCache).filter(CommitmentPriceCache.provider == provider).all()
        }
        now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        for resource_type, sku, region, os_, redundancy in combos:
            sp_row = cached.get((provider, "SavingsPlan", resource_type, region, sku, os_, redundancy, "1yr"))
            ri_row = cached.get((provider, "ReservedInstance", resource_type, region, sku, os_, redundancy, "1yr"))
            sp_fresh = sp_row is not None and sp_row.fetched_at >= cutoff
            ri_fresh = ri_row is not None and ri_row.fetched_at >= cutoff
            if sp_fresh and ri_fresh:
                continue

            sp_rates = sp_fetch(resource_type, sku, region, os_, redundancy) if not sp_fresh else None
            ri_rates = ri_fetch(resource_type, sku, region, os_, redundancy) if not ri_fresh else None

            if sp_rates:
                _upsert(session, cached, provider, "SavingsPlan", resource_type, region, sku, os_, redundancy, "1yr", sp_rates.get("1yr"), sp_rates.get("payg"), now_iso)
                _upsert(session, cached, provider, "SavingsPlan", resource_type, region, sku, os_, redundancy, "3yr", sp_rates.get("3yr"), sp_rates.get("payg"), now_iso)
            if ri_rates:
                payg_for_ri = (sp_rates or {}).get("payg")
                _upsert(session, cached, provider, "ReservedInstance", resource_type, region, sku, os_, redundancy, "1yr", ri_rates.get("1yr"), payg_for_ri, now_iso)
                _upsert(session, cached, provider, "ReservedInstance", resource_type, region, sku, os_, redundancy, "3yr", ri_rates.get("3yr"), payg_for_ri, now_iso)

        session.commit()


def get_commitment_prices(engine, provider: str = "Azure") -> pd.DataFrame:
    """Reads the full cached commitment price table for a provider back out
    as a DataFrame - this is what the analysis engine and UI actually read;
    they never call the live API directly."""
    columns = ["provider", "instrument", "resource_type", "region", "sku", "os", "redundancy", "term",
               "effective_hourly_rate_usd", "payg_hourly_rate_usd", "fetched_at"]
    with Session(engine) as session:
        rows = session.query(CommitmentPriceCache).filter(CommitmentPriceCache.provider == provider).all()
        data = [{c: getattr(r, c) for c in columns} for r in rows]
    return pd.DataFrame(data, columns=columns)
