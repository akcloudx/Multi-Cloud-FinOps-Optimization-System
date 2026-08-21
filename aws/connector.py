"""
aws/connector.py
AWS IAM authentication and live data connection module.

Handles:
  1. Credential validation (IAM Access Key & Secret Access Key)
  2. Live inventory fetch from AWS Resource Groups Tagging API / EC2 API
  3. Connection testing via STS GetCallerIdentity
  4. Required IAM Policies reference

REQUIRED AWS IAM POLICY PERMISSIONS:
  - ec2:DescribeInstances
  - ec2:DescribeReservedInstances
  - rds:DescribeDBInstances
  - rds:DescribeReservedDBInstances
  - savingsplans:DescribeSavingsPlans
  - pricing:GetProducts
  - ce:GetCostAndUsage
"""

import os
from typing import Optional
import pandas as pd

# Check if boto3 is available
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False


# AWS is genuinely inconsistent about which error code a service returns for
# an IAM permission denial - confirmed via official docs while researching
# this (see check_aws_permissions() below): EC2 uses "UnauthorizedOperation"
# (an older, EC2-specific convention), most other services use "AccessDenied",
# and some newer REST-based services use "AccessDeniedException". Checking
# against all three, rather than assuming one, is deliberate defensive coding
# given AWS itself doesn't apply this consistently.
_ACCESS_DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}


# AWS Managed Policy names verified against official AWS docs (each policy's
# own reference page + JSON document), not guessed - confirmed live 2026-08
# while the user tested this feature and asked for exact, non-confusing
# naming between "what Instructions says to attach" and "what the Test
# checklist reports".
#
# Reservations correction (2026-08-20): this list originally used
# ce:GetReservationUtilization for "read owned Reservations", which was
# wrong - confirmed via https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetReservationUtilization.html
# that it's a utilization-percentage/time-series metrics API, not a listing
# of owned reservations, and it's also a paid Cost Explorer API (see
# check_aws_permissions()'s docstring). The correct APIs for "what
# reservations do I own" are ec2:DescribeReservedInstances (confirmed via
# https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeReservedInstances.html)
# and rds:DescribeReservedDBInstances (confirmed via
# https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DescribeReservedDBInstances.html)
# - both free, both already covered by the same AmazonEC2ReadOnlyAccess /
# AmazonRDSReadOnlyAccess policies used for inventory (they're part of each
# service's own Describe* family), so no custom inline policy is needed
# after all.
REQUIRED_AWS_POLICIES = [
    {
        "Policy / Action": "ec2:DescribeInstances",
        "AWS Managed Policy": "AmazonEC2ReadOnlyAccess",
        "Required":        "Yes — Mandatory",
        "Purpose":         "Scan all EC2 instances across regions",
    },
    {
        "Policy / Action": "ec2:DescribeReservedInstances",
        "AWS Managed Policy": "AmazonEC2ReadOnlyAccess",
        "Required":        "Yes — For RI data",
        "Purpose":         "Read active EC2 Reserved Instances you own",
    },
    {
        "Policy / Action": "rds:DescribeDBInstances",
        "AWS Managed Policy": "AmazonRDSReadOnlyAccess",
        "Required":        "Yes — Mandatory",
        "Purpose":         "Scan RDS instances and database clusters",
    },
    {
        "Policy / Action": "rds:DescribeReservedDBInstances",
        "AWS Managed Policy": "AmazonRDSReadOnlyAccess",
        "Required":        "Yes — For RI data",
        "Purpose":         "Read active RDS Reserved Instances you own",
    },
    {
        "Policy / Action": "savingsplans:DescribeSavingsPlans",
        "AWS Managed Policy": "AWSSavingsPlansReadOnlyAccess",
        "Required":        "Yes — For SP data",
        "Purpose":         "Fetch active AWS Compute & EC2 Savings Plans",
    },
    {
        "Policy / Action": "pricing:GetProducts",
        "AWS Managed Policy": "AWSPriceListServiceFullAccess",
        "Required":        "Yes — For PAYG rates",
        "Purpose":         "Look up real On-Demand hourly rates for EC2/RDS inventory (free API despite the policy's name - Pricing has no mutating actions at all)",
    },
    {
        "Policy / Action": "ce:GetCostAndUsage",
        "AWS Managed Policy": "AWSBillingReadOnlyAccess",
        "Required":        "Yes — For Cost Explorer",
        "Purpose":         "Query AWS Cost Explorer for real historical spend and usage (not rates - see pricing:GetProducts for that)",
    },
]

