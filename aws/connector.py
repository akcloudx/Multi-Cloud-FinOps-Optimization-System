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
