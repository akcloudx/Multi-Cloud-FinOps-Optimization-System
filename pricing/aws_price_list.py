"""
pricing/aws_price_list.py
Live AWS Price List Query API lookup (boto3 'pricing' client), cached in the
RetailPrice SQL table - AWS's counterpart to pricing/azure_retail_api.py.

Facts verified 2026-08-21, not guessed (see project memory for the full
research trail):
  - Free to call: confirmed via AWS's own launch announcement
    (https://aws.amazon.com/blogs/aws/aws-price-list-api-update-new-query-and-metadata-functions/)
    - "available... at no charge."
  - The boto3 'pricing' client only exists in 3 regions - confirmed via
    botocore's own installed endpoints.json (ground truth, not a doc page):
    us-east-1, eu-central-1, ap-south-1. You always connect to ONE of these
    (hardcoded to us-east-1 below) regardless of which AWS region's prices
    you want - that's controlled by the regionCode filter in the request,
    unrelated to which endpoint you're connected to.
  - Requires pricing:GetProducts (aws/connector.py's REQUIRED_AWS_POLICIES) -
    a genuinely NEW permission, unlike Reservations, which reused
    inventory's existing policies. Managed policy AWSPriceListServiceFullAccess
    is functionally read-only despite the name - the Pricing service has no
    mutating actions at all (confirmed via its own policy JSON).
  - regionCode/instanceType/operatingSystem/databaseEngine/deploymentOption/
    tenancy/capacitystatus/licenseModel/preInstalledSw are all real,
    queryable product attribute names - confirmed against real downloaded
    price list files for both AmazonEC2 and AmazonRDS (not just docs).
  - The bulk/manual JSON files (https://pricing.us-east-1.amazonaws.com/offers/...)
    are genuinely public with zero AWS credentials needed (confirmed with a
    plain curl) but were ruled out: the EC2 us-east-1 file alone is 458MB,
    impractical to download per sync.

NOT independently live-tested against a real AWS account (no credentials
available in this environment) - the regionCode/instanceType filter
behavior and the exact candidate-selection logic below should be verified
once a real tenant with pricing:GetProducts granted runs a sync.
"""

import json
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from db.schema import RetailPrice

try:
    import boto3
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

CACHE_MAX_AGE_HOURS = 24
# The only 3 regions the 'pricing' client exists in - see module docstring.
_PRICING_API_REGION = "us-east-1"

# resource_type (as set by aws/connector.py's fetch_live_inventory) -> the
# exact databaseEngine attribute value used in AWS's own price list data -
# confirmed against real downloaded AmazonRDS price list entries.
_RDS_RESOURCE_TYPE_TO_DB_ENGINE = {
    "Amazon RDS for MySQL":         "MySQL",
    "Amazon RDS for PostgreSQL":    "PostgreSQL",
    "Amazon RDS for MariaDB":       "MariaDB",
    "Amazon RDS for Oracle":        "Oracle",
    "Amazon RDS for SQL Server":    "SQL Server",
    "Amazon Aurora (MySQL)":        "Aurora MySQL",
    "Amazon Aurora (PostgreSQL)":   "Aurora PostgreSQL",
}