# Fast lookup for the checklist renderer (app.py) - keeps the exact same
# managed-policy name in sync between the Instructions panel and the live
# Test Access Permissions result for the same action, so there's never a
# naming mismatch to cross-reference.
_MANAGED_POLICY_BY_ACTION = {p["Policy / Action"]: p["AWS Managed Policy"] for p in REQUIRED_AWS_POLICIES}


class AWSCredentials:
    """Holds AWS Access Key ID, Secret Access Key, and Region."""

    def __init__(
        self,
        access_key_id:     str,
        secret_access_key: str,
        region:            str = "us-east-1",
    ):
        self.access_key_id     = access_key_id.strip()
        self.secret_access_key = secret_access_key.strip()
        self.region            = region.strip() or "us-east-1"

    @property
    def is_complete(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)

    def to_env_dict(self) -> dict:
        return {
            "AWS_ACCESS_KEY_ID":     self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
            "AWS_DEFAULT_REGION":    self.region,
        }


def load_aws_credentials_from_env() -> Optional[AWSCredentials]:
    """Load AWS credentials from environment variables or .env file."""
    key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    sec = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    reg = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    if key and sec:
        return AWSCredentials(key, sec, reg)
    return None


def save_aws_credentials_to_env_file(creds: AWSCredentials, path: str = ".env") -> None:
    """Save AWS credentials to local .env file."""
    lines = []
    # Preserve existing file contents if possible
    existing = {}
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    existing[k] = v

    existing.update(creds.to_env_dict())
    out_lines = [f'{k}="{v}"\n' for k, v in existing.items()]
    with open(path, "w") as f:
        f.writelines(out_lines)


