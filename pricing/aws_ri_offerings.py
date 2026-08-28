"""
pricing/aws_ri_offerings.py
Real AWS Reserved Instance / Reserved Node purchase pricing, via each
service's own Describe*Offerings API - AWS's counterpart to
pricing/commitment_pricing.py's Azure Retail Prices RI lookup, and a
sibling to pricing/aws_price_list.py (same boto3 'per-service client,
own boto3.Session' pattern, same "not independently live-tested"
disclosure - see that module's own docstring).

Chosen over two alternatives, both ruled out:
  - ce:GetReservationPurchaseRecommendation (Cost Explorer) - a PAID
    call that returns a curated, usage-history-based recommendation
    list, not a queryable price catalog. Doesn't fit a per-SKU lookup.
  - The AWS Price List API's raw terms.Reserved JSON blob
    (pricing:GetProducts, same mechanism aws_price_list.py already uses
    for on-demand rates) - real, but needs careful JSON-blob parsing
    this session couldn't independently confirm the exact shape of via
    docs (repeated WebFetches of AWS's own "reading-an-offer.html" only
    ever rendered the unrelated Savings Plans section).

Each service's own Describe*Offerings API is free (not billed like
Cost Explorer), returns typed fields, and needs NO new IAM permission -
every action here lives under the same service:Describe* wildcard
already required by REQUIRED_AWS_POLICIES for inventory + existing
RI-purchase fetching (aws/connector.py::fetch_live_reservations).
Confirmed directly against the real, current AmazonRDSReadOnlyAccess
policy JSON ("rds:Describe*"); this app's own aws/connector.py comments
already assert the same wildcard fact for EC2/ElastiCache/Redshift.

Every field used below (FixedPrice/UsagePrice/Duration/RecurringCharges/
OfferingType/OfferingClass) is the IDENTICAL shape already read
successfully by fetch_live_reservations() for PURCHASED reservations -
confirmed live against this environment's own installed botocore
service models (boto3.client(svc).meta.service_model), not guessed:
  - Duration is returned as integer SECONDS on every service (31536000
    for 1yr, 94608000 for 3yr) - confirmed for all 6 services' output
    shapes.
  - RDS/ElastiCache/MemoryDB's Duration INPUT filter accepts "1" or "3"
    (years, as a string) per their own real parameter documentation
    ("Valid Values: 1 | 3 | 31536000 | 94608000") - confirmed via
    botocore's docstrings for RDS/ElastiCache; MemoryDB's carries the
    same "specified in years or seconds" doc wording so the same
    convention is assumed (not independently confirmed for MemoryDB
    specifically). Every returned offering's Duration is still
    re-validated client-side regardless, as a defensive safety net.
  - Redshift's/OpenSearch's Describe* operations take NO filter
    parameters at all (only pagination) - every offering is fetched
    and filtered client-side in Python, the same "fetch broadly, shape
    in Python" precedent fetch_live_reservations() already uses.
  - OfferingType's real casing is "No Upfront" (confirmed via EC2's own
    strict enum); OpenSearch's equivalent field is PaymentOption, real
    value "NO_UPFRONT" (confirmed via its own strict enum, same
    upper-snake-case convention already used for Cost Explorer's
    Savings Plans PaymentOption in aws/connector.py).

RDS's ProductDescription/ElastiCache's ProductDescription are
documented as a PARTIAL match ("results show offerings that partially
match the filter value" - RDS's own parameter doc), so a short engine
substring (matching this app's own existing _RDS_ENGINE_LABELS/
_ELASTICACHE_ENGINE_LABELS keys in aws/connector.py) is used rather
than a guessed exact display string.

Picks the cheapest matching "No Upfront" offering per term (mirrors
pricing/commitment_pricing.py::_fetch_azure_ri_rates picking min() over
candidates) - "No Upfront" chosen for the same reason the Savings Plans
feature used it: a pure hourly-equivalent rate, no separate upfront-
payment UI/amortization-period modeling needed.
"""

from typing import Optional

