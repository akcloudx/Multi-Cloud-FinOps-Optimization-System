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

import re
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from db.schema import RetailPrice

RETAIL_API_URL = "https://prices.azure.com/api/retail/prices"
CACHE_MAX_AGE_HOURS = 24
_REQUEST_TIMEOUT = 8

# Azure SQL Database Serverless General Purpose - real, live-verified gap
# (2026-09-02): this app's own SKU string for a Serverless database is
# built from ARM's currentSku.name + currentSku.capacity (azure_conn/
# connector.py's KQL, e.g. "GP_S_Gen5_1" for General Purpose Serverless
# Gen5, 1 max vCore - the "_S_" marker IS how Azure's real ARM SKU naming
# distinguishes Serverless from Provisioned, confirmed via Microsoft's
# own currentSku.name convention). But the generic armSkuName lookup
# below queries Azure's real Retail Prices API for that exact literal
# string, and Serverless SQL Database compute is NOT priced under that
# convention at all - confirmed by querying the live, public Retail
# Prices API directly: General Purpose Serverless compute's real
# armSkuName is just "{N} vCore" (e.g. "1 vCore"), a bare vCore count
# with no tier/family prefix, completely different from every Provisioned
# tier's armSkuName (e.g. "SQLDB_GP_Compute_Gen5_2"). The naive lookup
# therefore always returns zero items, and the resource silently sits at
# $0.00 - not because it's actually free, but because it was never
# priceable with that query. Real rate for GP Serverless 1 vCore,
# confirmed live: ~$0.38-$0.59/hr depending on the exact meter, not $0.
#
# That bare "{N} vCore" armSkuName is ALSO genuinely ambiguous on its
# own - confirmed live it's shared with Azure Database for MySQL/
# PostgreSQL Flexible Server AND SQL Managed Instance, each an entirely
# different product/price. serviceName eq 'SQL Database' is required to
# disambiguate, plus a productName match on "General Purpose" +
# "Serverless" (excluding the Read Repl / shared-resource-management
# meters, which are separate billable components, not primary compute).
#
# Scoped to General Purpose only, disclosed not hidden: Hyperscale
# Serverless's real armSkuName/productName mapping was NOT independently
# verified with the same confidence (multiple overlapping "SingleDB
# Hyperscale - Serverless" vs plain "Hyperscale - Serverless" product
# names exist live, and which one a single-database resource actually
# corresponds to wasn't confirmed) - a Hyperscale Serverless SKU falls
# through to the generic lookup below (which will find nothing and
# return None, same "can't determine, don't guess" behavior as before
# this fix), rather than risk caching a wrong rate.
_SQL_SERVERLESS_GP_SKU_RE = re.compile(r"^GP_S_Gen\d+_(\d+)$")


def _fetch_sql_serverless_gp_rate(vcores: str, region: str) -> Optional[float]:
    """Live-verified 2026-09-02 for 1 vCore/westus3 (a real, non-free rate
    found and confirmed correct). Azure's own retail catalog is disclosed-
    inconsistent beyond that, not this app's own gap: e.g. 2 vCore/westus3
    has ONLY a free-tier meter with no armSkuName populated at all, no paid
    "2 vCore" entry to match - confirmed live, not assumed. Returns None
    for any combo the catalog doesn't expose this way, same graceful
    "can't determine" fallback as every other unpriceable SKU in this
    module, not a silent wrong answer."""
    filter_q = (
        f"armRegionName eq '{region}' and serviceName eq 'SQL Database' "
        f"and armSkuName eq '{vcores} vCore' and priceType eq 'Consumption' and currencyCode eq 'USD'"
    )
    try:
        resp = requests.get(
            RETAIL_API_URL, params={"$filter": filter_q}, timeout=_REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        items = resp.json().get("Items", [])
    except Exception:
        return None

    candidates = [
        i for i in items
        if i.get("unitOfMeasure") == "1 Hour"
        and "General Purpose" in i.get("productName", "")
        and "Serverless" in i.get("productName", "")
        and "Read Repl" not in i.get("productName", "")
        and "shared resource management" not in i.get("productName", "")
        and "zone redundancy" not in i.get("meterName", "").lower()
        and "free" not in i.get("meterName", "").lower()
    ]
    if not candidates:
        return None
    return min(float(i["retailPrice"]) for i in candidates)


def _fetch_from_api(sku: str, region: str, os_: str) -> Optional[float]:
    """Queries the live Retail Prices API for a single SKU/region/OS combo.
    Returns the best-matching hourly consumption rate, or None if unpriceable
    (non-VM SKUs, unmapped regions, network failure, etc - all handled the same
    way: no price found, caller keeps whatever was already on the resource)."""
    if not sku or sku == "N/A" or not region:
        return None

    serverless_gp_match = _SQL_SERVERLESS_GP_SKU_RE.match(sku)
    if serverless_gp_match:
        return _fetch_sql_serverless_gp_rate(serverless_gp_match.group(1), region)

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
