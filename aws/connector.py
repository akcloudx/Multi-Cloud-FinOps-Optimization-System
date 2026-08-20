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
  - rds:DescribeDBInstances
  - savingsplans:DescribeSavingsPlans
  - ce:GetCostAndUsage
  - ce:GetSavingsPlansUtilization
  - ce:GetReservationUtilization
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


REQUIRED_AWS_POLICIES = [
    {
        "Policy / Action": "ec2:DescribeInstances",
        "Required":        "Yes — Mandatory",
        "Purpose":         "Scan all EC2 instances across regions",
    },
    {
        "Policy / Action": "rds:DescribeDBInstances",
        "Required":        "Yes — Mandatory",
        "Purpose":         "Scan RDS instances and database clusters",
    },
    {
        "Policy / Action": "savingsplans:DescribeSavingsPlans",
        "Required":        "Yes — For SP data",
        "Purpose":         "Fetch active AWS Compute & EC2 Savings Plans",
    },
    {
        "Policy / Action": "ce:GetCostAndUsage",
        "Required":        "Yes — For Cost Explorer",
        "Purpose":         "Query AWS Cost Explorer for un-discounted PAYG rates and usage",
    },
    {
        "Policy / Action": "ce:GetReservationUtilization",
        "Required":        "Yes — For RI data",
        "Purpose":         "Read active EC2 & RDS Reserved Instance utilization",
    },
]


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

    - ec2:DescribeInstances - checked via the real EC2 DryRun=True mechanism
      (AWS's own purpose-built "would this be allowed" flag for this
      service): a DryRunOperation response means allowed, UnauthorizedOperation
      means denied. No real call, no data pulled, no cost.
    - rds:DescribeDBInstances / savingsplans:DescribeSavingsPlans - EC2's
      DryRun convention isn't universal (RDS/Savings Plans don't support it),
      so these are checked with a real, minimal, genuinely free read-only
      call (MaxRecords=20 is RDS's own required minimum, not a chosen value).
    - ce:GetCostAndUsage / ce:GetReservationUtilization - deliberately NOT
      probed live. AWS's own Cost Explorer docs are explicit: "Each
      paginated API request incurs a charge of $0.01" (https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html)
      - a permission CHECK should never itself cost real money. Reported as
      "unverified" here; the real status gets confirmed the first time an
      actual cost sync calls these for real data anyway (data/sync_pipeline.py,
      once AWS live fetch exists), so the $0.01 is only ever spent getting
      real data, never spent just to check a box.

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
        "rds:DescribeDBInstances",
        lambda: session.client("rds").describe_db_instances(MaxRecords=20),
    )
    _probe(
        "savingsplans:DescribeSavingsPlans",
        lambda: session.client("savingsplans").describe_savings_plans(maxResults=1),
    )

    for action in ["ce:GetCostAndUsage", "ce:GetReservationUtilization"]:
        results.append({
            "action": action,
            "status": "unverified",
            "detail": "Not checked separately - the Cost Explorer API charges $0.01 per request, "
                      "so this is confirmed the first time real cost data is synced, not by a "
                      "standalone check.",
        })

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
