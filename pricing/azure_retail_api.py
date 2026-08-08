"""
pricing/azure_retail_api.py
Live Azure Retail Prices API lookup, cached in the RetailPrice SQL table.

This module is called ONLY from the ingestion pipeline (data/sync_pipeline.py),
never from the Streamlit render path - the app always reads pricing back out of
the SQL DB, per the architecture: API -> ingest -> SQL DB -> UI reads DB only.

API docs: https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices
Public, unauthenticated, no SDK required:
  GET https://prices.azure.com/api/retail/prices?$filter=
      armRegionName eq 'eastus' and armSkuName eq 'Standard_D4ds_v5' and
      priceType eq 'Consumption' and currencyCode eq 'USD'
"""

from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from db.schema import RetailPrice

RETAIL_API_URL = "https://prices.azure.com/api/retail/prices"
CACHE_MAX_AGE_HOURS = 24
_REQUEST_TIMEOUT = 8


def _fetch_from_api(sku: str, region: str, os_: str) -> Optional[float]:
    """Queries the live Retail Prices API for a single SKU/region/OS combo.
    Returns the best-matching hourly consumption rate, or None if unpriceable
    (non-VM SKUs, unmapped regions, network failure, etc - all handled the same
    way: no price found, caller keeps whatever was already on the resource)."""
    if not sku or sku == "N/A" or not region:
        return None

    filter_q = (
        f"armRegionName eq '{region}' and armSkuName eq '{sku}' "
        f"and priceType eq 'Consumption' and currencyCode eq 'USD'"
    )
    try:
        resp = requests.get(
            RETAIL_API_URL, params={"$filter": filter_q}, timeout=_REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        items = resp.json().get("Items", [])
    except Exception:
        return None

    if not items:
        return None

    wants_windows = (os_ == "Windows")
    hourly = [i for i in items if i.get("unitOfMeasure") == "1 Hour"]
    candidates = [
        i for i in hourly
        if ("windows" in i.get("productName", "").lower()) == wants_windows
        and "spot" not in i.get("meterName", "").lower()
        and "low priority" not in i.get("meterName", "").lower()
    ] or hourly

    if not candidates:
        return None
    return min(float(i["retailPrice"]) for i in candidates)


def refresh_retail_prices(engine, resource_rows: list[dict], provider: str = "Azure") -> dict:
    """
    For each unique (sku, region, os) in resource_rows, returns a cached-or-fresh
    hourly rate as {(sku, region, os): rate}. Reuses any RetailPrice row fetched
    within the last 24h; only hits the live API for stale or missing combos.
    """
    combos = {
        (r.get("SKU") or r.get("sku"), r.get("Region") or r.get("region"), r.get("OS") or r.get("os"))
        for r in resource_rows
    }
    combos = {c for c in combos if c[0] and c[0] != "N/A"}
    if not combos:
        return {}

    cutoff = (datetime.utcnow() - timedelta(hours=CACHE_MAX_AGE_HOURS)).strftime("%Y-%m-%d %H:%M:%S UTC")
    rates: dict = {}

    with Session(engine) as session:
        cached = {
            (row.sku, row.region, row.os): row
            for row in session.query(RetailPrice).filter(RetailPrice.provider == provider).all()
        }
        for sku, region, os_ in combos:
            key = (sku, region, os_)
            row = cached.get(key)
            if row and row.fetched_at >= cutoff:
                rates[key] = row.payg_rate_usd
                continue

            price = _fetch_from_api(sku, region, os_)
            if price is None:
                if row:
                    rates[key] = row.payg_rate_usd  # keep stale cache over no data at all
                continue

            rates[key] = price
            now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            if row:
                row.payg_rate_usd = price
                row.fetched_at = now_iso
            else:
                session.add(RetailPrice(
                    sku=sku, region=region, os=os_,
                    payg_rate_usd=price, provider=provider, fetched_at=now_iso,
                ))
        session.commit()

    return rates