def _extract_ondemand_prices(price_list_item: dict) -> list:
    """Returns every (price_usd, attributes) pair found under this product's
    terms.OnDemand section - deliberately NOT terms.Reserved, which lives
    right alongside it in the same response and contains 1yr/3yr term
    prices (and $0 "included in upfront fee" placeholder entries) that
    would silently corrupt a PAYG lookup if read from the wrong section."""
    attrs = price_list_item.get("product", {}).get("attributes", {})
    out = []
    for term in price_list_item.get("terms", {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            raw = dim.get("pricePerUnit", {}).get("USD")
            if raw is None:
                continue
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            if price > 0:   # skip $0 entries defensively - a real compute/instance rate is never legitimately free.
                out.append((price, attrs))
    return out


def _fetch_from_api(resource_type: str, sku: str, region: str, os_: str, redundancy: str, creds) -> Optional[float]:
    """Queries the live Price List Query API for a single resource_type/
    sku/region/os/redundancy combo. Returns the best-matching On-Demand
    hourly rate, or None if unpriceable (unmapped RDS engine, network
    failure, no matching product, etc.) - caller keeps whatever was already
    on the resource rather than fabricating a number, same discipline
    azure_retail_api.py's _fetch_from_api already follows."""
    if not HAS_BOTO3 or not sku or sku == "N/A" or not region:
        return None

    is_ec2 = (resource_type == "Compute")
    if is_ec2:
        service_code = "AmazonEC2"
    else:
        db_engine = _RDS_RESOURCE_TYPE_TO_DB_ENGINE.get(resource_type)
        if db_engine is None:
            return None   # unmapped/unrecognized RDS engine - don't guess a price for the wrong engine.
        service_code = "AmazonRDS"

    try:
        session = boto3.Session(
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
        )
        client = session.client("pricing", region_name=_PRICING_API_REGION)
        filters = [
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": sku},
        ]
        price_list = []
        next_token = None
        # A handful of pages is always enough for one instanceType in one
        # region (the realistic variant count - OS x tenancy x license x
        # capacity status - is a few dozen, not thousands); capped at 3
        # pages as a sane ceiling rather than looping unbounded on a
        # malformed/unexpected response.
        for _ in range(3):
            kwargs = {"ServiceCode": service_code, "Filters": filters, "MaxResults": 100}
            if next_token:
                kwargs["NextToken"] = next_token
            resp = client.get_products(**kwargs)
            price_list.extend(json.loads(p) for p in resp.get("PriceList", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
    except Exception:
        return None

    if not price_list:
        return None

    candidates = []
    for item in price_list:
        prices = _extract_ondemand_prices(item)
        if not prices:
            continue
        price, attrs = prices[0]   # OnDemand always has exactly one priceDimension per term in practice - confirmed against real EC2/RDS sample data.

        if is_ec2:
            # Excludes Dedicated/Host tenancy variants (different pricing
            # tier, not what a standard Shared-tenancy instance pays) and
            # "UnusedCapacityReservation" SKUs (a distinct reserved-capacity
            # product family, confirmed via real sample data containing
            # entries like usagetype="UnusedBox:c3.xlarge" that are NOT
            # standard On-Demand rates - would have silently produced a
            # wildly wrong price if not filtered out).
            if attrs.get("tenancy") != "Shared":
                continue
            if attrs.get("capacitystatus") != "Used":
                continue
            if attrs.get("operatingSystem") != os_:
                continue
        else:
            expected_deployment = "Multi-AZ" if redundancy == "Zone Redundant" else "Single-AZ"
            if attrs.get("deploymentOption") != expected_deployment:
                continue
            # db_engine is guaranteed set here (is_ec2 is False), already
            # validated as a real databaseEngine value above.
            if attrs.get("databaseEngine") != db_engine:
                continue

        candidates.append((price, attrs))

    if not candidates:
        return None

    # Prefer the plain base variant (no bundled software, standard
    # license) when multiple survive the filters above - e.g. EC2's
    # preInstalledSw distinguishes a bare OS from "Windows with SQL Server
    # Standard" pre-installed, a materially different (higher) rate for
    # the exact same instanceType/OS/region. Falls back to the cheapest
    # remaining candidate (mirroring azure_retail_api.py's own min()
    # fallback) rather than erroring if no "preferred" variant survives -
    # e.g. some RDS engines (Oracle, SQL Server) have no license-included
    # variant available in a given region.
    preferred = [
        (p, a) for p, a in candidates
        if a.get("preInstalledSw", "NA") == "NA"
        and a.get("licenseModel", "").lower() in ("no license required", "license included", "")
    ]
    pool = preferred or candidates
    return min(p for p, _ in pool)


def refresh_aws_prices(engine, resource_rows: list[dict], creds) -> dict:
    """
    AWS counterpart to azure_retail_api.refresh_retail_prices(). For each
    unique (Resource Type, SKU, Region, OS, Redundancy) in resource_rows,
    returns a cached-or-fresh hourly rate as
    {(resource_type, sku, region, os, redundancy): rate}. Reuses any
    RetailPrice row fetched within the last 24h; only hits the live API for
    stale or missing combos.

    Unlike Azure's version (a fully public, unauthenticated HTTP endpoint),
    this needs real AWS credentials - creds is required, not optional.
    """
    if not creds:
        return {}

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
        return {}

    cutoff = (datetime.utcnow() - timedelta(hours=CACHE_MAX_AGE_HOURS)).strftime("%Y-%m-%d %H:%M:%S UTC")
    rates: dict = {}

    with Session(engine) as session:
        cached = {
            (row.resource_type, row.sku, row.region, row.os, row.redundancy or "N/A"): row
            for row in session.query(RetailPrice).filter(RetailPrice.provider == "AWS").all()
        }
        for resource_type, sku, region, os_, redundancy in combos:
            key = (resource_type, sku, region, os_, redundancy)
            row = cached.get(key)
            if row and row.fetched_at >= cutoff:
                rates[key] = row.payg_rate_usd
                continue

            price = _fetch_from_api(resource_type, sku, region, os_, redundancy, creds)
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
                    sku=sku, region=region, os=os_, resource_type=resource_type, redundancy=redundancy,
                    payg_rate_usd=price, provider="AWS", fetched_at=now_iso,
                ))
        session.commit()

    return rates