def check_aws_permissions(creds: AWSCredentials) -> dict:
    """
    Real, per-permission check against REQUIRED_AWS_POLICIES - the AWS
    equivalent of azure_conn.connector.check_role_assignments(), adapted for
    a fundamentally different permission model. Azure RBAC assigns named
    roles at a scope, so "what roles does this principal have" is one clean,
    commonly-granted API call. AWS has no equivalent: permissions come from
    policy JSON attached in various ways, and the one API built to answer
    "would this be allowed" (iam:SimulatePrincipalPolicy) itself requires a
    separate, unusual IAM permission that AWS does not include in standard
    ReadOnlyAccess-style policies and explicitly flags as security-sensitive
    (see https://docs.aws.amazon.com/IAM/latest/UserGuide/permissions-required_policy-simulator.html)
    - granting it just so this app could check its own permissions would add
    friction, not remove it.

    So this checks each REQUIRED_AWS_POLICIES action directly, with one
    deliberate exception:

    - ec2:DescribeInstances / ec2:DescribeReservedInstances - both checked
      via the real EC2 DryRun=True mechanism (AWS's own purpose-built "would
      this be allowed" flag for this service): a DryRunOperation response
      means allowed, UnauthorizedOperation means denied. No real call, no
      data pulled, no cost. Confirmed via boto3 docs that DescribeReservedInstances
      supports DryRun just like DescribeInstances.
    - rds:DescribeDBInstances / rds:DescribeReservedDBInstances /
      savingsplans:DescribeSavingsPlans / pricing:GetProducts - EC2's
      DryRun convention isn't universal (RDS/Savings Plans/Pricing don't
      support it), so these are checked with a real, minimal, genuinely
      free read-only call (MaxRecords=20 is RDS's own required minimum,
      not a chosen value; pricing:GetProducts is confirmed free via AWS's
      own launch announcement, unlike ce:GetCostAndUsage below).
    - ce:GetCostAndUsage - deliberately NOT probed live. AWS's own Cost
      Explorer docs are explicit: "Each paginated API request incurs a
      charge of $0.01" (https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html)
      - a permission CHECK should never itself cost real money. Reported as
      "unverified" here; the real status gets confirmed the first time an
      actual cost sync calls this for real data anyway (data/sync_pipeline.py,
      once AWS live fetch exists), so the $0.01 is only ever spent getting
      real data, never spent just to check a box. (ec2:DescribeReservedInstances /
      rds:DescribeReservedDBInstances used to also be a paid Cost Explorer
      call via the now-removed ce:GetReservationUtilization - see
      REQUIRED_AWS_POLICIES's comment above for that correction - so they no
      longer need this "unverified" treatment at all; they're free and
      checked live like everything else above.)

    Returns {"checked": bool, "error": str|None, "results": [{"action",
    "status" ("ready"|"missing"|"unverified"|"error"), "detail"}]}. "error"
    (the check itself couldn't run - bad credentials, network issue) is kept
    distinct from "missing" (checked fine, genuinely not granted) - collapsing
    those two into one bucket was a real bug on the Azure side (see
    azure_conn.connector.status_from_role_check's docstring for the "Missing
    None" incident) and isn't being repeated here.
    """
    if not HAS_BOTO3:
        return {"checked": False, "error": "boto3 library is not installed. Run: pip install boto3", "results": []}
    if not creds.is_complete:
        return {"checked": False, "error": "AWS Access Key ID and Secret Access Key are required.", "results": []}

    try:
        session = boto3.Session(
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            region_name=creds.region,
        )
    except Exception as e:
        return {"checked": False, "error": f"Could not create AWS session: {str(e)[:250]}", "results": []}

    results = []

    def _probe(action: str, call, *, dry_run_convention: bool = False):
        try:
            call()
            results.append({"action": action, "status": "ready", "detail": None})
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if dry_run_convention and code == "DryRunOperation":
                results.append({"action": action, "status": "ready", "detail": None})
            elif code in _ACCESS_DENIED_CODES:
                results.append({"action": action, "status": "missing", "detail": f"Access denied ({code})."})
            else:
                results.append({"action": action, "status": "error", "detail": str(e)[:250]})
        except Exception as e:
            results.append({"action": action, "status": "error", "detail": str(e)[:250]})

    _probe(
        "ec2:DescribeInstances",
        lambda: session.client("ec2").describe_instances(DryRun=True),
        dry_run_convention=True,
    )
    _probe(
        "ec2:DescribeReservedInstances",
        lambda: session.client("ec2").describe_reserved_instances(DryRun=True),
        dry_run_convention=True,
    )
    _probe(
        "rds:DescribeDBInstances",
        lambda: session.client("rds").describe_db_instances(MaxRecords=20),
    )
    _probe(
        "rds:DescribeReservedDBInstances",
        lambda: session.client("rds").describe_reserved_db_instances(MaxRecords=20),
    )
    _probe(
        "savingsplans:DescribeSavingsPlans",
        lambda: session.client("savingsplans").describe_savings_plans(maxResults=1),
    )
    # The Pricing service (unlike every other client above) only exists in
    # 3 regions - confirmed via botocore's own installed endpoints.json,
    # not a doc page: us-east-1, eu-central-1, ap-south-1. Pinned to
    # us-east-1 here regardless of creds.region/the tenant's own region -
    # this client always talks to that single endpoint no matter which
    # AWS region's prices are being looked up (that's controlled by a
    # regionCode filter in the request itself, not by which endpoint you
    # connect to - see pricing/aws_price_list.py). Free to call - confirmed
    # via AWS's own launch announcement ("available... at no charge").
    _probe(
        "pricing:GetProducts",
        lambda: session.client("pricing", region_name="us-east-1").get_products(ServiceCode="AmazonEC2", MaxResults=1),
    )

    for action in ["ce:GetCostAndUsage"]:
        results.append({
            "action": action,
            "status": "unverified",
            "detail": "Not checked separately - the Cost Explorer API charges $0.01 per request, "
                      "so this is confirmed the first time real cost data is synced, not by a "
                      "standalone check.",
        })

    # Attaches the exact same managed-policy name shown in the Instructions
    # panel to each result, so the live checklist and the setup instructions
    # never say two different things for the same action.
    for r in results:
        r["managed_policy"] = _MANAGED_POLICY_BY_ACTION.get(r["action"])

    return {"checked": True, "error": None, "results": results}


