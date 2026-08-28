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
  - elasticache:DescribeReservedCacheNodes
  - redshift:DescribeReservedNodes
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
        "Policy / Action": "elasticache:DescribeCacheClusters / elasticache:DescribeReservedCacheNodes",
        "AWS Managed Policy": "AmazonElastiCacheReadOnlyAccess",
        "Required":        "Yes — For inventory + RI data",
        "Purpose":         "Scan ElastiCache clusters and read active Reserved Nodes you own (one elasticache:Describe* wildcard covers both - confirmed via the policy's own JSON; inventory scanning added 2026-08-23, previously only RI data was fetched)",
    },
    {
        "Policy / Action": "redshift:DescribeClusters / redshift:DescribeReservedNodes",
        "AWS Managed Policy": "AmazonRedshiftReadOnlyAccess",
        "Required":        "Yes — For inventory + RI data",
        "Purpose":         "Scan Redshift clusters and read active Reserved Nodes you own (one redshift:Describe* wildcard covers both; inventory scanning added 2026-08-23, previously only RI data was fetched)",
    },
    {
        "Policy / Action": "memorydb:DescribeClusters / memorydb:DescribeReservedNodes",
        "AWS Managed Policy": "AmazonMemoryDBReadOnlyAccess",
        "Required":        "Yes — For inventory + RI data",
        "Purpose":         "Scan MemoryDB clusters and read active Reserved Nodes you own (one memorydb:Describe* wildcard covers both, same pattern as ElastiCache/Redshift above)",
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
    {
        "Policy / Action": "ce:GetSavingsPlansPurchaseRecommendation",
        "AWS Managed Policy": "(no dedicated managed policy - attach a custom inline policy, or the broad ReadOnlyAccess policy)",
        "Required":        "Yes — For real-time Savings Plan pricing",
        "Purpose":         "Get AWS's own real discount %/savings estimate for Compute, SageMaker, and Database Savings Plans, based on the account's actual usage history - confirmed not covered by AWSBillingReadOnlyAccess or AWSSavingsPlansReadOnlyAccess (checked both policies' live JSON, 2026-08-28)",
    },
    # 2026-08-22 additions - closing the Database/Compute Savings Plan
    # coverage gap: DocumentDB/Neptune deliberately have NO entry here at
    # all - confirmed via AmazonDocDBReadOnlyAccess's own policy JSON
    # ("this policy also grants access to Amazon RDS and Amazon Neptune
    # resources") that both use plain rds:DescribeDBInstances under the
    # hood despite having their own boto3 clients/API endpoints - already
    # covered by the AmazonRDSReadOnlyAccess requirement above, so adding a
    # second checklist row for the same real IAM action would be redundant
    # and misleadingly imply a second permission is needed.
    {
        "Policy / Action": "dynamodb:ListTables / dynamodb:DescribeTable",
        "AWS Managed Policy": "AmazonDynamoDBReadOnlyAccess",
        "Required":        "Yes — For DynamoDB inventory",
        "Purpose":         "Scan provisioned-capacity DynamoDB tables (on-demand/pay-per-request tables have no hourly rate to track, see aws/connector.py)",
    },
    {
        "Policy / Action": "cassandra:Select",
        "AWS Managed Policy": "AmazonKeyspacesReadOnlyAccess",
        "Required":        "Yes — For Keyspaces inventory",
        "Purpose":         "List keyspaces/tables (Keyspaces' own IAM actions use the historical cassandra: prefix, not keyspaces: - confirmed via AWS's Service Authorization Reference)",
    },
    {
        "Policy / Action": "dms:DescribeReplicationInstances / dms:DescribeReplicationConfigs / dms:DescribeReplications",
        "AWS Managed Policy": "(no dedicated managed policy - attach a custom inline policy, or the broad ReadOnlyAccess policy)",
        "Required":        "Yes — For DMS inventory",
        "Purpose":         "Scan DMS replication instances and DMS Serverless replications (added 2026-08-23) - confirmed no AWS-managed read-only policy exists specifically for DMS user access (its managed policies are all internal service-linked-role policies)",
    },
    {
        "Policy / Action": "ecs:ListClusters / ecs:ListTasks / ecs:DescribeTasks",
        "AWS Managed Policy": "(no dedicated managed policy - attach a custom inline policy, or the broad ReadOnlyAccess policy)",
        "Required":        "Yes — For Fargate inventory",
        "Purpose":         "Scan running Fargate tasks for Compute Savings Plan coverage - confirmed no AWS-managed read-only policy exists specifically for ECS user access either",
    },
    # OpenSearch was already demo-tracked and listed as RI-eligible (see
    # db/aws_seed.py's AWS_RI_COVERAGE_NOTES) but had NO live fetch at all
    # until now - not even inventory. Real IAM action is "es:Describe*" -
    # yet another historical-naming holdover (OpenSearch Service was
    # renamed from "Elasticsearch Service", but its IAM action prefix and
    # boto3's legacy "es" client both still use the old name; the newer
    # "opensearch" boto3 client is a thin wrapper over the identical API,
    # confirmed via boto3's own service model comparison). One policy
    # covers both inventory (DescribeDomains) and Reserved Instances
    # (DescribeReservedInstances) - same "Describe* wildcard already covers
    # RI too" pattern as EC2/RDS above.
    {
        "Policy / Action": "es:DescribeDomains / es:DescribeReservedInstances",
        "AWS Managed Policy": "AmazonOpenSearchServiceReadOnlyAccess",
        "Required":        "Yes — For OpenSearch inventory + RI data",
        "Purpose":         "Scan OpenSearch domains and read active Reserved Instances you own (real IAM action is es:Describe* despite the newer 'opensearch' boto3 client name)",
    },
    # Added 2026-08-23 alongside SageMaker Endpoint/Notebook Instance
    # inventory - confirmed real (arn:aws:iam::aws:policy/AmazonSageMakerReadOnly),
    # its sagemaker:Describe*/List* wildcard already covers ListEndpoints/
    # DescribeEndpoint/DescribeEndpointConfig/ListNotebookInstances, no
    # separate policy needed for each call. Savings Plan data itself needs
    # no new permission - the existing savingsplans:DescribeSavingsPlans
    # entry above already returns SageMaker-type plans too (one API, filtered
    # by the savingsPlanType field, not a separate call per type).
    {
        "Policy / Action": "sagemaker:ListEndpoints / sagemaker:DescribeEndpoint / sagemaker:DescribeEndpointConfig / sagemaker:ListNotebookInstances",
        "AWS Managed Policy": "AmazonSageMakerReadOnly",
        "Required":        "Yes — For SageMaker inventory",
        "Purpose":         "Scan Real-Time Inference Endpoints and Notebook Instances for SageMaker Savings Plan coverage - Training/Processing/Batch Transform jobs are not scanned (ephemeral, no persistent resource identity to track)",
    },
    # Added 2026-08-23 alongside Neptune Analytics inventory - a genuinely
    # separate product/client ("neptune-graph") from classic Neptune above,
    # confirmed real (arn:aws:iam::aws:policy/NeptuneGraphReadOnlyAccess).
    {
        "Policy / Action": "neptune-graph:ListGraphs / neptune-graph:GetGraph",
        "AWS Managed Policy": "NeptuneGraphReadOnlyAccess",
        "Required":        "Yes — For Neptune Analytics inventory",
        "Purpose":         "Scan Neptune Analytics graphs (a separate product from Neptune Database) for Database Savings Plan coverage",
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
      elasticache:DescribeReservedCacheNodes / redshift:DescribeReservedNodes /
      savingsplans:DescribeSavingsPlans / pricing:GetProducts - EC2's
      DryRun convention isn't universal (RDS/ElastiCache/Redshift/Savings
      Plans/Pricing don't support it), so these are checked with a real,
      minimal, genuinely free read-only call (MaxRecords=20 is RDS's own
      required minimum, confirmed as the same minimum for ElastiCache and
      Redshift too, not a chosen value; pricing:GetProducts is confirmed
      free via AWS's own launch announcement, unlike ce:GetCostAndUsage
      below).
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
        "elasticache:DescribeCacheClusters / elasticache:DescribeReservedCacheNodes",
        lambda: session.client("elasticache").describe_reserved_cache_nodes(MaxRecords=20),
    )
    _probe(
        "redshift:DescribeClusters / redshift:DescribeReservedNodes",
        lambda: session.client("redshift").describe_reserved_nodes(MaxRecords=20),
    )
    _probe(
        "memorydb:DescribeClusters / memorydb:DescribeReservedNodes",
        lambda: session.client("memorydb").describe_reserved_nodes(MaxResults=20),
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
    _probe(
        "dynamodb:ListTables / dynamodb:DescribeTable",
        lambda: session.client("dynamodb").list_tables(Limit=20),
    )
    _probe(
        "cassandra:Select",
        lambda: session.client("keyspaces").list_keyspaces(maxResults=20),
    )
    _probe(
        "dms:DescribeReplicationInstances",
        lambda: session.client("dms").describe_replication_instances(MaxRecords=20),
    )
    _probe(
        "ecs:ListClusters / ecs:ListTasks / ecs:DescribeTasks",
        lambda: session.client("ecs").list_clusters(maxResults=20),
    )
    _probe(
        "es:DescribeDomains / es:DescribeReservedInstances",
        lambda: session.client("opensearch").list_domain_names(),
    )
    _probe(
        "sagemaker:ListEndpoints / sagemaker:DescribeEndpoint / sagemaker:DescribeEndpointConfig / sagemaker:ListNotebookInstances",
        lambda: session.client("sagemaker").list_endpoints(MaxResults=20),
    )
    _probe(
        "neptune-graph:ListGraphs / neptune-graph:GetGraph",
        lambda: session.client("neptune-graph").list_graphs(maxResults=20),
    )

    for action in ["ce:GetCostAndUsage", "ce:GetSavingsPlansPurchaseRecommendation"]:
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
    """Collapses EC2's 6 lifecycle states into the two categorical values
    the rest of the app keys off of. Real bug fixed 2026-08-25: this used
    to return the literal string "Stopped (deallocated)" - Azure's own VM
    terminology, reused here on the (wrong) assumption that
    analysis/engine.py checked for that exact string - it actually only
    ever checks `!= "Running"`, so the AWS-inappropriate wording was purely
    cosmetic, but real (showed up as-is in the UI for every stopped AWS
    resource type). "Stopped" is EC2's own real terminology (confirmed via
    AWS's own EC2 pricing docs: a stopped instance is not billed for
    compute, same semantic as Azure's "deallocated", just AWS's own word
    for it)."""
    s = (state or "").lower()
    if s in ("stopped", "stopping", "shutting-down"):
        return "Stopped"
    return "Running"   # running, pending, or any future/unknown state - matches Azure's own fallback default.


def _map_rds_state(status: str) -> str:
    """Same two-bucket mapping as _map_ec2_state, for RDS's DBInstanceStatus.
    RDS's own docs confirm a "stopped" DB instance is not billed for compute
    (storage still bills, same nuance EC2 has) - genuinely equivalent to
    Azure's "deallocated" concept, just AWS's own real wording for it."""
    s = (status or "").lower()
    if s == "stopped":
        return "Stopped"
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


def map_rds_engine(engine: str, license_model: str = None) -> str:
    """RDS's `Engine` field (e.g. "mysql", "aurora-postgresql") - confirmed
    valid values via https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_CreateDBInstance.html's
    Engine parameter enum. Falls back to the raw engine string (rather than
    a generic "Database" bucket) for any engine not in the map, so an
    unrecognized/future engine is still visible and identifiable, not
    silently mislabeled.

    license_model splits Oracle only (added 2026-08-29) into "Amazon RDS
    for Oracle (BYOL)" / "(License Included)" - a real product distinction
    with different Reserved Instance size-flexibility eligibility (BYOL:
    flexible, same as MySQL/PostgreSQL/MariaDB; License Included: NOT
    flexible, same as SQL Server - confirmed via AWS's own Oracle
    licensing docs, see analysis/engine.py's _RDS_FLEX_ELIGIBLE_TYPES).
    Every other engine's `_RDS_ENGINE_LABELS` value already reflects the
    real product split without needing this parameter (SQL Server has no
    BYOL option to disambiguate; MySQL/MariaDB/PostgreSQL's own
    LicenseModel value is a fixed, non-choice constant per engine, not a
    real product variant to split on).

    Callers pass real RDS values: inventory rows pass DescribeDBInstances'
    own `LicenseModel` field directly ("bring-your-own-license" /
    "license-included"); reservation rows have no such field, so the
    caller (pricing/aws_commitment_mapping.py) pre-parses the real
    ProductDescription "(li)" suffix (confirmed via AWS's own CLI docs
    example, "oracle-se2(li)") into the same "license-included" string
    before calling this. Defaults to BYOL when not supplied - the safer
    direction for this app's purposes, since every real call site for
    Oracle rows DOES supply it (see both call sites)."""
    base = _RDS_ENGINE_LABELS.get((engine or "").lower(), engine or "Unknown")
    if base == "Amazon RDS for Oracle":
        if (license_model or "").strip().lower() in ("license-included", "license included"):
            return "Amazon RDS for Oracle (License Included)"
        return "Amazon RDS for Oracle (BYOL)"
    return base


_ELASTICACHE_ENGINE_LABELS = {
    "redis":     "Amazon ElastiCache for Redis",
    "memcached": "Amazon ElastiCache for Memcached",
    "valkey":    "Amazon ElastiCache for Valkey",
}


def map_elasticache_engine(engine: str) -> str:
    """ElastiCache's `Engine` field (DescribeCacheClusters) - confirmed
    valid values "redis"/"memcached"/"valkey" via boto3's service model and
    a real "memcached" example in AWS's own docs. Split by engine (matching
    the real AWS product names, and mirroring map_rds_engine()'s per-engine
    convention) rather than kept as one flat "Amazon ElastiCache" bucket -
    added 2026-08-23 after confirming Database Savings Plans only cover
    ElastiCache for Valkey specifically (verified against the actual
    Database Savings Plans pricing table, which lists "ElastiCache for
    Valkey Instances"/"...Serverless" and nothing for Redis or Memcached) -
    a flat resource_type made that real distinction impossible to check.
    Reserved Instances remain available for all three engines (unlike
    Savings Plans) - this split doesn't change RI eligibility, only makes
    the SP-side restriction checkable. Falls back to the raw engine string
    for any future/unrecognized engine, same discipline as map_rds_engine."""
    return _ELASTICACHE_ENGINE_LABELS.get((engine or "").lower(), engine or "Unknown")


_MEMORYDB_ENGINE_LABELS = {
    "redis":  "Amazon MemoryDB for Redis",
    "valkey": "Amazon MemoryDB for Valkey",
}


def map_memorydb_engine(engine: str) -> str:
    """MemoryDB's `Engine` field (DescribeClusters) - confirmed valid values
    "redis"/"valkey" via real downloaded AmazonMemoryDB price list data
    (MemoryDB has no Memcached-compatible option at all, unlike ElastiCache -
    confirmed the same way: no "Memcached" cacheEngine/engine value appears
    anywhere in its price list). Split by engine for the same reason as
    map_elasticache_engine() - real downloaded price list data confirms
    MemoryDB's per-node rate genuinely differs by engine at the identical
    instanceType (e.g. db.r6g.large: $0.309/hr Redis vs $0.2163/hr Valkey in
    us-east-1) - a flat resource_type would silently blend two different
    real prices into one ambiguous pricing-cache key."""
    return _MEMORYDB_ENGINE_LABELS.get((engine or "").lower(), engine or "Unknown")


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
                            # Renamed from the generic "Compute" to "Amazon EC2"
                            # 2026-08-23 - "Compute" was ALSO Azure's own
                            # resource_type for VMs, and both providers'
                            # resource_type strings share one flat namespace
                            # everywhere downstream (analysis/ri_eligibility.py's
                            # _RULES dict, app.py's Inventory tab filters,
                            # pricing/aws_price_list.py's dispatch, etc.) -
                            # EC2 was the one accidental collision (every
                            # other AWS type already has a distinct "Amazon
                            # .../AWS ..." name), silently routing EC2 rows
                            # through Azure's VM-family eligibility regex
                            # (harmless by coincidence - no EC2 SKU ever
                            # matches the Azure Basic_/Standard_ pattern - but
                            # confusing and fragile). See db/aws_seed.py,
                            # analysis/ri_eligibility.py, app.py,
                            # pricing/aws_price_list.py, pricing/
                            # aws_commitment_mapping.py, and analysis/
                            # focus_mapping.py for the matching updates.
                            "Resource Type":           "Amazon EC2",
                            "Resource State":          _map_ec2_state(state),
                            "Region":                  region,
                            "OS":                      map_ec2_platform(inst.get("PlatformDetails", "")),
                            "SKU":                     inst.get("InstanceType", "N/A"),
                            "Redundancy":              "N/A",
                            "HA Replicas":             0,
                            "PAYG Hourly Cost USD":    0.0,
                            "Avg Daily Running Hours": 24,
                            "Subscription":            account_id,
                            # Real Placement.AvailabilityZone (e.g.
                            # "us-east-1a", confirmed via boto3's
                            # DescribeInstances model - always present for a
                            # non-terminated instance). Added 2026-08-23 to
                            # match a "Zonal" EC2 Reserved Instance (scope=
                            # "Availability Zone") against the specific
                            # instances it actually covers - see
                            # Commitment.scope_availability_zone.
                            "Availability Zone":      inst.get("Placement", {}).get("AvailabilityZone", ""),
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
                        "Resource Type":           map_rds_engine(db.get("Engine", ""), db.get("LicenseModel")),
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

        # DocumentDB and Neptune - same DescribeDBInstances shape as RDS
        # (DBInstanceClass/DBInstanceStatus/AvailabilityZone), confirmed via
        # boto3's real service model 2026-08-22 - both share RDS's
        # underlying control plane so closely that their own official
        # read-only IAM policies grant plain rds:DescribeDBInstances, not a
        # docdb:/neptune:-prefixed action (confirmed via
        # AmazonDocDBReadOnlyAccess's own policy JSON) - no new IAM
        # permission needed beyond what RDS inventory already requires.
        # Neither service has a Reserved Instance API at all (confirmed: no
        # Describe*Reserved* operation exists on either boto3 client), so
        # they're Database-Savings-Plan-eligible only, never RI-eligible -
        # correctly absent from AWS_RI_COVERAGE_NOTES.
        try:
            docdb = session.client("docdb", region_name=region)

            # DocumentDB Serverless - added 2026-08-23 after confirming
            # feasibility. Serverless clusters bill per DCU-hour (a
            # continuously auto-scaling capacity metric, not a fixed
            # instance class), confirmed real Database-Savings-Plan-eligible
            # via AWS's own SP announcement. The ACTUAL live DCU usage is
            # only exposed as a CloudWatch time-series metric, not a
            # describable static value - but describe_db_clusters()'s real
            # ServerlessV2ScalingConfiguration.MinCapacity field (confirmed
            # via botocore's own installed docdb service model) IS a static,
            # describable floor, and AWS's own SP guidance explicitly
            # recommends committing against exactly that floor ("identify
            # the minimum hourly DCU spend your cluster maintains
            # consistently and commit at that level"). So MinCapacity is
            # used as this resource's steady-state baseline - the same
            # "conservative floor, not live average" principle this app's
            # own safety-buffer already applies everywhere else - rather
            # than guessing at real-time usage this app has no way to fetch.
            # A cluster only has this field populated if it's genuinely
            # Serverless (confirmed via the field's own documentation -
            # provisioned clusters simply omit it).
            serverless_min_capacity = {}
            try:
                for page in docdb.get_paginator("describe_db_clusters").paginate():
                    for cluster in page.get("DBClusters", []):
                        scaling = cluster.get("ServerlessV2ScalingConfiguration")
                        if scaling and scaling.get("MinCapacity") is not None:
                            serverless_min_capacity[cluster.get("DBClusterIdentifier", "")] = scaling["MinCapacity"]
            except (ClientError, BotoCoreError):
                pass   # if this fails, every instance below just falls through to the regular provisioned path - never silently mis-tag a resource as Serverless without confirming it.

            paginator = docdb.get_paginator("describe_db_instances")
            for page in paginator.paginate():
                for db in page.get("DBInstances", []):
                    min_capacity = serverless_min_capacity.get(db.get("DBClusterIdentifier", ""))
                    is_serverless = min_capacity is not None
                    records.append({
                        "Resource ID":             db.get("DBInstanceArn") or db.get("DBInstanceIdentifier", ""),
                        "Resource Name":           db.get("DBInstanceIdentifier", ""),
                        "Resource Type":           "Amazon DocumentDB Serverless" if is_serverless else "Amazon DocumentDB",
                        "Resource State":          _map_rds_state(db.get("DBInstanceStatus", "")),
                        "Region":                  region,
                        "OS":                      "N/A",
                        # Serverless SKU is a synthetic "{DCU}DCU-min" string
                        # (no real AWS instance-class SKU exists for it,
                        # same "encode the real billing unit into the SKU"
                        # convention already used for Fargate/DynamoDB) so
                        # pricing/aws_price_list.py can parse the real
                        # MinCapacity back out and price it against the real
                        # $/DCU-hr rate.
                        "SKU":                     f"{min_capacity:g}DCU-min" if is_serverless else db.get("DBInstanceClass", "N/A"),
                        "Redundancy":              "N/A",   # DocumentDB HA is achieved via separate replica instances, not a Multi-AZ flag on one instance - confirmed no such price-differentiating attribute exists on real DocumentDB price list entries (unlike RDS).
                        "HA Replicas":             0,
                        "PAYG Hourly Cost USD":    0.0,
                        "Avg Daily Running Hours": 24,
                        "Subscription":            account_id,
                        "Provider":                "AWS",
                        "Is Orphaned":             False,
                    })
        except (ClientError, BotoCoreError):
            pass

        try:
            neptune = session.client("neptune", region_name=region)

            # Neptune Serverless - added 2026-08-23, same shape/reasoning as
            # DocumentDB Serverless above: bills per-NCU-hour (a continuously
            # auto-scaling capacity metric, not a fixed instance class),
            # confirmed real Database-Savings-Plan-eligible. The real live
            # NCU usage is only a CloudWatch time-series metric, but
            # describe_db_clusters()'s real ServerlessV2ScalingConfiguration.
            # MinCapacity field (confirmed via botocore's own installed
            # neptune service model - identical shape to DocumentDB's) is a
            # static, describable floor, used as the steady-state baseline
            # for the same reason: AWS's own Database Savings Plans guidance
            # recommends committing against the minimum sustained spend.
            serverless_min_capacity = {}
            try:
                for page in neptune.get_paginator("describe_db_clusters").paginate():
                    for cluster in page.get("DBClusters", []):
                        scaling = cluster.get("ServerlessV2ScalingConfiguration")
                        if scaling and scaling.get("MinCapacity") is not None:
                            serverless_min_capacity[cluster.get("DBClusterIdentifier", "")] = scaling["MinCapacity"]
            except (ClientError, BotoCoreError):
                pass   # if this fails, every instance below just falls through to the regular provisioned path - never silently mis-tag a resource as Serverless without confirming it.

            paginator = neptune.get_paginator("describe_db_instances")
            for page in paginator.paginate():
                for db in page.get("DBInstances", []):
                    min_capacity = serverless_min_capacity.get(db.get("DBClusterIdentifier", ""))
                    is_serverless = min_capacity is not None
                    records.append({
                        "Resource ID":             db.get("DBInstanceArn") or db.get("DBInstanceIdentifier", ""),
                        "Resource Name":           db.get("DBInstanceIdentifier", ""),
                        "Resource Type":           "Amazon Neptune Serverless" if is_serverless else "Amazon Neptune",
                        "Resource State":          _map_rds_state(db.get("DBInstanceStatus", "")),
                        "Region":                  region,
                        "OS":                      "N/A",
                        # Synthetic "{NCU}NCU-min" SKU, same convention as
                        # DocumentDB Serverless's "{DCU}DCU-min" - no real
                        # AWS instance-class SKU exists for Serverless.
                        "SKU":                     f"{min_capacity:g}NCU-min" if is_serverless else db.get("DBInstanceClass", "N/A"),
                        "Redundancy":              "N/A",   # Confirmed via real Neptune price list data: every Database Instance entry carries the same single "Multi-AZ" deploymentOption value regardless of actual replica topology - not a real Single-AZ/Multi-AZ price split the way RDS has, so not modeled as one here either.
                        "HA Replicas":             0,
                        "PAYG Hourly Cost USD":    0.0,
                        "Avg Daily Running Hours": 24,
                        "Subscription":            account_id,
                        "Provider":                "AWS",
                        "Is Orphaned":             False,
                    })
        except (ClientError, BotoCoreError):
            pass

        # Amazon Neptune Analytics - a genuinely SEPARATE product from
        # Neptune (Database) above, its own boto3 client ("neptune-graph",
        # not "neptune"), confirmed via botocore's own installed service
        # model (real operations: ListGraphs/GetGraph/StartGraph/StopGraph -
        # no Reserved*-style operation exists anywhere, so no RI concept).
        # A graph analytics engine, not a transactional database - billed by
        # a FIXED, user-chosen "provisionedMemory" capacity (in m-NCU,
        # memory-optimized Neptune Capacity Units), unlike Neptune
        # Serverless's auto-scaling min/max range - ListGraphs itself
        # returns provisionedMemory/status/replicaCount directly, no second
        # describe call needed. Confirmed Database-SP-eligible via AWS's own
        # March 2026 announcement extending Database Savings Plans to
        # Neptune Analytics. Real IAM: NeptuneGraphReadOnlyAccess.
        #
        # Genuinely unlike every other resource this app tracks: a STOPPED
        # graph still bills, at 10% of the running rate (confirmed via real
        # downloaded AmazonNeptune price list data - CreateGraph vs
        # StoppedGraph operations at the same m-ncu tier are exactly a 10:1
        # ratio, not a coincidence). The SKU encodes both the capacity AND
        # the running/stopped state (pricing/aws_price_list.py parses the
        # "-stopped" suffix to pick the right price-list operation) so this
        # real, non-zero "still costing money while stopped" fact isn't
        # silently dropped the way every other service's Stopped state
        # (correctly) implies $0/hr.
        #
        # replicaCount is captured for visibility (HA Replicas column,
        # matching the Redshift/OpenSearch node-count convention) but NOT
        # multiplied into PAYG Hourly Cost - unlike Redshift/OpenSearch
        # (each additional node is a separately billed, separately priced
        # line item there, confirmed via those services' own price list
        # data), no confirmed price list evidence was found that Neptune
        # Analytics replicas bill as a separate line item distinct from the
        # primary's provisionedMemory rate - left undisclosed rather than
        # guessed, same "don't fabricate what can't be confirmed" discipline
        # as the DynamoDB Reserved Capacity caveat elsewhere in this app.
        try:
            ng = session.client("neptune-graph", region_name=region)
            for page in ng.get_paginator("list_graphs").paginate():
                for graph in page.get("graphs", []):
                    status = graph.get("status", "")
                    is_running = (status == "AVAILABLE")
                    capacity = graph.get("provisionedMemory")
                    if capacity is None:
                        continue   # shouldn't happen for a real graph, but never fabricate a SKU from a missing value.
                    records.append({
                        "Resource ID":             graph.get("arn") or graph.get("id", ""),
                        "Resource Name":           graph.get("name", ""),
                        "Resource Type":           "Amazon Neptune Analytics",
                        "Resource State":          "Running" if is_running else "Stopped",
                        "Region":                  region,
                        "OS":                      "N/A",
                        "SKU":                     f"{capacity}m-NCU" if is_running else f"{capacity}m-NCU-stopped",
                        "Redundancy":              "N/A",
                        "HA Replicas":             graph.get("replicaCount") or 0,
                        "PAYG Hourly Cost USD":    0.0,
                        # 0 for a stopped graph, matching every other
                        # service's stopped-state convention (this field
                        # means "hours actively running", not "hours
                        # incurring any charge at all" - the residual 10%
                        # stopped rate is captured entirely in the SKU/price
                        # lookup above, not here) - keeps this resource
                        # correctly excluded from the 24x7 SP baseline
                        # filter (Resource State == "Running" already
                        # excludes it too, this is just for consistency).
                        "Avg Daily Running Hours": 24 if is_running else 0,
                        "Subscription":            account_id,
                        "Provider":                "AWS",
                        "Is Orphaned":             False,
                    })
        except (ClientError, BotoCoreError):
            pass

        # DMS replication instances - genuinely instance-class-based
        # (ReplicationInstanceClass, e.g. "dms.t3.medium") with a real
        # Single/Multi-AZ price split (confirmed via real AWSDatabaseMigrationSvc
        # price list data), unlike DocumentDB/Neptune above. No Reserved
        # Instance API exists for DMS either - Database-SP-eligible only.
        try:
            dms = session.client("dms", region_name=region)
            paginator = dms.get_paginator("describe_replication_instances")
            for page in paginator.paginate():
                for ri in page.get("ReplicationInstances", []):
                    records.append({
                        "Resource ID":             ri.get("ReplicationInstanceArn") or ri.get("ReplicationInstanceIdentifier", ""),
                        "Resource Name":           ri.get("ReplicationInstanceIdentifier", ""),
                        "Resource Type":           "AWS DMS Replication Instance",
                        "Resource State":          _map_rds_state(ri.get("ReplicationInstanceStatus", "")),
                        "Region":                  region,
                        "OS":                      "N/A",
                        "SKU":                     ri.get("ReplicationInstanceClass", "N/A"),
                        "Redundancy":              "Zone Redundant" if ri.get("MultiAZ") else "Locally Redundant",
                        "HA Replicas":             0,
                        "PAYG Hourly Cost USD":    0.0,
                        "Avg Daily Running Hours": 24,
                        "Subscription":            account_id,
                        "Provider":                "AWS",
                        "Is Orphaned":             False,
                    })
        except (ClientError, BotoCoreError):
            pass

        # DMS Serverless - a genuinely different resource ("ReplicationConfig",
        # not "ReplicationInstance" above), added 2026-08-23. Confirmed real
        # via botocore's own installed dms service model:
        # CreateReplicationConfig/DescribeReplicationConfigs, with a
        # ComputeConfig.MinCapacityUnits/MaxCapacityUnits DCU range (DMS's
        # own "DCU" - a different unit from DocumentDB's DCU, same acronym,
        # unrelated products). Same "MinCapacity floor as steady-state
        # baseline" reasoning as DocumentDB/Neptune Serverless. Real
        # current status comes from describe_replications() (a genuinely
        # separate call - DescribeReplicationConfigs only returns the
        # config, not live status), matching the config back to its
        # replication by ReplicationConfigArn.
        try:
            dms_serverless = session.client("dms", region_name=region)
            status_by_config_arn = {}
            try:
                for page in dms_serverless.get_paginator("describe_replications").paginate():
                    for repl in page.get("Replications", []):
                        status_by_config_arn[repl.get("ReplicationConfigArn", "")] = repl.get("Status", "")
            except (ClientError, BotoCoreError):
                pass   # config-only fallback below still works without live status - defaults to Stopped rather than assuming Running.

            for page in dms_serverless.get_paginator("describe_replication_configs").paginate():
                for cfg in page.get("ReplicationConfigs", []):
                    compute = cfg.get("ComputeConfig") or {}
                    min_capacity = compute.get("MinCapacityUnits")
                    if min_capacity is None:
                        continue   # never fabricate a SKU from a missing value.
                    config_arn = cfg.get("ReplicationConfigArn", "")
                    status = status_by_config_arn.get(config_arn, "")
                    is_running = status.lower() in ("running", "starting")
                    is_multi_az = bool(compute.get("MultiAZ"))
                    records.append({
                        "Resource ID":             config_arn or cfg.get("ReplicationConfigIdentifier", ""),
                        "Resource Name":           cfg.get("ReplicationConfigIdentifier", ""),
                        "Resource Type":           "AWS DMS Serverless",
                        "Resource State":          "Running" if is_running else "Stopped",
                        "Region":                  region,
                        "OS":                      "N/A",
                        # Synthetic "{DCU}DCU-min" SKU, same convention as
                        # DocumentDB/Neptune Serverless's "{unit}-min" SKUs.
                        "SKU":                     f"{min_capacity}DCU-min",
                        "Redundancy":              "Zone Redundant" if is_multi_az else "Locally Redundant",
                        "HA Replicas":             0,
                        "PAYG Hourly Cost USD":    0.0,
                        "Avg Daily Running Hours": 24 if is_running else 0,
                        "Subscription":            account_id,
                        "Provider":                "AWS",
                        "Is Orphaned":             False,
                    })
        except (ClientError, BotoCoreError):
            pass

        # DynamoDB and Keyspaces - both serverless/table-based (no instance
        # class at all), billed by provisioned Read/Write Capacity Units at
        # a flat $/unit-hour rate (confirmed identical shape via real price
        # list data for both AmazonDynamoDB and AmazonMCS - Keyspaces
        # intentionally price-matches DynamoDB). Only PROVISIONED-mode
        # tables get a resource row: on-demand/pay-per-request tables have
        # no capacity commitment concept at all (billed per actual request,
        # like Lambda) - same "don't fabricate what can't be priced"
        # discipline used for SageMaker/Lambda/Timestream elsewhere in this
        # app, just at the table level instead of the whole service level.
        # SKU encodes the provisioned RCU/WCU pair (e.g. "5RCU-5WCU") so
        # pricing/aws_price_list.py can parse it back out and compute
        # rcu*rate + wcu*rate - there's no literal AWS "SKU" for this the
        # way EC2/RDS have.
        try:
            ddb = session.client("dynamodb", region_name=region)
            paginator = ddb.get_paginator("list_tables")
            table_names = [name for page in paginator.paginate() for name in page.get("TableNames", [])]
            for name in table_names:
                desc = ddb.describe_table(TableName=name).get("Table", {})
                throughput = desc.get("ProvisionedThroughput", {})
                rcu = throughput.get("ReadCapacityUnits") or 0
                wcu = throughput.get("WriteCapacityUnits") or 0
                if not rcu and not wcu:
                    continue   # on-demand table - no provisioned capacity to price.
                records.append({
                    "Resource ID":             desc.get("TableArn") or name,
                    "Resource Name":           name,
                    "Resource Type":           "Amazon DynamoDB",
                    "Resource State":          "Running" if desc.get("TableStatus") == "ACTIVE" else "Stopped",
                    "Region":                  region,
                    "OS":                      "N/A",
                    "SKU":                     f"{rcu}RCU-{wcu}WCU",
                    "Redundancy":              "N/A",
                    "HA Replicas":             0,
                    "PAYG Hourly Cost USD":    0.0,
                    "Avg Daily Running Hours": 24,
                    "Subscription":            account_id,
                    "Provider":                "AWS",
                    "Is Orphaned":             False,
                })
        except (ClientError, BotoCoreError):
            pass

        try:
            ks = session.client("keyspaces", region_name=region)
            for kp in ks.list_keyspaces().get("keyspaces", []):
                keyspace_name = kp.get("keyspaceName", "")
                try:
                    for tp in ks.list_tables(keyspaceName=keyspace_name).get("tables", []):
                        table_name = tp.get("tableName", "")
                        table = ks.get_table(keyspaceName=keyspace_name, tableName=table_name)
                        cap = table.get("capacitySpecification", {})
                        if cap.get("throughputMode") != "PROVISIONED":
                            continue   # pay-per-request table - no provisioned capacity to price, same as DynamoDB on-demand above.
                        rcu = cap.get("readCapacityUnits") or 0
                        wcu = cap.get("writeCapacityUnits") or 0
                        records.append({
                            "Resource ID":             table.get("resourceArn") or f"{keyspace_name}.{table_name}",
                            "Resource Name":           f"{keyspace_name}.{table_name}",
                            "Resource Type":           "Amazon Keyspaces",
                            "Resource State":          "Running" if table.get("status") == "ACTIVE" else "Stopped",
                            "Region":                  region,
                            "OS":                      "N/A",
                            "SKU":                     f"{rcu}RCU-{wcu}WCU",
                            "Redundancy":              "N/A",
                            "HA Replicas":             0,
                            "PAYG Hourly Cost USD":    0.0,
                            "Avg Daily Running Hours": 24,
                            "Subscription":            account_id,
                            "Provider":                "AWS",
                            "Is Orphaned":             False,
                        })
                except (ClientError, BotoCoreError):
                    pass   # one keyspace's tables failing to list shouldn't drop every other keyspace in the region.
        except (ClientError, BotoCoreError):
            pass

        # Fargate (ECS launch type) - genuinely no instance class either;
        # billed per-vCPU-hour + per-GB-hour of the task's own configured
        # cpu/memory (confirmed via real AmazonECS price list data: separate
        # Linux/Windows and x86/ARM rate tiers). SKU encodes vCPU/memory so
        # pricing/aws_price_list.py can parse it back and apply the right
        # rate tier by (region, arch, OS). EC2-launch-type ECS tasks are
        # deliberately NOT included here - those already show up as regular
        # EC2 instances via the EC2 block above (the cluster's underlying
        # EC2 capacity), counting them again here would double-count the
        # same compute. Compute-Savings-Plan-eligible only (confirmed via
        # AWS's own Savings Plans docs) - Fargate has no Reserved Instance
        # concept at all.
        try:
            ecs = session.client("ecs", region_name=region)
            cluster_arns = [a for page in ecs.get_paginator("list_clusters").paginate() for a in page.get("clusterArns", [])]
            for cluster_arn in cluster_arns:
                try:
                    task_arns = [
                        a for page in ecs.get_paginator("list_tasks").paginate(cluster=cluster_arn, desiredStatus="RUNNING")
                        for a in page.get("taskArns", [])
                    ]
                    for i in range(0, len(task_arns), 100):   # describe_tasks accepts at most 100 ARNs per call - confirmed via boto3's service model.
                        batch = task_arns[i:i + 100]
                        for task in ecs.describe_tasks(cluster=cluster_arn, tasks=batch).get("tasks", []):
                            if task.get("launchType") != "FARGATE":
                                continue   # EC2-launch-type tasks already counted as regular EC2 instances above.
                            cpu_units = int(task.get("cpu") or 0)
                            memory_mb = int(task.get("memory") or 0)
                            if not cpu_units or not memory_mb:
                                continue
                            vcpu = cpu_units / 1024.0
                            memory_gb = memory_mb / 1024.0
                            platform_family = (task.get("platformFamily") or "").upper()
                            os_ = "Windows" if "WINDOWS" in platform_family else "Linux"
                            task_id = (task.get("taskArn") or "").rsplit("/", 1)[-1]
                            records.append({
                                "Resource ID":             task.get("taskArn") or task_id,
                                "Resource Name":           task_id,
                                "Resource Type":           "AWS Fargate",
                                "Resource State":          "Running" if task.get("lastStatus") == "RUNNING" else "Stopped",
                                "Region":                  region,
                                "OS":                      os_,
                                "SKU":                     f"{vcpu:g}vCPU-{memory_gb:g}GB",
                                "Redundancy":              "N/A",
                                "HA Replicas":             0,
                                "PAYG Hourly Cost USD":    0.0,
                                "Avg Daily Running Hours": 24,
                                "Subscription":            account_id,
                                "Provider":                "AWS",
                                "Is Orphaned":             False,
                            })
                except (ClientError, BotoCoreError):
                    pass   # one cluster failing to list tasks shouldn't drop every other cluster in the region.
        except (ClientError, BotoCoreError):
            pass

        # OpenSearch - already demo-tracked and RI-eligible (see
        # db/aws_seed.py's AWS_RI_COVERAGE_NOTES) but had no live fetch of
        # any kind until now. Real IAM action is "es:Describe*" (confirmed
        # via AmazonOpenSearchServiceReadOnlyAccess's own policy JSON -
        # another historical-naming holdover from before the "Elasticsearch
        # Service" rename). DescribeDomains accepts at most 5 domain names
        # per call - confirmed via AWS's own API docs - hence the batching.
        # No running/stopped lifecycle exists for OpenSearch domains
        # (confirmed via boto3's DescribeDomains output shape - no state
        # field beyond Processing/Deleted, which DescribeDomains itself
        # never returns for a deleted domain) - always "Running", same
        # always-on convention as ElastiCache/Redshift/DocumentDB/Neptune.
        try:
            aos = session.client("opensearch", region_name=region)
            domain_names = [d["DomainName"] for d in aos.list_domain_names().get("DomainNames", [])]
            for i in range(0, len(domain_names), 5):
                batch = domain_names[i:i + 5]
                for dom in aos.describe_domains(DomainNames=batch).get("DomainStatusList", []):
                    cluster = dom.get("ClusterConfig", {})
                    instance_type = cluster.get("InstanceType", "N/A")
                    instance_count = cluster.get("InstanceCount") or 1
                    domain_name = dom.get("DomainName", "")
                    domain_arn = dom.get("ARN") or domain_name
                    # One row PER NODE, not per domain - unlike RDS's
                    # Multi-AZ (where the standby is invisible via the API
                    # and AWS bills it through a separate, already-doubled
                    # price meter on the single visible instance),
                    # OpenSearch's InstanceCount nodes are real, homogeneous,
                    # identically-priced instances with no special primary/
                    # standby price differentiation (confirmed via real
                    # AmazonES price list data) - modeling each as its own
                    # resource row is the mechanically accurate match, not a
                    # workaround, and reuses the existing per-instance
                    # pricing lookup unchanged rather than needing a new
                    # count-aware variant.
                    for node_idx in range(instance_count):
                        records.append({
                            "Resource ID":             f"{domain_arn}#node{node_idx}",
                            "Resource Name":           f"{domain_name}-node-{node_idx}" if instance_count > 1 else domain_name,
                            "Resource Type":           "Amazon OpenSearch",
                            "Resource State":          "Running",
                            "Region":                  region,
                            "OS":                      "N/A",
                            "SKU":                     instance_type,
                            "Redundancy":              "N/A",   # No per-instance price differentiator for zone awareness - confirmed via real AmazonES price list data.
                            "HA Replicas":             0,
                            "PAYG Hourly Cost USD":    0.0,
                            "Avg Daily Running Hours": 24,
                            "Subscription":            account_id,
                            "Provider":                "AWS",
                            "Is Orphaned":             False,
                        })
        except (ClientError, BotoCoreError):
            pass

        # ElastiCache, Redshift, MemoryDB - discovered 2026-08-23 that
        # inventory for these was NEVER fetched at all, only their Reserved
        # Instance purchases (see fetch_live_reservations() below) - a real
        # tenant's actual ElastiCache/Redshift resources were completely
        # invisible despite already being demo-tracked and RI-eligible.
        # elasticache:Describe* / redshift:Describe* (already required for
        # RI data above) already cover DescribeCacheClusters/DescribeClusters
        # too - confirmed via each policy's own JSON - no new IAM permission
        # needed for either.
        try:
            ec = session.client("elasticache", region_name=region)
            paginator = ec.get_paginator("describe_cache_clusters")
            for page in paginator.paginate():
                for cc in page.get("CacheClusters", []):
                    records.append({
                        "Resource ID":             cc.get("ARN") or cc.get("CacheClusterId", ""),
                        "Resource Name":           cc.get("CacheClusterId", ""),
                        "Resource Type":           map_elasticache_engine(cc.get("Engine", "")),
                        "Resource State":          "Running" if cc.get("CacheClusterStatus") == "available" else "Stopped",
                        "Region":                  region,
                        "OS":                      "N/A",
                        "SKU":                     cc.get("CacheNodeType", "N/A"),
                        "Redundancy":              "N/A",   # No per-node price differentiator for replication-group membership (confirmed via real AmazonElastiCache price list data - each node in a replication group is billed at the same flat per-node rate as a standalone cluster, no Multi-AZ-style doubled meter the way RDS has).
                        "HA Replicas":             0,
                        "PAYG Hourly Cost USD":    0.0,
                        "Avg Daily Running Hours": 24,
                        "Subscription":            account_id,
                        "Provider":                "AWS",
                        "Is Orphaned":             False,
                    })
        except (ClientError, BotoCoreError):
            pass

        try:
            rs = session.client("redshift", region_name=region)
            paginator = rs.get_paginator("describe_clusters")
            for page in paginator.paginate():
                for cl in page.get("Clusters", []):
                    records.append({
                        "Resource ID":             cl.get("ClusterNamespaceArn") or cl.get("ClusterIdentifier", ""),
                        "Resource Name":           cl.get("ClusterIdentifier", ""),
                        "Resource Type":           "Amazon Redshift",
                        "Resource State":          "Running" if cl.get("ClusterStatus") == "available" else "Stopped",
                        "Region":                  region,
                        "OS":                      "N/A",
                        "SKU":                     cl.get("NodeType", "N/A"),
                        "Redundancy":              "N/A",
                        "HA Replicas":             max((cl.get("NumberOfNodes") or 1) - 1, 0),
                        "PAYG Hourly Cost USD":    0.0,
                        "Avg Daily Running Hours": 24,
                        "Subscription":            account_id,
                        "Provider":                "AWS",
                        "Is Orphaned":             False,
                    })
        except (ClientError, BotoCoreError):
            pass

        # MemoryDB - new service, never tracked in this app at all before
        # 2026-08-23. Real IAM action is memorydb:Describe* (confirmed via
        # AmazonMemoryDBReadOnlyAccess's own policy JSON), one wildcard
        # covering both inventory and Reserved Nodes, same pattern as every
        # other service added this session. One row per real node (iterating
        # Shards[].Nodes[], which DescribeClusters exposes directly - unlike
        # OpenSearch, MemoryDB gives real per-node identifiers, no synthetic
        # IDs needed).
        #
        # Resource Type deliberately stays flat "Amazon MemoryDB" (NOT split
        # by engine like ElastiCache above) - confirmed via boto3's service
        # model that MemoryDB's Reserved Node purchase record (below) carries
        # NO engine/product-description field at all, unlike ElastiCache's
        # ReservedCacheNode (which has ProductDescription). Splitting
        # inventory by engine while RI purchases can't be engine-specific
        # would make coverage matching (which requires an exact Resource
        # Type match between demand and supply) silently and permanently
        # broken for MemoryDB - AWS's own reservation product doesn't
        # distinguish engine either, so neither does this app's matching key.
        # Real per-node engine is still captured in Resource Name for
        # visibility (via map_memorydb_engine()) - just not part of the
        # matching key. See pricing/aws_price_list.py for how this affects
        # pricing (defaults to the safer/higher-priced engine when the real
        # one can't be threaded through a flat SKU+resource_type lookup).
        try:
            mdb = session.client("memorydb", region_name=region)
            paginator = mdb.get_paginator("describe_clusters")
            for page in paginator.paginate(ShowShardDetails=True):
                for cluster in page.get("Clusters", []):
                    cluster_name = cluster.get("Name", "")
                    node_type = cluster.get("NodeType", "N/A")
                    engine_label = map_memorydb_engine(cluster.get("Engine", ""))
                    cluster_arn = cluster.get("ARN") or cluster_name
                    for shard in cluster.get("Shards", []):
                        for node in shard.get("Nodes", []):
                            node_name = node.get("Name", "")
                            records.append({
                                "Resource ID":             f"{cluster_arn}#{node_name}",
                                "Resource Name":           f"{node_name or cluster_name} ({engine_label.rsplit(' ', 1)[-1]})",
                                "Resource Type":           "Amazon MemoryDB",
                                "Resource State":          "Running" if node.get("Status") == "available" else "Stopped",
                                "Region":                  region,
                                "OS":                      "N/A",
                                "SKU":                     node_type,
                                "Redundancy":              "N/A",
                                "HA Replicas":             0,
                                "PAYG Hourly Cost USD":    0.0,
                                "Avg Daily Running Hours": 24,
                                "Subscription":            account_id,
                                "Provider":                "AWS",
                                "Is Orphaned":             False,
                            })
        except (ClientError, BotoCoreError):
            pass

        # Amazon SageMaker - Real-Time Inference Endpoints + Notebook
        # Instances. Added 2026-08-23 after confirming feasibility: unlike
        # classic Lambda (no running-resource state) or Lambda Managed
        # Instances (RI/SP-eligible but AWS exposes no per-instance
        # visibility, only pool-level CloudWatch aggregates), these two
        # SageMaker resource types are real, listable, persistent resources
        # with an instance type and running/stopped state, the same shape
        # every other tracked service in this app has. Training/Processing/
        # Data Wrangler/Batch Transform jobs are deliberately NOT fetched -
        # they're one-shot ephemeral executions with no persistent identity,
        # not a "resource" this app's inventory model can represent at all
        # (same category as Lambda invocations or Glue jobs). Real IAM:
        # AmazonSageMakerReadOnly (sagemaker:Describe*/List* wildcard,
        # confirmed via the policy's own JSON) - see REQUIRED_AWS_POLICIES.
        try:
            sm = session.client("sagemaker", region_name=region)

            # Real-Time Inference Endpoints. DescribeEndpoint itself doesn't
            # carry instance type (that lives on the EndpointConfig it
            # references, confirmed via CreateEndpointConfig's own
            # ProductionVariants[].InstanceType parameter) - one extra
            # DescribeEndpointConfig call per endpoint is needed to get the
            # real SKU, same "list then describe for detail" shape RDS/EC2
            # already use elsewhere in this function.
            for page in sm.get_paginator("list_endpoints").paginate():
                for ep_summary in page.get("Endpoints", []):
                    ep_name = ep_summary.get("EndpointName", "")
                    try:
                        ep = sm.describe_endpoint(EndpointName=ep_name)
                        config = sm.describe_endpoint_config(EndpointConfigName=ep.get("EndpointConfigName", ep_name))
                        status = ep.get("EndpointStatus", "")
                        for variant in config.get("ProductionVariants", []):
                            instance_type = variant.get("InstanceType")
                            if not instance_type:
                                continue   # serverless/managed-instance variants carry no fixed instance type - out of scope, same reasoning as Lambda Managed Instances.
                            instance_count = variant.get("InitialInstanceCount") or 1
                            variant_name = variant.get("VariantName", "")
                            for node_idx in range(instance_count):
                                records.append({
                                    "Resource ID":             f"{ep.get('EndpointArn') or ep_name}#{variant_name}#{node_idx}",
                                    "Resource Name":           f"{ep_name}-{variant_name}" if instance_count == 1 else f"{ep_name}-{variant_name}-{node_idx}",
                                    "Resource Type":           "Amazon SageMaker Endpoint",
                                    "Resource State":          "Running" if status == "InService" else "Stopped",
                                    "Region":                  region,
                                    "OS":                      "N/A",
                                    "SKU":                     instance_type,
                                    "Redundancy":              "N/A",
                                    "HA Replicas":             0,
                                    "PAYG Hourly Cost USD":    0.0,
                                    "Avg Daily Running Hours": 24,
                                    "Subscription":            account_id,
                                    "Provider":                "AWS",
                                    "Is Orphaned":             False,
                                })
                    except (ClientError, BotoCoreError):
                        pass   # one endpoint failing to describe shouldn't drop every other endpoint in the region.

            # Notebook Instances - InstanceType is returned directly on the
            # list response itself (NotebookInstanceSummary), no separate
            # describe call needed, unlike Endpoints above.
            for page in sm.get_paginator("list_notebook_instances").paginate():
                for nb in page.get("NotebookInstances", []):
                    nb_name = nb.get("NotebookInstanceName", "")
                    records.append({
                        "Resource ID":             nb.get("NotebookInstanceArn") or nb_name,
                        "Resource Name":           nb_name,
                        "Resource Type":           "Amazon SageMaker Notebook Instance",
                        "Resource State":          "Running" if nb.get("NotebookInstanceStatus") == "InService" else "Stopped",
                        "Region":                  region,
                        "OS":                      "N/A",
                        "SKU":                     nb.get("InstanceType", "N/A"),
                        "Redundancy":              "N/A",
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
    Fetches every active EC2, RDS, ElastiCache, and Redshift Reserved
    Instance/Node across every AWS region enabled for this account.
    Returns a DataFrame shaped for AWSReservationPurchase (db/schema.py) -
    NOT the Azure-shaped ReservationPurchase table; see that class's
    docstring for why they're kept separate.

    ElastiCache and Redshift added 2026-08-22 - each is a genuinely
    separate reservation system from EC2/RDS's (elasticache:
    DescribeReservedCacheNodes / redshift:DescribeReservedNodes, own IAM
    permissions, own managed policies), not something EC2/RDS's fetch
    happens to also cover. Confirmed their response shapes are close
    enough to EC2/RDS's (FixedPrice/UsagePrice/Duration/RecurringCharges/
    State) via boto3's own service model plus real examples in AWS's docs
    that no new AWSReservationPurchase columns were needed - both are
    added as new `service` values ("ElastiCache"/"Redshift") in the same
    per-region loop, same active-only filter, same effective-hourly-rate
    formula in pricing/aws_commitment_mapping.py. Both are standard
    regionalized services (confirmed via botocore's own endpoints.json,
    same as EC2/RDS - not global-only like Pricing/Savings Plans), so the
    same all-region scan applies. Redshift's DescribeReservedNodes is
    confirmed to be ONLY the classic provisioned-cluster reservation
    system - Redshift Serverless uses a completely separate RPU-based
    reservation concept with its own different API, not fetched here (no
    Redshift Serverless inventory tracked by this app at all yet).
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
    # Same sts:GetCallerIdentity call fetch_live_inventory() already makes -
    # tags every purchase record with the calling account's real Account ID
    # (traceability only, see Commitment.scope_availability_zone's comment
    # on why this app doesn't restrict matching by it).
    account_id = session.client("sts").get_caller_identity().get("Account", "")
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

        try:
            ec = session.client("elasticache", region_name=region)
            for ri in ec.describe_reserved_cache_nodes().get("ReservedCacheNodes", []):
                if ri.get("State") != "active":
                    continue
                records.append({
                    "service":               "ElastiCache",
                    "reserved_instance_id":  ri.get("ReservedCacheNodeId", ""),
                    "instance_type":         ri.get("CacheNodeType", ""),
                    "region":                region,
                    "availability_zone":     None,
                    "product_description":   ri.get("ProductDescription"),   # "redis" | "memcached" | "valkey" - same engine-identifier convention as DescribeCacheClusters's Engine field, confirmed via a real "memcached" example in AWS's own docs.
                    "instance_count":        ri.get("CacheNodeCount", 0),
                    "duration_seconds":      ri.get("Duration", 0),
                    "fixed_price":           ri.get("FixedPrice"),
                    "usage_price":           ri.get("UsagePrice"),
                    "currency_code":         None,   # ElastiCache's ReservedCacheNode has no CurrencyCode field (confirmed via boto3 service model) - unlike EC2/RDS/Redshift.
                    "offering_type":         ri.get("OfferingType"),
                    "offering_class":        None,
                    "instance_tenancy":      None,
                    "scope":                 None,
                    "multi_az":              None,
                    "state":                 ri.get("State", ""),
                    "start_time":            str(ri.get("StartTime")) if ri.get("StartTime") else None,
                    "recurring_charge_hourly": _recurring_hourly_charge(ri.get("RecurringCharges")),
                })
        except (ClientError, BotoCoreError):
            pass

        try:
            rs = session.client("redshift", region_name=region)
            for ri in rs.describe_reserved_nodes().get("ReservedNodes", []):
                if ri.get("State") != "active":
                    continue
                records.append({
                    "service":               "Redshift",
                    "reserved_instance_id":  ri.get("ReservedNodeId", ""),
                    "instance_type":         ri.get("NodeType", ""),
                    "region":                region,
                    "availability_zone":     None,
                    "product_description":   None,   # Redshift has no ProductDescription field (confirmed via boto3 service model) - unlike EC2/RDS/ElastiCache, there's only one Redshift engine.
                    "instance_count":        ri.get("NodeCount", 0),
                    "duration_seconds":      ri.get("Duration", 0),
                    "fixed_price":           ri.get("FixedPrice"),
                    "usage_price":           ri.get("UsagePrice"),
                    "currency_code":         ri.get("CurrencyCode"),
                    "offering_type":         ri.get("OfferingType"),
                    "offering_class":        None,
                    "instance_tenancy":      None,
                    "scope":                 None,
                    "multi_az":              None,
                    "state":                 ri.get("State", ""),
                    "start_time":            str(ri.get("StartTime")) if ri.get("StartTime") else None,
                    "recurring_charge_hourly": _recurring_hourly_charge(ri.get("RecurringCharges")),
                })
        except (ClientError, BotoCoreError):
            pass

        # OpenSearch Reserved Instances - same real field shape as EC2/RDS/
        # ElastiCache/Redshift (InstanceType/Duration/FixedPrice/UsagePrice/
        # InstanceCount/RecurringCharges/State), confirmed via boto3's
        # opensearch service model. No ProductDescription field (single
        # engine, like Redshift) and no scope/tenancy/multi_az concept
        # either (confirmed via the same service model - OpenSearch RIs
        # aren't Regional/Zonal or Standard/Convertible the way EC2's are).
        try:
            aos_ri = session.client("opensearch", region_name=region)
            for ri in aos_ri.describe_reserved_instances().get("ReservedInstances", []):
                if ri.get("State") != "active":
                    continue
                records.append({
                    "service":               "OpenSearch",
                    "reserved_instance_id":  ri.get("ReservedInstanceId", ""),
                    "instance_type":         ri.get("InstanceType", ""),
                    "region":                region,
                    "availability_zone":     None,
                    "product_description":   None,
                    "instance_count":        ri.get("InstanceCount", 0),
                    "duration_seconds":      ri.get("Duration", 0),
                    "fixed_price":           ri.get("FixedPrice"),
                    "usage_price":           ri.get("UsagePrice"),
                    "currency_code":         ri.get("CurrencyCode"),
                    "offering_type":         ri.get("PaymentOption"),
                    "offering_class":        None,
                    "instance_tenancy":      None,
                    "scope":                 None,
                    "multi_az":              None,
                    "state":                 ri.get("State", ""),
                    "start_time":            str(ri.get("StartTime")) if ri.get("StartTime") else None,
                    "recurring_charge_hourly": _recurring_hourly_charge(ri.get("RecurringCharges")),
                })
        except (ClientError, BotoCoreError):
            pass

        # MemoryDB Reserved Nodes - confirmed via boto3's service model: no
        # UsagePrice field at all (unlike every other RI type fetched so
        # far) - only FixedPrice + RecurringCharges. _effective_hourly_rate()
        # already treats a missing usage_price as 0.0, so this needs no
        # special-casing, just an honest None passthrough rather than
        # guessing a value. Also no product_description/engine field - see
        # fetch_live_inventory()'s MemoryDB block for why scope_resource_type
        # stays flat "Amazon MemoryDB" rather than split by engine.
        try:
            mdb_ri = session.client("memorydb", region_name=region)
            for ri in mdb_ri.describe_reserved_nodes().get("ReservedNodes", []):
                if ri.get("State") != "active":
                    continue
                records.append({
                    "service":               "MemoryDB",
                    "reserved_instance_id":  ri.get("ReservationId", ""),
                    "instance_type":         ri.get("NodeType", ""),
                    "region":                region,
                    "availability_zone":     None,
                    "product_description":   None,
                    "instance_count":        ri.get("NodeCount", 0),
                    "duration_seconds":      ri.get("Duration", 0),
                    "fixed_price":           ri.get("FixedPrice"),
                    "usage_price":           None,
                    "currency_code":         None,
                    "offering_type":         ri.get("OfferingType"),
                    "offering_class":        None,
                    "instance_tenancy":      None,
                    "scope":                 None,
                    "multi_az":              None,
                    "state":                 ri.get("State", ""),
                    "start_time":            str(ri.get("StartTime")) if ri.get("StartTime") else None,
                    "recurring_charge_hourly": _recurring_hourly_charge(ri.get("RecurringCharges")),
                })
        except (ClientError, BotoCoreError):
            pass

    df = pd.DataFrame(records)
    if not df.empty:
        df["account_id"] = account_id
    return df


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
    account_id = session.client("sts").get_caller_identity().get("Account", "")
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

    df = pd.DataFrame(records)
    if not df.empty:
        df["account_id"] = account_id
    return df


def fetch_savings_plans_recommendation(
    creds: AWSCredentials,
    savings_plans_type: str,
    term_years: str,
    payment_option: str = "NO_UPFRONT",
    lookback_days: str = "THIRTY_DAYS",
) -> float | None:
    """
    Real-time Savings Plans discount %, via
    ce:GetSavingsPlansPurchaseRecommendation - a PAID Cost Explorer call
    (same $0.01/request cost class as ce:GetCostAndUsage, see
    check_aws_permissions' "unverified" treatment of both), so this is
    called sparingly by the sync pipeline (staleness-checked), never on a
    live page render.

    Request/response shape verified directly against botocore's own
    service definition (github.com/boto/botocore, data/ce/2017-10-25/
    service-2.json), not guessed:
      - savings_plans_type: one of the real SavingsPlansType enum values.
        This app passes "COMPUTE_SP", "SAGEMAKER", or "DB_COMPUTE_SP" -
        each a clean one-to-one mapping onto this app's own pool tabs
        (confirmed via AWS's own official Savings Plans docs,
        docs.aws.amazon.com/savingsplans/latest/userguide/plan-types.html:
        Database Savings Plans is a single unified commitment spanning
        Aurora/RDS/DynamoDB/ElastiCache/DocumentDB/Timestream/Neptune/
        Keyspaces/DMS/OpenSearch, matching this app's Database pool - the
        other database-adjacent enum values, RDS/ELASTICACHE/REDSHIFT/
        OPENSEARCH/DYNAMODB_RESERVATIONS, are older per-service Reserved
        Instance-style recommendations, a separate AWS product not used
        here).
      - term_years: "ONE_YEAR" or "THREE_YEARS" - Database Savings Plans
        are 1-year-only (confirmed via AWS's own Savings Plans FAQ), so
        this app never requests "THREE_YEARS" for "DB_COMPUTE_SP".
      - Returns SavingsPlansPurchaseRecommendationSummary's
        EstimatedSavingsPercentage - the account-level discount % for
        that type/term/payment-option, computed by AWS from the
        account's own real historical usage (more accurate than a
        static per-SKU rate lookup, which is why this doesn't try to
        populate the Azure-shaped CommitmentPriceCache table instead).

    Returns None on any failure, missing/empty data, or malformed
    response - never raises, so a failed pricing fetch can't break
    inventory/RI/SP sync (same isolated-failure principle as
    fetch_live_reservations/fetch_live_savings_plans).
    """
    if not HAS_BOTO3 or not creds.is_complete:
        return None

    try:
        session = boto3.Session(
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            region_name=creds.region,
        )
        ce_client = session.client("ce")
        response = ce_client.get_savings_plans_purchase_recommendation(
            SavingsPlansType=savings_plans_type,
            TermInYears=term_years,
            PaymentOption=payment_option,
            LookbackPeriodInDays=lookback_days,
        )
        recommendations = response.get("SavingsPlansPurchaseRecommendations") or []
        if not recommendations:
            return None
        summary = recommendations[0].get("SavingsPlansPurchaseRecommendationSummary") or {}
        pct = summary.get("EstimatedSavingsPercentage")
        return float(pct) if pct is not None else None
    except (ClientError, BotoCoreError, ValueError, TypeError, KeyError, IndexError):
        return None
