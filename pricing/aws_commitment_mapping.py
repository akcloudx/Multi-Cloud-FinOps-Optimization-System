"""
pricing/aws_commitment_mapping.py
Maps a real AWS Reserved Instance / Savings Plan purchase record
(AWSReservationPurchase / AWSSavingsPlanPurchase - schema-matched to AWS's
actual APIs, see db/schema.py) into this app's own simplified Commitment
table - the table RI Coverage / Savings Plan Analysis actually read
(commitments/existing_commitments.py). AWS counterpart to
pricing/commitment_mapping.py (Azure).

Materially simpler than the Azure side, for two confirmed, not guessed,
reasons:
  - An AWS Reservation purchase record already carries UsagePrice/
    FixedPrice/Duration - together these fully determine the real
    effective hourly rate with NO separate pricing-API lookup needed
    (Azure Reservations carry no $ amount at all and need
    pricing/commitment_pricing.py's Retail Prices lookup for this).
  - AWS's Savings Plan API states savingsPlanType directly (confirmed via
    boto3's own enum: "Compute" | "EC2Instance" | "SageMaker" | "Database")
    - no sku_name-based guessing needed the way Azure's mapping has to do.

commitment_type strings ("Reserved Instance", "Compute Savings Plan",
"EC2 Instance Savings Plan", "Database Savings Plan") match exactly what
commitments/existing_commitments.py's bucketing expects. "SageMaker" is
the one Savings Plan type still deliberately dropped (raw purchase still
stored in AWSSavingsPlanPurchase for completeness, no Commitment row) -
this app has no SageMaker inventory model at all to match coverage
against, same "don't fabricate what can't be priced/modeled" discipline
the Azure mapping module already follows for unmodeled
reserved_resource_type values. Database Savings Plans are NOT dropped -
confirmed as a real AWS product via AWS's own FAQ, see
derive_aws_savings_plan_commitment_fields()'s docstring below for the
full verification.
"""

from typing import Optional

from aws.connector import map_ec2_platform, map_rds_engine

# AWS's own definition, confirmed via https://docs.aws.amazon.com/savingsplans/latest/userguide/what-is-savings-plans.html:
# "One year: ... 365 days (31,536,000 seconds). Three years: ... 1,095 days
# (94,608,000 seconds)." Same constants confirmed as the only valid
# Duration filter values for RDS reservations
# (https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DescribeReservedDBInstances.html:
# "Valid Values: 1 | 3 | 31536000 | 94608000").
_DURATION_SECONDS_TO_TERM = {31536000: "1-year", 94608000: "3-year"}


def _effective_hourly_rate(usage_price, fixed_price, duration_seconds, recurring_hourly) -> Optional[float]:
    """Amortized fixed_price (spread over the full term) + usage_price
    (the per-hour rate charged while running) + any additional recurring
    hourly charge not already folded into usage_price (RecurringCharges is
    a genuinely separate field on the API response - e.g. some
    license-included Windows/SQL Server RIs bill an extra recurring
    per-hour software fee this way, distinct from the base compute
    UsagePrice). This is the standard, widely-used formula for an AWS RI's
    real effective hourly cost - not independently verified against a real
    billed invoice in this environment (no AWS account available), worth
    a sanity check against real numbers once a tenant with actual
    Reservations syncs."""
    if not duration_seconds:
        return None
    hours = duration_seconds / 3600.0
    amortized_fixed = (fixed_price or 0.0) / hours
    return amortized_fixed + (usage_price or 0.0) + (recurring_hourly or 0.0)


def derive_aws_reservation_commitment_fields(purchase: dict) -> Optional[dict]:
    """Maps one AWSReservationPurchase-shaped dict into a full
    Commitment-shaped dict, hourly_usd_commitment INCLUDED (unlike the
    Azure side, no separate pricing lookup needed - see module docstring).
    Returns None only if duration_seconds is missing/zero (can't compute a
    rate) or the term isn't a recognized 1yr/3yr value."""
    service = purchase.get("service")
    duration = purchase.get("duration_seconds") or 0
    term = _DURATION_SECONDS_TO_TERM.get(duration)
    if term is None:
        return None

    rate = _effective_hourly_rate(
        purchase.get("usage_price"), purchase.get("fixed_price"), duration, purchase.get("recurring_charge_hourly"),
    )
    if rate is None:
        return None

    if service == "EC2":
        scope_resource_type = "Compute"
        scope_os = map_ec2_platform(purchase.get("product_description") or "")
        scope_redundancy = "N/A"
    elif service == "RDS":
        scope_resource_type = map_rds_engine(purchase.get("product_description") or "")
        scope_os = "N/A"
        scope_redundancy = "Zone Redundant" if purchase.get("multi_az") else "Locally Redundant"
    elif service == "ElastiCache":
        # This app's inventory taxonomy tracks ElastiCache as one flat
        # "Amazon ElastiCache" resource_type regardless of engine (aws/
        # connector.py's fetch_live_inventory doesn't split redis vs
        # memcached vs valkey either) - matched here for consistency, not
        # further split by product_description's real redis/memcached/
        # valkey value even though that data is available on the purchase
        # record. Redundancy: unlike RDS, the reservation record itself
        # carries no Multi-AZ signal (confirmed via boto3's service model -
        # no such field exists on ReservedCacheNode), so this is "N/A"
        # rather than guessed - same reasoning Azure Reservations use when
        # a purchase record doesn't carry a needed signal.
        scope_resource_type = "Amazon ElastiCache"
        scope_os = "N/A"
        scope_redundancy = "N/A"
    elif service == "Redshift":
        # Single engine, no product_description field exists on the
        # purchase record at all (confirmed via boto3's service model) -
        # matches this app's flat "Amazon Redshift" inventory resource_type.
        scope_resource_type = "Amazon Redshift"
        scope_os = "N/A"
        scope_redundancy = "N/A"
    else:
        return None   # unrecognized service - don't guess a category.

    return {
        "commitment_type":       "Reserved Instance",
        "scope_sku":             purchase.get("instance_type") or "N/A",
        "scope_resource_type":   scope_resource_type,
        "scope_region":          purchase.get("region") or "",
        "scope_os":              scope_os,
        "scope_redundancy":      scope_redundancy,
        "hourly_usd_commitment": rate,
        "reserved_qty":          purchase.get("instance_count") or 0,
        "term":                  term,
        "expiry_date":           None,   # AWS's response carries Start + Duration, not an explicit expiry timestamp - left for a future round to derive if the UI needs it.
        "is_inferred_mapping":   False,
        "mapping_note":          None,
    }