def test_aws_connection(creds: AWSCredentials) -> dict:
    """
    Test AWS connection using sts:GetCallerIdentity via boto3.
    """
    if not HAS_BOTO3:
        return {
            "success": False,
            "message": "boto3 library is not installed. Run: pip install boto3",
            "account_id": None,
            "error": "Missing boto3 package",
        }

    if not creds.is_complete:
        return {
            "success": False,
            "message": "AWS Access Key ID and Secret Access Key are required.",
            "account_id": None,
            "error": "Incomplete credentials",
        }

    try:
        session = boto3.Session(
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            region_name=creds.region,
        )
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        account_id = identity.get("Account")
        arn = identity.get("Arn")
        return {
            "success": True,
            "message": f"Connected to AWS Account ID: {account_id} ({arn})",
            "account_id": account_id,
            "error": None,
        }
    except Exception as e:
        err = str(e)
        return {
            "success": False,
            "message": f"AWS Connection failed: {err[:200]}",
            "account_id": None,
            "error": err,
        }


# ── Live Inventory Fetch (EC2 + RDS) ─────────────────────────────────────────
# AWS has no single "list every resource in my account" API the way Azure's
# Resource Graph is subscription-wide - ec2:DescribeInstances and
# rds:DescribeDBInstances are both REGION-scoped, confirmed via their own API
# references (https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeInstances.html,
# https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DescribeDBInstances.html).
# So getting full account-wide coverage means calling each once per enabled
# region - decided with the user (asked first, chose "all regions" over
# "just the tenant's one default region") specifically so a resource running
# outside the default region isn't silently invisible to the app, mirroring
# how Azure's fetch already covers an entire subscription, not one resource
# group. Both calls are free (see check_aws_permissions()'s docstring for the
# Cost Explorer contrast), so scanning every region costs nothing extra
# beyond a little latency.
def _discover_regions(session) -> list:
    """Regions enabled for this account only (not ALL AWS regions that
    exist) - confirmed via https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeRegions.html
    that omitting AllRegions defaults to enabled-only, which is what we want:
    scanning a disabled/not-opted-in region would just fail with an auth
    error, so there's no point including it."""
    ec2 = session.client("ec2")
    resp = ec2.describe_regions()
    return [r["RegionName"] for r in resp.get("Regions", [])]


def _map_ec2_state(state: str) -> str:
    """Collapses EC2's 6 lifecycle states into the exact two categorical
    values analysis/engine.py keys off of (`== "Running"` / `== "Stopped
    (deallocated)"`, checked literally, not a general "not running" test -
    see engine.py lines 89/373/393/449/537). "stopped" is the real EC2
    equivalent of Azure's "deallocated" state (confirmed via AWS's own EC2
    pricing docs: a stopped instance is not billed for compute) - same
    semantic, reusing the same label rather than inventing an AWS-specific
    one so the shared analysis engine treats it identically."""
    s = (state or "").lower()
    if s in ("stopped", "stopping", "shutting-down"):
        return "Stopped (deallocated)"
    return "Running"   # running, pending, or any future/unknown state - matches Azure's own fallback default.


