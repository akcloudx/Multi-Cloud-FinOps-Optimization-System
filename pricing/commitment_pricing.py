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
inventory - not a full regional catalog dump (that's the difference from the
spreadsheet workbook this was modeled on, which pulled thousands of rows per
region; targeting inventory keeps this fast and small while answering the
same question: "what would this resource cost under a 1yr/3yr commitment").

Azure Retail Prices API docs: https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices
The api-version=2023-01-01-preview is required to get:
  - type eq 'Consumption' items' nested `savingsPlan: [{term, unitPrice}]` array
  - type eq 'Reservation' items' `reservationTerm` field
Verified directly against the live API while building this (2026-08):
  Consumption item for Standard_D4ds_v5/eastus carries
    savingsPlan: [{"term": "1 Year", "unitPrice": 0.154471}, {"term": "3 Years", "unitPrice": 0.1032142}]
  Reservation items for the same SKU/region:
    {"reservationTerm": "1 Year", "unitPrice": 1168.0}   <- TOTAL price for the year, not hourly
    {"reservationTerm": "3 Years", "unitPrice": 2257.0}  <- TOTAL price for three years, not hourly

AWS is NOT implemented yet (Live AWS inventory ingestion itself doesn't exist
yet either - see aws/connector.py). The public API here is provider-agnostic
on purpose (`provider` param, "SavingsPlan"/"ReservedInstance" instrument
names that mean the same thing for AWS's own Compute/EC2 Instance Savings
Plans and EC2/RDS Reserved Instances) so AWS's Price List API can plug in
later via _fetch_aws_sp_rates/_fetch_aws_ri_rates without changing callers.
"""

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
from sqlalchemy.orm import Session

from db.schema import CommitmentPriceCache

RETAIL_API_URL = "https://prices.azure.com/api/retail/prices"
_API_VERSION = "2023-01-01-preview"
_REQUEST_TIMEOUT = 8
CACHE_MAX_AGE_HOURS = 24
MONTH_HOURS = 730

_TERM_MONTHS = {"1yr": 12, "3yr": 36}
_AZURE_TERM_LABELS = {"1yr": "1 Year", "3yr": "3 Years"}


def _matches_os(item: dict, os_: str) -> bool:
    wants_windows = (os_ == "Windows")
    return ("windows" in item.get("productName", "").lower()) == wants_windows


def _fetch_consumption_items(sku: str, region: str) -> list:
    filter_q = (
        f"armRegionName eq '{region}' and armSkuName eq '{sku}' "
        f"and priceType eq 'Consumption' and currencyCode eq 'USD'"
    )
    resp = requests.get(
        RETAIL_API_URL,
        params={"$filter": filter_q, "api-version": _API_VERSION},
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = [i for i in resp.json().get("Items", []) if i.get("unitOfMeasure") == "1 Hour"]
    return [
        i for i in items
        if "spot" not in i.get("meterName", "").lower()
        and "low priority" not in i.get("meterName", "").lower()
    ]


def _windows_license_surcharge(items: list, os_: str, base_meter_id: Optional[str]) -> float:
    """Azure Savings Plans and Reservations commit against the base compute
    meter only - a Windows VM's OS-license premium keeps billing at PAYG on
    top of the committed rate even after committing (unless Azure Hybrid
    Benefit is applied, which this app doesn't model). Verified directly
    against the live API while building this: the item carrying
    `savingsPlan`/appearing under Reservation has productName WITHOUT
    "Windows" in it, while the Windows-priced item has no savingsPlan entry
    at all - they're the same SKU, two different meters. Returns the $/hr
    delta to add on top of the committed base rate; 0 for non-Windows OS."""
    if os_ != "Windows" or base_meter_id is None:
        return 0.0
    windows_items = [i for i in items if _matches_os(i, "Windows")]
    base_items = [i for i in items if i.get("meterId") == base_meter_id]
    if not windows_items or not base_items:
        return 0.0
    windows_payg = min(float(i["retailPrice"]) for i in windows_items)
    base_payg = float(base_items[0]["retailPrice"])
    return max(0.0, windows_payg - base_payg)


def _fetch_azure_sp_rates(sku: str, region: str, os_: str) -> dict:
    """Returns {"payg": float|None, "1yr": float|None, "3yr": float|None},
    all already $/hr - Savings Plan rates are natively hourly in the API.
    1yr/3yr include the Windows license surcharge on top of the committed
    base-compute rate where applicable (see _windows_license_surcharge)."""
    result = {"payg": None, "1yr": None, "3yr": None}
    if not sku or sku == "N/A" or not region:
        return result

    try:
        items = _fetch_consumption_items(sku, region)
    except Exception:
        return result
    if not items:
        return result

    # PAYG baseline: what the customer actually pays today for this OS.
    os_matched = [i for i in items if _matches_os(i, os_)] or items
    payg_item = min(os_matched, key=lambda i: float(i["retailPrice"]))
    result["payg"] = float(payg_item["retailPrice"])

    # Savings Plan rates attach to the base compute meter, not the
    # Windows-suffixed one - find whichever item actually carries them.
    # Some non-VM services (e.g. PostgreSQL Flexible Server) reuse VM-style
    # armSkuName values for their underlying compute tier and can also carry
    # a savingsPlan entry, so prefer an actual "Compute" serviceFamily match
    # when more than one candidate exists, to avoid picking the wrong one.
    sp_candidates = [i for i in items if i.get("savingsPlan")]
    sp_item = next((i for i in sp_candidates if i.get("serviceFamily") == "Compute"), None) \
        or (sp_candidates[0] if sp_candidates else None)
    if not sp_item:
        return result

    surcharge = _windows_license_surcharge(items, os_, sp_item.get("meterId"))
    for sp in sp_item.get("savingsPlan", []):
        term, rate = sp.get("term"), sp.get("unitPrice")
        if rate is None:
            continue
        if term == "1 Year":
            result["1yr"] = float(rate) + surcharge
        elif term == "3 Years":
            result["3yr"] = float(rate) + surcharge
    return result


def _fetch_azure_ri_rates(sku: str, region: str, os_: str) -> dict:
    """Returns {"1yr": float|None, "3yr": float|None}, normalized to $/hr by
    dividing the API's total-term price by (term_months * 730), plus the
    Windows license surcharge on top where applicable (reservations are also
    base-compute-only - see _windows_license_surcharge)."""
    result = {"1yr": None, "3yr": None}
    if not sku or sku == "N/A" or not region:
        return result

    filter_q = (
        f"armRegionName eq '{region}' and armSkuName eq '{sku}' "
        f"and priceType eq 'Reservation' and currencyCode eq 'USD'"
    )
    try:
        resp = requests.get(
            RETAIL_API_URL,
            params={"$filter": filter_q, "api-version": _API_VERSION},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        ri_items = resp.json().get("Items", [])
        consumption_items = _fetch_consumption_items(sku, region)
    except Exception:
        return result
    if not ri_items:
        return result

    surcharge = 0.0
    if ri_items:
        surcharge = _windows_license_surcharge(consumption_items, os_, ri_items[0].get("meterId"))

    for term_key, months in _TERM_MONTHS.items():
        label = _AZURE_TERM_LABELS[term_key]
        matches = [c for c in ri_items if c.get("reservationTerm") == label]
        if matches:
            total_price = min(float(c["retailPrice"]) for c in matches)
            result[term_key] = total_price / (months * MONTH_HOURS) + surcharge
    return result


def _fetch_aws_sp_rates(sku: str, region: str, os_: str) -> dict:
    """AWS Savings Plans (Compute SP / EC2 Instance SP) pricing - not
    implemented yet. Live AWS inventory ingestion doesn't exist yet either
    (aws/connector.py), so there's nothing to look this up against in
    practice; returns all-None so callers degrade gracefully rather than
    crash. Real implementation would call AWS's Price List API / Savings
    Plans pricing endpoints with the same (sku, region, os) -> {"payg","1yr","3yr"} shape."""
    return {"payg": None, "1yr": None, "3yr": None}


def _fetch_aws_ri_rates(sku: str, region: str, os_: str) -> dict:
    """AWS Reserved Instances (EC2 Standard/Convertible, RDS) pricing - not
    implemented yet, same reasoning as _fetch_aws_sp_rates."""
    return {"1yr": None, "3yr": None}


def _upsert(session: Session, cached: dict, provider: str, instrument: str,
            region: str, sku: str, os_: str, term: str,
            hourly_rate: Optional[float], payg_rate: Optional[float], now_iso: str):
    if hourly_rate is None:
        return
    key = (provider, instrument, region, sku, os_, term)
    row = cached.get(key)
    if row:
        row.effective_hourly_rate_usd = hourly_rate
        row.payg_hourly_rate_usd = payg_rate
        row.fetched_at = now_iso
    else:
        session.add(CommitmentPriceCache(
            provider=provider, instrument=instrument, region=region, sku=sku, os=os_,
            term=term, effective_hourly_rate_usd=hourly_rate, payg_hourly_rate_usd=payg_rate,
            fetched_at=now_iso,
        ))


def refresh_commitment_prices(engine, resource_rows: list[dict], provider: str = "Azure") -> None:
    """
    For each unique (sku, region, os) in resource_rows, fetches real 1yr/3yr
    Savings Plan and Reserved Instance rates and upserts them into
    CommitmentPriceCache. Reuses any row fetched within CACHE_MAX_AGE_HOURS;
    only hits the live API for stale or missing combos - same caching
    discipline as pricing/azure_retail_api.py.
    """
    combos = {
        (r.get("SKU") or r.get("sku"), r.get("Region") or r.get("region"), r.get("OS") or r.get("os"))
        for r in resource_rows
    }
    combos = {c for c in combos if c[0] and c[0] != "N/A"}
    if not combos:
        return

    cutoff = (datetime.utcnow() - timedelta(hours=CACHE_MAX_AGE_HOURS)).strftime("%Y-%m-%d %H:%M:%S UTC")
    sp_fetch = _fetch_azure_sp_rates if provider == "Azure" else _fetch_aws_sp_rates
    ri_fetch = _fetch_azure_ri_rates if provider == "Azure" else _fetch_aws_ri_rates

    with Session(engine) as session:
        cached = {
            (row.provider, row.instrument, row.region, row.sku, row.os, row.term): row
            for row in session.query(CommitmentPriceCache).filter(CommitmentPriceCache.provider == provider).all()
        }
        now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        for sku, region, os_ in combos:
            sp_row = cached.get((provider, "SavingsPlan", region, sku, os_, "1yr"))
            ri_row = cached.get((provider, "ReservedInstance", region, sku, os_, "1yr"))
            sp_fresh = sp_row is not None and sp_row.fetched_at >= cutoff
            ri_fresh = ri_row is not None and ri_row.fetched_at >= cutoff
            if sp_fresh and ri_fresh:
                continue

            sp_rates = sp_fetch(sku, region, os_) if not sp_fresh else None
            ri_rates = ri_fetch(sku, region, os_) if not ri_fresh else None

            if sp_rates:
                _upsert(session, cached, provider, "SavingsPlan", region, sku, os_, "1yr", sp_rates.get("1yr"), sp_rates.get("payg"), now_iso)
                _upsert(session, cached, provider, "SavingsPlan", region, sku, os_, "3yr", sp_rates.get("3yr"), sp_rates.get("payg"), now_iso)
            if ri_rates:
                payg_for_ri = (sp_rates or {}).get("payg")
                _upsert(session, cached, provider, "ReservedInstance", region, sku, os_, "1yr", ri_rates.get("1yr"), payg_for_ri, now_iso)
                _upsert(session, cached, provider, "ReservedInstance", region, sku, os_, "3yr", ri_rates.get("3yr"), payg_for_ri, now_iso)

        session.commit()


def get_commitment_prices(engine, provider: str = "Azure") -> pd.DataFrame:
    """Reads the full cached commitment price table for a provider back out
    as a DataFrame - this is what the analysis engine and UI actually read;
    they never call the live API directly."""
    columns = ["provider", "instrument", "region", "sku", "os", "term",
               "effective_hourly_rate_usd", "payg_hourly_rate_usd", "fetched_at"]
    with Session(engine) as session:
        rows = session.query(CommitmentPriceCache).filter(CommitmentPriceCache.provider == provider).all()
        data = [{c: getattr(r, c) for c in columns} for r in rows]
    return pd.DataFrame(data, columns=columns)