def derive_aws_savings_plan_commitment_fields(purchase: dict) -> Optional[dict]:
    """Maps one AWSSavingsPlanPurchase-shaped dict into a full
    Commitment-shaped dict. Returns None only for SageMaker (this app has
    no SageMaker inventory model at all, no coverage to match against) -
    see module docstring.

    Database Savings Plans are a REAL AWS product - confirmed via AWS's
    own FAQ (https://aws.amazon.com/savingsplans/faqs/) and, more
    authoritatively, the full AWS Savings Plans User Guide (checked
    2026-08-22): "Database Savings Plans provide flexibility to use AWS
    database services while reducing costs by up to 35% on Aurora, RDS,
    DynamoDB, ElastiCache, DocumentDB, Timestream, Neptune, Keyspaces,
    DMS, and Amazon OpenSearch Service" - a wider list than the FAQ page
    alone implied (the FAQ's shorter list omits Timestream/Neptune/
    Keyspaces/DMS/OpenSearch; see db/aws_seed.py's AWS_DATABASE_SP_TYPES
    for where this mattered in practice - OpenSearch has real tracked
    inventory in this app and was wrongly excluded in an earlier pass of
    this fix before the full guide was checked). Redshift is confirmed
    NOT covered by either source. The guide also confirms Database
    Savings Plans are 1-year term ONLY - striking parallel to this app's
    own existing Azure-side docstring (commitments/existing_commitments.py)
    already describing "Savings Plan for Databases: 1-year ONLY" -
    genuinely analogous products across providers, not just
    similarly-named ones.
    An earlier round of this app (before this fetch existed) used
    "EC2 Instance Savings Plan" as a placeholder in the "database" SP
    bucket, not knowing Database Savings Plans were real - corrected
    alongside this change (see commitments/existing_commitments.py):
    EC2 Instance Savings Plans are purely EC2/compute (never covered
    databases) and now bucket as Compute; "Database Savings Plan" is the
    new, actually-correct Database bucket entry."""
    sp_type = purchase.get("savings_plan_type")
    duration = purchase.get("term_duration_seconds") or 0
    term = _DURATION_SECONDS_TO_TERM.get(duration, "1-year")   # Savings Plans are always exactly 1yr or 3yr by AWS's own definition (Database SP is 1yr-only); default kept only as a last-resort guard, not expected to trigger.

    if sp_type == "Compute":
        commitment_type = "Compute Savings Plan"
        scope_sku = "Any Compute"
        scope_region = "Global"   # Compute Savings Plans apply regardless of instance family, size, OS, tenancy, or region - confirmed via AWS's own Savings Plans docs.
    elif sp_type == "EC2Instance":
        commitment_type = "EC2 Instance Savings Plan"
        family = purchase.get("ec2_instance_family") or "Unknown"
        scope_sku = f"{family} Family"
        scope_region = purchase.get("region") or "Unknown"   # EC2 Instance Savings Plans ARE region + instance-family locked (deeper discount than Compute SP in exchange for less flexibility) - confirmed via AWS's own Savings Plans docs.
    elif sp_type == "Database":
        commitment_type = "Database Savings Plan"
        scope_sku = "Any Database"   # covers Aurora/RDS/DynamoDB/ElastiCache/DocumentDB collectively per AWS's FAQ - no finer per-engine scope is exposed by the API, matching Azure's own "Any Database" convention for the same reason.
        scope_region = "Global"
    else:
        return None   # SageMaker - no inventory model in this app at all, see module docstring.

    return {
        "commitment_type":       commitment_type,
        "scope_sku":             scope_sku,
        "scope_resource_type":   None,
        "scope_region":          scope_region,
        "scope_os":              "N/A",
        "scope_redundancy":      "N/A",
        "hourly_usd_commitment": purchase.get("commitment_hourly_usd") or 0.0,
        "reserved_qty":          0,
        "term":                  term,
        "expiry_date":           (purchase.get("end") or "")[:10] or None,
        "is_inferred_mapping":   False,
        "mapping_note":          None,
    }