def _map_rds_state(status: str) -> str:
    """Same two-bucket mapping as _map_ec2_state, for RDS's DBInstanceStatus.
    RDS's own docs confirm a "stopped" DB instance is not billed for compute
    (storage still bills, same nuance EC2 has) - genuinely equivalent to
    Azure's "deallocated" concept, not just a display label chosen for
    convenience."""
    s = (status or "").lower()
    if s == "stopped":
        return "Stopped (deallocated)"
    return "Running"


def map_ec2_platform(platform_details: str) -> str:
    """EC2's platformDetails is a free-text billing field (e.g. "Windows",
    "Windows with SQL Server Standard", "Linux/UNIX", "Red Hat Enterprise
    Linux", "SUSE Linux"). Collapsed to exactly the 4 values AWS's own
    On-Demand pricing distinguishes by price - confirmed against real
    downloaded price list data (pricing/aws_price_list.py): the
    `operatingSystem` attribute on EC2 price list entries only ever takes
    "Windows"/"RHEL"/"SUSE"/"Linux" (anything else - Ubuntu, Amazon Linux,
    Debian, plain "Linux/UNIX" - is priced under the generic "Linux"
    bucket). Originally this collapsed straight to "Windows"/"Linux" only,
    losing RHEL/SUSE - harmless while no AWS pricing engine existed, but
    would have silently mispriced every RHEL/SUSE instance as generic
    Linux once one did (RHEL/SUSE carry real licensing surcharges over
    base Linux, the same kind of real $ difference the Windows-surcharge
    bug on the Azure side turned out to be) - fixed as part of building
    that pricing engine, not left as a latent trap."""
    p = (platform_details or "").lower()
    if "windows" in p:
        return "Windows"
    if "red hat" in p or "rhel" in p:
        return "RHEL"
    if "suse" in p:
        return "SUSE"
    return "Linux"


_RDS_ENGINE_LABELS = {
    "mysql":              "Amazon RDS for MySQL",
    "postgres":           "Amazon RDS for PostgreSQL",
    "mariadb":            "Amazon RDS for MariaDB",
    "oracle-ee":          "Amazon RDS for Oracle",
    "oracle-ee-cdb":      "Amazon RDS for Oracle",
    "oracle-se2":         "Amazon RDS for Oracle",
    "oracle-se2-cdb":     "Amazon RDS for Oracle",
    "sqlserver-ee":       "Amazon RDS for SQL Server",
    "sqlserver-se":       "Amazon RDS for SQL Server",
    "sqlserver-ex":       "Amazon RDS for SQL Server",
    "sqlserver-web":      "Amazon RDS for SQL Server",
    "aurora-mysql":       "Amazon Aurora (MySQL)",
    "aurora-postgresql":  "Amazon Aurora (PostgreSQL)",
}


def map_rds_engine(engine: str) -> str:
    """RDS's `Engine` field (e.g. "mysql", "aurora-postgresql") - confirmed
    valid values via https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_CreateDBInstance.html's
    Engine parameter enum. Falls back to the raw engine string (rather than
    a generic "Database" bucket) for any engine not in the map, so an
    unrecognized/future engine is still visible and identifiable, not
    silently mislabeled."""
    return _RDS_ENGINE_LABELS.get((engine or "").lower(), engine or "Unknown")


