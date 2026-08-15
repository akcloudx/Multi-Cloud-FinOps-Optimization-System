"""
db/aws_seed.py — Mock AWS infrastructure seed data.

Stored in separate database file: aws_finops.db

COMPUTE (Compute Savings Plans  OR  EC2 Instance Savings Plans  OR  EC2 Reserved Instances):
  - Prod EC2 Instances 24x7 (m5.large, c5.xlarge)  → EC2 RI / SP candidates
  - Dev  EC2 Instances 10 hrs/day (t3.medium, t3.micro) → Compute SP candidates
  - Stopped legacy EC2 with active RI → Orphaned

DATABASES (RDS Reserved Instances  OR  DynamoDB Reserved Capacity):
  Amazon RDS PostgreSQL, Amazon RDS MySQL, Amazon Aurora, Amazon DynamoDB

RI-ONLY SERVICES (Reserved Capacity / Nodes):
  Amazon ElastiCache, Amazon Redshift, Amazon OpenSearch Service, Amazon S3 Glacier

COMMITMENTS:
  - 2 × AWS EC2 Reserved Instances
  - 1 × AWS RDS Reserved Instance
  - 1 × AWS ElastiCache Reserved Node
  - 1 × AWS Compute Savings Plan
  - 1 × AWS EC2 Instance Savings Plan
"""

import sys, os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy.orm import Session
from db.schema import init_db, get_engine, CloudInventory, Commitment


# ── AWS Compute Inventory (EC2) ────────────────────────────────────────────────