from aws.connector import AWSCredentials, _ELASTICACHE_ENGINE_LABELS

try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

_TERM_SECONDS = {"1yr": 31536000, "3yr": 94608000}


def _recurring_hourly(charges: list) -> Optional[float]:
    """Local copy of aws/connector.py::_recurring_hourly_charge - kept
    local rather than cross-importing a private (leading-underscore)
    helper, same locality precedent pricing/commitment_pricing.py's own
    MONTH_HOURS/_AZURE_TERM_LABELS constants already follow instead of
    importing from analysis/commitment_economics.py."""
    total = 0.0
    found = False
    for c in charges or []:
        if c.get("Frequency") == "Hourly" and c.get("Amount") is not None:
            total += float(c["Amount"])
            found = True
    return total if found else None


def _effective_hourly_rate(usage_price, fixed_price, duration_seconds, recurring_hourly) -> Optional[float]:
    """Local copy of pricing/aws_commitment_mapping.py::_effective_hourly_rate
    - amortized fixed_price (spread over the full term) + usage_price +
    any separate recurring hourly charge. Same formula, same locality
    reasoning as _recurring_hourly above."""
    if not duration_seconds:
        return None
    hours = duration_seconds / 3600.0
    amortized_fixed = (fixed_price or 0.0) / hours
    return amortized_fixed + (usage_price or 0.0) + (recurring_hourly or 0.0)


def _best_rate(offerings: list, term_years: str, fixed_key="FixedPrice", usage_key="UsagePrice",
                duration_key="Duration", recurring_key="RecurringCharges") -> Optional[float]:
    """Shared candidate-selection step, called AFTER service-specific
    filtering has already narrowed `offerings` to the right SKU/engine/
    tenancy - re-validates Duration client-side (defensive, see module
    docstring) and picks the cheapest matching offering."""
    target_seconds = _TERM_SECONDS[term_years]
    rates = []
    for o in offerings:
        try:
            if int(float(o.get(duration_key) or 0)) != target_seconds:
                continue
        except (TypeError, ValueError):
            continue
        rate = _effective_hourly_rate(
            o.get(usage_key), o.get(fixed_key), target_seconds, _recurring_hourly(o.get(recurring_key)),
        )
        if rate is not None:
            rates.append(rate)
    return min(rates) if rates else None


def _session(creds: AWSCredentials):
    return boto3.Session(aws_access_key_id=creds.access_key_id, aws_secret_access_key=creds.secret_access_key)


# EC2's Offerings ProductDescription enum only distinguishes Linux/UNIX
# vs Windows (confirmed via botocore's own strict enum on this field -
# no separate RHEL/SUSE value exists, unlike on-demand pricing's
# operatingSystem attribute) - a real AWS quirk, not a simplification
# this app is choosing.
_EC2_OS_TO_PRODUCT_DESCRIPTION = {"Windows": "Windows", "RHEL": "Linux/UNIX", "SUSE": "Linux/UNIX", "Linux": "Linux/UNIX"}


def fetch_ec2_ri_rates(creds: AWSCredentials, instance_type: str, region: str, os_: str) -> dict:
    """Returns {"1yr": float|None, "3yr": float|None}. OfferingClass
    fixed to "standard" (this app doesn't track EC2 OfferingClass choice
    anywhere else - Convertible RIs trade a lower discount for the
    ability to exchange instance families, out of scope for a
    per-resource $ estimate). Tenancy fixed to "default" (shared) -
    this app's inventory doesn't capture Dedicated Host/Instance tenancy."""
    result = {"1yr": None, "3yr": None}
    if not HAS_BOTO3 or not instance_type or not region:
        return result
    product_description = _EC2_OS_TO_PRODUCT_DESCRIPTION.get(os_, "Linux/UNIX")
    try:
        client = _session(creds).client("ec2", region_name=region)
        offerings = client.describe_reserved_instances_offerings(
            InstanceType=instance_type, ProductDescription=product_description,
            OfferingClass="standard", InstanceTenancy="default", IncludeMarketplace=False,
        ).get("ReservedInstancesOfferings", [])
    except (ClientError, BotoCoreError):
        return result
    offerings = [o for o in offerings if o.get("OfferingType") == "No Upfront" and o.get("Scope") == "Region"]
    for term in ("1yr", "3yr"):
        result[term] = _best_rate(offerings, term)
    return result