def fetch_live_inventory(creds: AWSCredentials) -> pd.DataFrame:
    """
    Fetches live EC2 + RDS inventory across every AWS region enabled for
    this account. Returns a DataFrame in the exact same shape as
    azure_conn.connector.fetch_live_inventory() so both providers feed
    data/sync_pipeline.py identically.

    "PAYG Hourly Cost USD" is left at 0.0 for every row - there is no AWS
    pricing engine yet (unlike Azure's pricing/azure_retail_api.py real
    Retail Prices API sync), so this is an honest placeholder, not a
    computed value, until that's built as its own round.

    "Subscription" holds the real AWS Account ID (via sts:GetCallerIdentity,
    which requires no IAM permission at all for any authenticated principal -
    confirmed AWS-wide behavior, not gated by REQUIRED_AWS_POLICIES) since
    AWS has no "subscription" concept - this mirrors the same Account ID
    already tracked/displayed elsewhere for AWS tenants (db/schema.py's
    CloudTenant.aws_account_id).
    """
    if not HAS_BOTO3:
        raise ImportError("boto3 library is not installed. Run: pip install boto3")
    if not creds.is_complete:
        raise ValueError("AWS Access Key ID and Secret Access Key are required.")

    session = boto3.Session(
        aws_access_key_id=creds.access_key_id,
        aws_secret_access_key=creds.secret_access_key,
        region_name=creds.region,
    )

    account_id = session.client("sts").get_caller_identity().get("Account", "")
    regions = _discover_regions(session)

    records = []

    for region in regions:
        try:
            ec2 = session.client("ec2", region_name=region)
            paginator = ec2.get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        state = inst.get("State", {}).get("Name", "")
                        if state == "terminated":
                            continue   # gone, not a resource that still exists - matches how Resource Graph never returns deleted Azure resources either.
                        tags = {t.get("Key"): t.get("Value") for t in inst.get("Tags", [])}
                        instance_id = inst.get("InstanceId", "")
                        records.append({
                            "Resource ID":             instance_id,
                            "Resource Name":           tags.get("Name") or instance_id,
                            "Resource Type":           "Compute",
                            "Resource State":          _map_ec2_state(state),
                            "Region":                  region,
                            "OS":                      map_ec2_platform(inst.get("PlatformDetails", "")),
                            "SKU":                     inst.get("InstanceType", "N/A"),
                            "Redundancy":              "N/A",
                            "HA Replicas":             0,
                            "PAYG Hourly Cost USD":    0.0,
                            "Avg Daily Running Hours": 24,
                            "Subscription":            account_id,
                            "Provider":                "AWS",
                            "Is Orphaned":             False,
                        })
        except (ClientError, BotoCoreError):
            pass   # region not usable with these credentials (e.g. a just-enabled region still propagating) - skip it, don't fail the whole account-wide scan over one region.

        try:
            rds = session.client("rds", region_name=region)
            paginator = rds.get_paginator("describe_db_instances")
            for page in paginator.paginate():
                for db in page.get("DBInstances", []):
                    records.append({
                        "Resource ID":             db.get("DBInstanceArn") or db.get("DBInstanceIdentifier", ""),
                        "Resource Name":           db.get("DBInstanceIdentifier", ""),
                        "Resource Type":           map_rds_engine(db.get("Engine", "")),
                        "Resource State":          _map_rds_state(db.get("DBInstanceStatus", "")),
                        "Region":                  region,
                        "OS":                      "N/A",
                        "SKU":                     db.get("DBInstanceClass", "N/A"),
                        "Redundancy":              "Zone Redundant" if db.get("MultiAZ") else "Locally Redundant",
                        "HA Replicas":             0,
                        "PAYG Hourly Cost USD":    0.0,
                        "Avg Daily Running Hours": 24,
                        "Subscription":            account_id,
                        "Provider":                "AWS",
                        "Is Orphaned":             False,
                    })
        except (ClientError, BotoCoreError):
            pass

    return pd.DataFrame(records)