AWS_COMPUTE_INVENTORY = [
    # Prod EC2 — 24x7 → Reserved Instance or SP candidates
    {"resource_id": "i-0123456789abcdef0", "resource_name": "aws-prod-web-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "us-east-1", "os": "Linux", "sku": "m5.large",
     "payg_hourly_usd": 0.096, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    {"resource_id": "i-0123456789abcdef1", "resource_name": "aws-prod-web-02",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "us-east-1", "os": "Linux", "sku": "m5.large",
     "payg_hourly_usd": 0.096, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    {"resource_id": "i-0123456789abcdef2", "resource_name": "aws-prod-app-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "us-east-1", "os": "Linux", "sku": "c5.xlarge",
     "payg_hourly_usd": 0.170, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    {"resource_id": "i-0123456789abcdef3", "resource_name": "aws-prod-app-02",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "us-east-1", "os": "Linux", "sku": "c5.xlarge",
     "payg_hourly_usd": 0.170, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Dev EC2 — 10 hrs/day → Compute Savings Plan candidates
    {"resource_id": "i-0dev123456789abc0", "resource_name": "aws-dev-sandbox-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "us-east-1", "os": "Linux", "sku": "t3.medium",
     "payg_hourly_usd": 0.0416, "avg_daily_running_hours": 10,
     "subscription": "acc-aws-99887766", "provider": "AWS", "is_orphaned": False},

    {"resource_id": "i-0dev123456789abc1", "resource_name": "aws-dev-test-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "us-west-2", "os": "Linux", "sku": "t3.micro",
     "payg_hourly_usd": 0.0104, "avg_daily_running_hours": 10,
     "subscription": "acc-aws-99887766", "provider": "AWS", "is_orphaned": False},

    # Stopped EC2 — Orphaned (RI active but instance is stopped)
    {"resource_id": "i-0legacy123456789a", "resource_name": "aws-legacy-batch",
     "resource_type": "Compute", "resource_state": "Stopped (deallocated)",
     "region": "us-east-1", "os": "Linux", "sku": "c5.xlarge",
     "payg_hourly_usd": 0.170, "avg_daily_running_hours": 0,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": True},
]


# ── AWS Database Inventory (RDS / DynamoDB) ────────────────────────────────────

AWS_DATABASE_INVENTORY = [
    # AWS RDS PostgreSQL — db.m5.large
    {"resource_id": "rds-prod-postgres-01", "resource_name": "prod-orders-rds",
     "resource_type": "AWS RDS PostgreSQL",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "db.m5.large",
     "payg_hourly_usd": 0.176, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # AWS RDS MySQL — db.t3.medium
    {"resource_id": "rds-prod-mysql-01", "resource_name": "prod-cms-rds",
     "resource_type": "AWS RDS MySQL",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "db.t3.medium",
     "payg_hourly_usd": 0.068, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Amazon Aurora PostgreSQL — db.r5.large
    {"resource_id": "aurora-prod-cluster-01", "resource_name": "prod-aurora-cluster",
     "resource_type": "Amazon Aurora",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "db.r5.large",
     "payg_hourly_usd": 0.290, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Amazon DynamoDB — Provisioned Throughput
    {"resource_id": "dynamo-prod-table-01", "resource_name": "prod-user-sessions",
     "resource_type": "Amazon DynamoDB",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "100_WCU_100_RCU",
     "payg_hourly_usd": 0.065, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},
]


# ── AWS RI-Only / Reserved Node Inventory ──────────────────────────────────────

AWS_RI_ONLY_INVENTORY = [
    # Amazon ElastiCache Redis Node — cache.m5.large
    {"resource_id": "elasticache-prod-01", "resource_name": "prod-cache-redis",
     "resource_type": "Amazon ElastiCache",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "cache.m5.large",
     "payg_hourly_usd": 0.136, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Amazon Redshift Cluster — dc2.large
    {"resource_id": "redshift-prod-cluster-01", "resource_name": "prod-dw-redshift",
     "resource_type": "Amazon Redshift",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "dc2.large",
     "payg_hourly_usd": 0.250, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Amazon OpenSearch — r5.large.search
    {"resource_id": "opensearch-prod-01", "resource_name": "prod-logs-opensearch",
     "resource_type": "Amazon OpenSearch",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "r5.large.search",
     "payg_hourly_usd": 0.180, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},
]

AWS_INVENTORY = AWS_COMPUTE_INVENTORY + AWS_DATABASE_INVENTORY + AWS_RI_ONLY_INVENTORY


# ── AWS Commitments ────────────────────────────────────────────────────────────

AWS_COMMITMENTS = [
    # ── Reserved Instances — EC2 ────────────────────────────────────────────────
    {"commitment_id": "RI-EC2-M5-LARGE-USE1-LIN",
     "commitment_type": "Reserved Instance",
     "scope_sku": "m5.large", "scope_region": "us-east-1", "scope_os": "Linux",
     "hourly_usd_commitment": 0.060, "reserved_qty": 2,
     "term": "1-year", "expiry_date": "2026-12-01", "provider": "AWS"},

    {"commitment_id": "RI-EC2-C5-XLARGE-USE1-LIN",
     "commitment_type": "Reserved Instance",
     "scope_sku": "c5.xlarge", "scope_region": "us-east-1", "scope_os": "Linux",
     "hourly_usd_commitment": 0.110, "reserved_qty": 3,
     "term": "1-year", "expiry_date": "2026-10-15", "provider": "AWS"},

    # ── Reserved Instance — RDS Database ────────────────────────────────────────
    {"commitment_id": "RI-RDS-M5-LARGE-USE1",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "db.m5.large", "scope_region": "us-east-1", "scope_os": "N/A",
     "hourly_usd_commitment": 0.115,
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2027-01-01", "provider": "AWS"},

    # ── Reserved Instance — ElastiCache Node ────────────────────────────────────
    {"commitment_id": "RI-ELASTICACHE-M5-LARGE-USE1",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "cache.m5.large", "scope_region": "us-east-1", "scope_os": "N/A",
     "hourly_usd_commitment": 0.088,
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-11-20", "provider": "AWS"},

    # ── AWS Compute Savings Plan (flexible $/hr across EC2, Fargate, Lambda) ───
    {"commitment_id": "SP-AWS-COMPUTE-001",
     "commitment_type": "Compute Savings Plan",
     "scope_sku": "Any Compute", "scope_region": "Global", "scope_os": "Any",
     "hourly_usd_commitment": 0.35,
     "reserved_qty": 0, "term": "1-year", "expiry_date": "2027-02-10", "provider": "AWS"},

    # ── AWS EC2 Instance Savings Plan (scoped to family in region) ────────────
    {"commitment_id": "SP-AWS-EC2-INSTANCE-001",
     "commitment_type": "EC2 Instance Savings Plan",
     "scope_sku": "m5 family", "scope_region": "us-east-1", "scope_os": "Linux",
     "hourly_usd_commitment": 0.15,
     "reserved_qty": 0, "term": "1-year", "expiry_date": "2027-04-01", "provider": "AWS"},
]


# ── AWS Eligibility & Coverage Notes ──────────────────────────────────────────

AWS_COMPUTE_SP_TYPES = {"Compute", "AWS Lambda", "AWS Fargate"}
# Note: AWS does NOT have a Database Savings Plan. RDS uses RDS Reserved Instances.
# EC2 Instance Savings Plans cover specific EC2 families within a region.
AWS_DATABASE_SP_TYPES = {"Compute"}  # Used for EC2 Instance Savings Plans pool

AWS_RI_COVERAGE_NOTES = {
    "Compute":              ("EC2 On-Demand hourly compute rate (Standard: fixed family; Convertible: exchangeable family)", "EBS volumes, data transfer, OS licensing surcharges"),
    "AWS RDS PostgreSQL":   ("RDS DB instance hourly compute capacity (Single-AZ or Multi-AZ)", "Storage (GB-month), provisioned IOPS, automated backups"),
    "AWS RDS MySQL":        ("RDS DB instance hourly compute capacity (size-flexible within family)", "Storage, IOPS, automated backup storage"),
    "Amazon Aurora":        ("Aurora DB cluster instance compute capacity", "Storage per GB-month, I/O rate charges"),
    "Amazon DynamoDB":      ("Provisioned WCU & RCU throughput capacity", "Data storage per GB, backup/restore, data transfer"),
    "Amazon ElastiCache":   ("Cache node hourly compute capacity (Redis / Memcached)", "Data storage overhead, snapshot backups"),
    "Amazon Redshift":      ("Redshift data warehouse node compute capacity", "Storage exceeding node capacity, concurrency scaling"),
    "Amazon OpenSearch":    ("Search cluster node instance compute capacity", "EBS volume storage, snapshot storage"),
}


def seed_aws_if_empty(engine=None):
    if engine is None:
        engine = get_engine("AWS", "demo")
    init_db("AWS", "demo")
    with Session(engine) as session:
        if session.query(CloudInventory).filter_by(provider="AWS").count() == 0:
            session.bulk_insert_mappings(CloudInventory, AWS_INVENTORY)
            session.bulk_insert_mappings(Commitment, AWS_COMMITMENTS)
            session.commit()
            print(f"[OK] Seeded AWS database with {len(AWS_INVENTORY)} resources, {len(AWS_COMMITMENTS)} commitments.")
        else:
            print("[INFO] AWS database already seeded -- skipping.")


if __name__ == "__main__":
    seed_aws_if_empty()