# Representative substring per label - relies on AWS's own documented
# "partial match" behavior for this filter (RDS's own parameter doc),
# not a guessed exact display string. SQL Server's several license-model
# engine keys all share one representative substring since this app
# doesn't split SQL Server by license model (out of scope - see
# analysis/engine.py's _RDS_FLEX_ELIGIBLE_TYPES, SQL Server is never
# size-flexibility-eligible regardless of license model, so there was no
# real reason to split it the way Oracle was). Oracle's two labels both
# query the same "oracle" substring - the license-model distinction is
# resolved AFTER the fetch instead, by checking each returned offering's
# own ProductDescription for the real "(li)" suffix (see
# fetch_rds_ri_rates below) - confirmed via AWS's own CLI docs example,
# "oracle-se2(li)" for License Included.
_RDS_LABEL_TO_QUERY = {
    "Amazon RDS for MySQL":                     "mysql",
    "Amazon RDS for PostgreSQL":                 "postgresql",
    "Amazon RDS for MariaDB":                    "mariadb",
    "Amazon RDS for Oracle (BYOL)":              "oracle",
    "Amazon RDS for Oracle (License Included)":  "oracle",
    "Amazon RDS for SQL Server":                 "sql server",
    "Amazon Aurora (MySQL)":                     "aurora mysql",
    "Amazon Aurora (PostgreSQL)":                "aurora postgresql",
}


def fetch_rds_ri_rates(creds: AWSCredentials, db_instance_class: str, region: str, resource_type: str, multi_az: bool) -> dict:
    """resource_type is this app's own label (e.g. "Amazon RDS for
    MySQL" - see aws/connector.py::map_rds_engine), mapped to a real
    ProductDescription query substring above. Not independently
    live-tested - verify the exact ProductDescription match once a real
    tenant with rds:Describe* runs a sync (see module docstring)."""
    result = {"1yr": None, "3yr": None}
    query = _RDS_LABEL_TO_QUERY.get(resource_type)
    if not HAS_BOTO3 or not db_instance_class or not region or not query:
        return result
    try:
        client = _session(creds).client("rds", region_name=region)
        offerings = []
        for term_str in ("1", "3"):
            offerings += client.describe_reserved_db_instances_offerings(
                DBInstanceClass=db_instance_class, ProductDescription=query,
                Duration=term_str, MultiAZ=bool(multi_az),
            ).get("ReservedDBInstancesOfferings", [])
        # Oracle only: the "oracle" substring query above matches BOTH
        # license models' offerings - narrow down to the one this
        # resource_type actually is, using the real "(li)" ProductDescription
        # suffix (see module comment above _RDS_LABEL_TO_QUERY).
        if resource_type == "Amazon RDS for Oracle (License Included)":
            offerings = [o for o in offerings if o.get("ProductDescription", "").endswith("(li)")]
        elif resource_type == "Amazon RDS for Oracle (BYOL)":
            offerings = [o for o in offerings if not o.get("ProductDescription", "").endswith("(li)")]
    except (ClientError, BotoCoreError):
        return result
    offerings = [o for o in offerings if o.get("OfferingType") == "No Upfront"]
    for term in ("1yr", "3yr"):
        result[term] = _best_rate(offerings, term)
    return result