# ── Live Reservation / Savings Plan Fetch ────────────────────────────────────
# Facts confirmed this session (2026-08-22) via boto3's own service model
# and AWS's official API docs, not guessed:
#   - EC2 RIs (ec2:DescribeReservedInstances) and RDS RIs
#     (rds:DescribeReservedDBInstances) are genuinely region/AZ-scoped
#     purchases - like inventory, these need the same all-region scan, not
#     one call.
#   - Savings Plans (savingsplans:DescribeSavingsPlans) are the opposite:
#     confirmed via botocore's own endpoints.json that this service is
#     NOT regionalized ("isRegionalized": false, single "aws-global"
#     endpoint) - one call from any region returns every Savings Plan on
#     the account, no per-region loop needed.
#   - Unlike Azure Reservations (which carry no $ amount at all and need a
#     separate Retail Prices lookup - see azure_conn/connector.py), an AWS
#     Reserved Instance purchase record ALREADY carries UsagePrice and
#     FixedPrice - together with Duration these fully determine the real
#     effective hourly rate with no external pricing lookup at all
#     (pricing/aws_commitment_mapping.py does this arithmetic directly).
#   - Duration is raw seconds, confirmed exact valid values (not
#     calendar-based): 31536000 (1yr, defined by AWS as exactly 365 days)
#     or 94608000 (3yr, exactly 1095 days) - same constants across EC2 RIs,
#     RDS RIs, and Savings Plans (confirmed in AWS's own Savings Plans docs).
#   - RDS's ProductDescription uses the SAME lowercase engine-identifier
#     convention as DescribeDBInstances's Engine field (confirmed via a
#     real "mysql" example in AWS's own DescribeReservedDBInstances docs) -
#     so aws.connector.map_rds_engine() (already built for inventory) is
#     reused directly rather than a second, possibly-drifting mapping.
_RESERVATION_DURATION_SECONDS = {"1yr": 31536000, "3yr": 94608000}


def _recurring_hourly_charge(charges: list) -> Optional[float]:
    """Sums RecurringCharges[] entries where Frequency == "Hourly" - same
    list shape on both EC2 and RDS reservation responses (confirmed via
    boto3's service model). Returns None (not 0.0) when there are no
    recurring charges at all, so callers can tell "genuinely zero
    recurring cost" apart from "field wasn't populated" if that ever
    matters - in practice this just becomes 0.0 either way once combined
    with usage_price."""
    total = 0.0
    found = False
    for c in charges or []:
        if c.get("Frequency") == "Hourly" and c.get("Amount") is not None:
            total += float(c["Amount"])
            found = True
    return total if found else None


def fetch_live_reservations(creds: AWSCredentials) -> pd.DataFrame:
    """
    Fetches every active EC2 + RDS Reserved Instance across every AWS
    region enabled for this account. Returns a DataFrame shaped for
    AWSReservationPurchase (db/schema.py) - NOT the Azure-shaped
    ReservationPurchase table; see that class's docstring for why they're
    kept separate.
    """
    if not HAS_BOTO3:
        raise ImportError("boto3 library is not installed. Run: pip install boto3")
    if not creds.is_complete:
        raise ValueError("AWS Access Key ID and Secret Access Key are required.")

    session = boto3.Session(
        aws_access_key_id=creds.access_key_id,
        aws_secret_access_key=creds.secret_access_key,
        region_name=creds.region,
    )
    regions = _discover_regions(session)
    records = []

    for region in regions:
        try:
            ec2 = session.client("ec2", region_name=region)
            for ri in ec2.describe_reserved_instances().get("ReservedInstances", []):
                if ri.get("State") != "active":
                    continue   # expired/retired/pending purchases aren't real current coverage - matches how the inventory scan skips "terminated" instances.
                records.append({
                    "service":               "EC2",
                    "reserved_instance_id":  ri.get("ReservedInstancesId", ""),
                    "instance_type":         ri.get("InstanceType", ""),
                    "region":                region,
                    "availability_zone":     ri.get("AvailabilityZone"),
                    "product_description":   ri.get("ProductDescription"),
                    "instance_count":        ri.get("InstanceCount", 0),
                    "duration_seconds":      ri.get("Duration", 0),
                    "fixed_price":           ri.get("FixedPrice"),
                    "usage_price":           ri.get("UsagePrice"),
                    "currency_code":         ri.get("CurrencyCode"),
                    "offering_type":         ri.get("OfferingType"),
                    "offering_class":        ri.get("OfferingClass"),
                    "instance_tenancy":      ri.get("InstanceTenancy"),
                    "scope":                 ri.get("Scope"),
                    "multi_az":              None,
                    "state":                 ri.get("State", ""),
                    "start_time":            str(ri.get("Start")) if ri.get("Start") else None,
                    "recurring_charge_hourly": _recurring_hourly_charge(ri.get("RecurringCharges")),
                })
        except (ClientError, BotoCoreError):
            pass

        try:
            rds = session.client("rds", region_name=region)
            for ri in rds.describe_reserved_db_instances().get("ReservedDBInstances", []):
                if ri.get("State") != "active":
                    continue
                records.append({
                    "service":               "RDS",
                    "reserved_instance_id":  ri.get("ReservedDBInstanceId", ""),
                    "instance_type":         ri.get("DBInstanceClass", ""),
                    "region":                region,
                    "availability_zone":     None,
                    "product_description":   ri.get("ProductDescription"),
                    "instance_count":        ri.get("DBInstanceCount", 0),
                    "duration_seconds":      ri.get("Duration", 0),
                    "fixed_price":           ri.get("FixedPrice"),
                    "usage_price":           ri.get("UsagePrice"),
                    "currency_code":         ri.get("CurrencyCode"),
                    "offering_type":         ri.get("OfferingType"),
                    "offering_class":        None,
                    "instance_tenancy":      None,
                    "scope":                 None,
                    "multi_az":              ri.get("MultiAZ"),
                    "state":                 ri.get("State", ""),
                    "start_time":            str(ri.get("StartTime")) if ri.get("StartTime") else None,
                    "recurring_charge_hourly": _recurring_hourly_charge(ri.get("RecurringCharges")),
                })
        except (ClientError, BotoCoreError):
            pass

    return pd.DataFrame(records)


