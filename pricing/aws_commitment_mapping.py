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
"EC2 Instance Savings Plan") are NOT arbitrary - they match exactly what
commitments/existing_commitments.py already expects (that bucketing logic
predates this module, written in anticipation of this exact naming).
"SageMaker"/"Database" Savings Plan purchases are still stored in the raw
AWSSavingsPlanPurchase table (for completeness) but deliberately produce NO
Commitment row - this app has no SageMaker inventory model at all, and
"Database" Savings Plans aren't a category commitments/existing_commitments.py
or analysis/engine.py's coverage matching understands; fabricating a
category for them would be worse than omitting the row, same "don't
fabricate what can't be priced/modeled" discipline the Azure mapping
module already follows for unmodeled reserved_resource_type values.
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
    Commitment-shaped dict. Returns None for savingsPlanType values this
    app's UI has no bucket for (SageMaker, Database) - see module
    docstring for why that's a deliberate omission, not a gap to silently
    guess around."""
    sp_type = purchase.get("savings_plan_type")
    duration = purchase.get("term_duration_seconds") or 0
    term = _DURATION_SECONDS_TO_TERM.get(duration, "1-year")   # Savings Plans are always exactly 1yr or 3yr by AWS's own definition; default kept only as a last-resort guard, not expected to trigger.

    if sp_type == "Compute":
        commitment_type = "Compute Savings Plan"
        scope_sku = "Any Compute"
        scope_region = "Global"   # Compute Savings Plans apply regardless of instance family, size, OS, tenancy, or region - confirmed via AWS's own Savings Plans docs.
    elif sp_type == "EC2Instance":
        commitment_type = "EC2 Instance Savings Plan"
        family = purchase.get("ec2_instance_family") or "Unknown"
        scope_sku = f"{family} Family"
        scope_region = purchase.get("region") or "Unknown"   # EC2 Instance Savings Plans ARE region + instance-family locked (deeper discount than Compute SP in exchange for less flexibility) - confirmed via AWS's own Savings Plans docs.
    else:
        return None   # SageMaker / Database - no bucket in this app's UI, see module docstring.

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