def fetch_elasticache_ri_rates(creds: AWSCredentials, cache_node_type: str, region: str, resource_type: str) -> dict:
    """resource_type is this app's own per-engine label (see
    aws/connector.py::map_elasticache_engine) - reverse-mapped to the
    real "redis"/"memcached"/"valkey" ProductDescription value."""
    reverse = {v: k for k, v in _ELASTICACHE_ENGINE_LABELS.items()}
    engine = reverse.get(resource_type)
    result = {"1yr": None, "3yr": None}
    if not HAS_BOTO3 or not cache_node_type or not region or not engine:
        return result
    try:
        client = _session(creds).client("elasticache", region_name=region)
        offerings = []
        for term_str in ("1", "3"):
            offerings += client.describe_reserved_cache_nodes_offerings(
                CacheNodeType=cache_node_type, ProductDescription=engine, Duration=term_str,
            ).get("ReservedCacheNodesOfferings", [])
    except (ClientError, BotoCoreError):
        return result
    offerings = [o for o in offerings if o.get("OfferingType") == "No Upfront"]
    for term in ("1yr", "3yr"):
        result[term] = _best_rate(offerings, term)
    return result


def fetch_redshift_ri_rates(creds: AWSCredentials, node_type: str, region: str) -> dict:
    """No server-side filter exists on this API at all (confirmed via
    botocore's own service model - only pagination params) - fetches
    every offering in the region and filters client-side."""
    result = {"1yr": None, "3yr": None}
    if not HAS_BOTO3 or not node_type or not region:
        return result
    try:
        client = _session(creds).client("redshift", region_name=region)
        offerings, marker = [], None
        for _ in range(5):
            kwargs = {"Marker": marker} if marker else {}
            resp = client.describe_reserved_node_offerings(**kwargs)
            offerings += resp.get("ReservedNodeOfferings", [])
            marker = resp.get("Marker")
            if not marker:
                break
    except (ClientError, BotoCoreError):
        return result
    offerings = [o for o in offerings if o.get("NodeType") == node_type and o.get("OfferingType") == "No Upfront"]
    for term in ("1yr", "3yr"):
        result[term] = _best_rate(offerings, term)
    return result


def fetch_opensearch_ri_rates(creds: AWSCredentials, instance_type: str, region: str) -> dict:
    """No server-side filter exists on this API at all (confirmed via
    botocore's own service model - only pagination params) - fetches
    every offering in the region and filters client-side. PaymentOption
    (not OfferingType) is this service's real field name, real value
    "NO_UPFRONT" (confirmed via its own strict enum)."""
    result = {"1yr": None, "3yr": None}
    if not HAS_BOTO3 or not instance_type or not region:
        return result
    try:
        client = _session(creds).client("opensearch", region_name=region)
        offerings, next_token = [], None
        for _ in range(5):
            kwargs = {"NextToken": next_token} if next_token else {}
            resp = client.describe_reserved_instance_offerings(**kwargs)
            offerings += resp.get("ReservedInstanceOfferings", [])
            next_token = resp.get("NextToken")
            if not next_token:
                break
    except (ClientError, BotoCoreError):
        return result
    offerings = [o for o in offerings if o.get("InstanceType") == instance_type and o.get("PaymentOption") == "NO_UPFRONT"]
    for term in ("1yr", "3yr"):
        result[term] = _best_rate(offerings, term)
    return result


def fetch_memorydb_ri_rates(creds: AWSCredentials, node_type: str, region: str) -> dict:
    """No UsagePrice field on this API at all (confirmed via botocore's
    own service model, same as the purchased-reservation side already
    handles in aws/connector.py) - a plain dict.get("UsagePrice") on a
    response that never has that key already returns None, and
    _effective_hourly_rate already treats a missing usage_price as 0.0,
    so no special-casing is needed here."""
    result = {"1yr": None, "3yr": None}
    if not HAS_BOTO3 or not node_type or not region:
        return result
    try:
        client = _session(creds).client("memorydb", region_name=region)
        offerings = []
        for term_str in ("1", "3"):
            offerings += client.describe_reserved_nodes_offerings(
                NodeType=node_type, Duration=term_str,
            ).get("ReservedNodesOfferings", [])
    except (ClientError, BotoCoreError):
        return result
    offerings = [o for o in offerings if o.get("OfferingType") == "No Upfront"]
    for term in ("1yr", "3yr"):
        result[term] = _best_rate(offerings, term)
    return result