def fetch_live_savings_plans(creds: AWSCredentials) -> pd.DataFrame:
    """
    Fetches every active Savings Plan on this account - a single call, not
    an all-region scan, since the Savings Plans service is account-wide
    (confirmed via botocore's own endpoints.json - see module comment
    above). Returns a DataFrame shaped for AWSSavingsPlanPurchase
    (db/schema.py).
    """
    if not HAS_BOTO3:
        raise ImportError("boto3 library is not installed. Run: pip install boto3")
    if not creds.is_complete:
        raise ValueError("AWS Access Key ID and Secret Access Key are required.")

    session = boto3.Session(
        aws_access_key_id=creds.access_key_id,
        aws_secret_access_key=creds.secret_access_key,
        region_name=creds.region,
    )
    records = []
    try:
        sp_client = session.client("savingsplans", region_name="us-east-1")
        for sp in sp_client.describe_savings_plans().get("savingsPlans", []):
            if sp.get("state") != "active":
                continue
            records.append({
                "savings_plan_id":         sp.get("savingsPlanId", ""),
                "savings_plan_arn":        sp.get("savingsPlanArn"),
                "description":             sp.get("description"),
                "start":                   sp.get("start"),
                "end":                     sp.get("end"),
                "state":                   sp.get("state", ""),
                "region":                  sp.get("region"),
                "ec2_instance_family":     sp.get("ec2InstanceFamily"),
                "savings_plan_type":       sp.get("savingsPlanType", ""),
                "payment_option":          sp.get("paymentOption", ""),
                "product_types":           ",".join(sp.get("productTypes", []) or []),
                "currency":                sp.get("currency"),
                "commitment_hourly_usd":   float(sp["commitment"]) if sp.get("commitment") is not None else 0.0,
                "upfront_payment_amount":  float(sp["upfrontPaymentAmount"]) if sp.get("upfrontPaymentAmount") is not None else None,
                "recurring_payment_amount": float(sp["recurringPaymentAmount"]) if sp.get("recurringPaymentAmount") is not None else None,
                "term_duration_seconds":   sp.get("termDurationInSeconds", 0),
            })
    except (ClientError, BotoCoreError):
        pass

    return pd.DataFrame(records)
