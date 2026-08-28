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
"EC2 Instance Savings Plan", "Database Savings Plan",
"SageMaker Savings Plan") match exactly what
commitments/existing_commitments.py's bucketing expects. All 4 real AWS
savingsPlanType values (Compute/EC2Instance/Database/SageMaker) map to a
real Commitment row - SageMaker used to be deliberately dropped here (this
app had no SageMaker inventory model at the time), but that's no longer
true as of 2026-08-23 (see derive_aws_savings_plan_commitment_fields()'s
docstring below) and the stale drop was found and fixed the same day.
Database Savings Plans are similarly a real, not-dropped product -
confirmed as a real AWS product via AWS's own FAQ, see
derive_aws_savings_plan_commitment_fields()'s docstring below for the
full verification.
"""

from typing import Optional

from aws.connector import map_ec2_platform, map_rds_engine, map_elasticache_engine

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
        scope_resource_type = "Amazon EC2"   # renamed from "Compute" 2026-08-23 - must match aws/connector.py's live-fetch resource_type exactly, or real EC2 RI purchases would silently stop matching their inventory demand.
        scope_os = map_ec2_platform(purchase.get("product_description") or "")
        scope_redundancy = "N/A"
    elif service == "RDS":
        # Oracle's ProductDescription encodes license model as a real "(li)"
        # suffix (confirmed via AWS's own describe-reserved-db-instances-
        # offerings CLI docs example: "oracle-se2(li)" for License Included;
        # BYOL is the bare engine id with no suffix) - pre-parsed here into
        # the same "license-included" string map_rds_engine()'s inventory
        # call site already passes from DescribeDBInstances' own
        # LicenseModel field, so both sides resolve to the identical two
        # split resource_types ("Amazon RDS for Oracle (BYOL)" / "(License
        # Included)") and RI purchases correctly match their demand.
        product_description = purchase.get("product_description") or ""
        if product_description.endswith("(li)"):
            engine_id, license_model = product_description[:-4], "license-included"
        else:
            engine_id, license_model = product_description, None
        scope_resource_type = map_rds_engine(engine_id, license_model)
        scope_os = "N/A"
        scope_redundancy = "Zone Redundant" if purchase.get("multi_az") else "Locally Redundant"
    elif service == "ElastiCache":
        # Split by engine (Redis/Memcached/Valkey) as of 2026-08-23, matching
        # aws/connector.py's inventory taxonomy - previously kept as one flat
        # "Amazon ElastiCache" bucket, which made it impossible to check
        # Database Savings Plans' real Valkey-only restriction (confirmed
        # against the actual Database Savings Plans pricing table). RIs
        # remain purchasable and eligible for all three engines - this split
        # only makes the SP-side distinction checkable, it doesn't change RI
        # eligibility. product_description carries the real engine
        # ("redis"/"memcached"/"valkey", confirmed via a real "memcached"
        # example in AWS's own docs - same convention as DescribeCacheClusters's
        # Engine field), so no guessing is needed, just reading data that was
        # already on the purchase record and simply unused until now.
        # Redundancy: the reservation record carries no Multi-AZ signal at
        # all (confirmed via boto3's service model - no such field exists on
        # ReservedCacheNode), so this is "N/A" rather than guessed - same
        # reasoning Azure Reservations use when a purchase record doesn't
        # carry a needed signal.
        scope_resource_type = map_elasticache_engine(purchase.get("product_description") or "")
        scope_os = "N/A"
        scope_redundancy = "N/A"
    elif service == "MemoryDB":
        # Deliberately flat "Amazon MemoryDB" (NOT split by engine like
        # ElastiCache above) - confirmed via boto3's service model that
        # MemoryDB's ReservedNode purchase record carries no engine/product-
        # description field at all, unlike ElastiCache's ReservedCacheNode.
        # Splitting here while the purchase record can't specify engine
        # would create a scope_resource_type this fetch could never actually
        # produce for any real reservation, permanently breaking coverage
        # matching - see aws/connector.py's MemoryDB inventory block for the
        # matching, deliberately-flat resource_type on the demand side.
        scope_resource_type = "Amazon MemoryDB"
        scope_os = "N/A"
        scope_redundancy = "N/A"
    elif service == "Redshift":
        # Single engine, no product_description field exists on the
        # purchase record at all (confirmed via boto3's service model) -
        # matches this app's flat "Amazon Redshift" inventory resource_type.
        scope_resource_type = "Amazon Redshift"
        scope_os = "N/A"
        scope_redundancy = "N/A"
    elif service == "OpenSearch":
        # Single engine (like Redshift), no product_description or
        # Multi-AZ/scope/tenancy field on the purchase record (confirmed
        # via boto3's opensearch service model) - matches this app's flat
        # "Amazon OpenSearch" inventory resource_type. No per-instance
        # redundancy price differentiator either (see aws/connector.py's
        # fetch_live_inventory - real price list data confirms Zone
        # Awareness doesn't change the per-node rate).
        scope_resource_type = "Amazon OpenSearch"
        scope_os = "N/A"
        scope_redundancy = "N/A"
    else:
        return None   # unrecognized service - don't guess a category.

    # "Zonal" scope (EC2 only - confirmed via boto3's DescribeReservedInstances
    # Scope field: "Availability Zone" | "Region") is a real, tighter
    # restriction than Region: a Zonal RI only covers instances in that
    # specific AZ, not the whole region - added 2026-08-23, previously
    # ignored entirely (scope/availability_zone were captured on the
    # purchase record but never read here), which would have overstated
    # coverage for any Zonal RI against same-instance-type demand in a
    # DIFFERENT AZ of the same region. None for Regional-scope RIs (the more
    # common case) and for every non-EC2 service (RDS/ElastiCache/Redshift/
    # OpenSearch/MemoryDB Reservations have no AZ-scope concept at all,
    # confirmed via each service's own boto3 model) - matches
    # analysis/engine.py's _aws_scope_matches, which treats None as
    # unrestricted at that dimension.
    scope_availability_zone = purchase.get("availability_zone") if purchase.get("scope") == "Availability Zone" else None

    return {
        "commitment_type":       "Reserved Instance",
        "scope_sku":             purchase.get("instance_type") or "N/A",
        "scope_resource_type":   scope_resource_type,
        "scope_region":          purchase.get("region") or "",
        "scope_os":              scope_os,
        "scope_redundancy":      scope_redundancy,
        "scope_availability_zone": scope_availability_zone,
        # Real Account ID this purchase was fetched under (aws/connector.py's
        # sts:GetCallerIdentity) - traceability only, NOT used to restrict
        # matching. Unlike Azure (which defaults Reservations/Savings Plans
        # to Single-subscription scope unless Shared is deliberately
        # chosen), AWS shares unused RI/Savings Plan discount across an
        # Organization's linked accounts BY DEFAULT once consolidated
        # billing is active (confirmed via AWS's own Savings Plans user
        # guide) - this app has no organizations/ram API access to detect
        # the edge cases where that sharing is disabled for a specific
        # account or restricted via Group Sharing, so account-level scope
        # is deliberately left unrestricted rather than guessed at.
        "scope_subscription_id": None,
        "hourly_usd_commitment": rate,
        "reserved_qty":          purchase.get("instance_count") or 0,
        "term":                  term,
        "expiry_date":           None,   # AWS's response carries Start + Duration, not an explicit expiry timestamp - left for a future round to derive if the UI needs it.
        "is_inferred_mapping":   False,
        "mapping_note":          None,
        "offering_class":        purchase.get("offering_class"),   # EC2 only ("standard"/"convertible"); already None for RDS/ElastiCache/Redshift at the fetch layer (aws/connector.py) since those services have no such split.
    }


def derive_aws_savings_plan_commitment_fields(purchase: dict) -> Optional[dict]:
    """Maps one AWSSavingsPlanPurchase-shaped dict into a full
    Commitment-shaped dict. Returns None only for an unrecognized
    savings_plan_type value (defensive fallback - AWS's own enum is closed
    to exactly 4 values, so this should never actually trigger).

    SageMaker used to be dropped here deliberately ("this app has no
    SageMaker inventory model at all, no coverage to match against" - see
    the module docstring above, now stale) - that was true when this
    mapping function was written, but SageMaker Real-Time Inference
    Endpoints and Notebook Instances gained real live inventory tracking
    and their own "SageMaker Savings Plan" Commitment bucket later the same
    session (see aws/connector.py, db/aws_seed.py, commitments/
    existing_commitments.py's _AWS_SAGEMAKER_SP_TYPES, and app.py's Pool C
    UI section) - this mapping function was never updated to match, so a
    real live tenant's actual SageMaker Savings Plan purchase would have
    been silently dropped on sync, never reaching the Commitment table or
    Pool C's "Already Committed" figure at all. Found 2026-08-23 while
    checking this file for a different, unrelated question and fixed
    alongside it.

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
    elif sp_type == "SageMaker":
        commitment_type = "SageMaker Savings Plan"
        scope_sku = "Any SageMaker"   # matches db/aws_seed.py's demo commitment convention - SageMaker AI Savings Plans apply "regardless of instance family, size, Region, or component" per AWS's own SP pricing page, no finer scope to expose.
        scope_region = "Global"
    else:
        return None   # unrecognized savings_plan_type - AWS's own enum is closed to Compute/EC2Instance/Database/SageMaker, so this should never actually trigger; a defensive guard, not an expected path.

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
