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

    # Amazon DocumentDB — db.r5.large. Added 2026-08-22 alongside the live
    # fetch (aws/connector.py) built for it - real IAM action confirmed as
    # plain rds:DescribeDBInstances (shared control plane with RDS), no
    # Reserved Instance API exists, Database-SP-eligible only.
    {"resource_id": "docdb-prod-cluster-01", "resource_name": "prod-catalog-docdb",
     "resource_type": "Amazon DocumentDB",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "db.r5.large",
     "payg_hourly_usd": 0.277, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Amazon DocumentDB Serverless - a genuinely different resource type
    # from provisioned DocumentDB above (billed per-DCU-hour, not by a
    # named instance class). Added 2026-08-23 alongside its new live fetch
    # (aws/connector.py) - real rate is MinCapacity (2 DCU here) x the real
    # $0.0822/DCU-hr Standard On-Demand rate, confirmed against real
    # downloaded AmazonDocDB price list data (productFamily "Serverless").
    # No Reserved Instance concept exists (same as provisioned DocumentDB),
    # Database-SP-eligible only - confirmed real via AWS's Database Savings
    # Plans announcement, which explicitly calls out Serverless coverage.
    {"resource_id": "docdb-serverless-prod-events-01", "resource_name": "prod-events-docdb-serverless",
     "resource_type": "Amazon DocumentDB Serverless",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "2DCU-min",
     "payg_hourly_usd": 0.1644, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Amazon Neptune — db.r5.large. Same real-IAM/no-RI facts as DocumentDB above.
    {"resource_id": "neptune-prod-cluster-01", "resource_name": "prod-graph-neptune",
     "resource_type": "Amazon Neptune",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "db.r5.large",
     "payg_hourly_usd": 0.348, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # AWS DMS Replication Instance — dms.t3.medium. Unlike DocumentDB/Neptune,
    # DMS genuinely has its own Single-AZ/Multi-AZ price split (confirmed
    # via real AWSDatabaseMigrationSvc price list data) and its own
    # dms:DescribeReplicationInstances IAM action (no dedicated AWS-managed
    # read-only policy exists for it - see REQUIRED_AWS_POLICIES). No
    # Reserved Instance API either - Database-SP-eligible only.
    {"resource_id": "dms-prod-repl-01", "resource_name": "prod-migration-replica",
     "resource_type": "AWS DMS Replication Instance",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "dms.t3.medium",
     "payg_hourly_usd": 0.0745, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Amazon Keyspaces — provisioned-throughput table (mirrors DynamoDB's
    # own SKU convention: this app's live fetch encodes provisioned RCU/WCU
    # into the SKU string since neither service has a literal AWS "SKU" the
    # way EC2/RDS do - see aws/connector.py). Real IAM action is
    # cassandra:Select (confirmed via AWS's Service Authorization Reference -
    # Keyspaces' own IAM actions still use the historical "cassandra:"
    # prefix, not "keyspaces:"), granted via AmazonKeyspacesReadOnlyAccess.
    {"resource_id": "keyspaces-prod-table-01", "resource_name": "prod-events-keyspaces",
     "resource_type": "Amazon Keyspaces",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "50RCU-50WCU",
     "payg_hourly_usd": 0.039, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},
]

# ── AWS Fargate (Compute Savings Plan eligible, not RI-eligible) ──────────────
# Modeled separately from AWS_COMPUTE_INVENTORY (which is EC2-only) since
# Fargate tasks are billed per-vCPU-hour + per-GB-hour of the task's own
# configured cpu/memory, not a named instance type - SKU encodes that
# directly (e.g. "0.5vCPU-1GB"), matching aws/connector.py's live fetch
# convention. Confirmed via AWS's own Savings Plans docs that Fargate is
# Compute-Savings-Plan-eligible, and via boto3/ECS's API that it has no
# Reserved Instance concept at all.
AWS_FARGATE_INVENTORY = [
    {"resource_id": "arn:aws:ecs:us-east-1:111122223333:task/prod-cluster/fargate-api-01",
     "resource_name": "prod-api-fargate-01",
     "resource_type": "AWS Fargate",
     "resource_state": "Running",
     "region": "us-east-1", "os": "Linux", "sku": "0.5vCPU-1GB",
     "payg_hourly_usd": 0.024685, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},
]


# ── AWS RI-Only / Reserved Node Inventory ──────────────────────────────────────

AWS_RI_ONLY_INVENTORY = [
    # Amazon ElastiCache for Redis Node — cache.m5.large. Resource type
    # split by engine 2026-08-23 (was flat "Amazon ElastiCache") after
    # confirming Database Savings Plans only cover ElastiCache for Valkey -
    # rate corrected to the real On-Demand Redis rate for this instance
    # type/region ($0.156/hr, confirmed against real downloaded
    # AmazonElastiCache price list data - was a rounded $0.136 estimate
    # from before this app had any ElastiCache pricing lookup at all).
    {"resource_id": "elasticache-prod-01", "resource_name": "prod-cache-redis",
     "resource_type": "Amazon ElastiCache for Redis",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "cache.m5.large",
     "payg_hourly_usd": 0.156, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Amazon ElastiCache for Valkey Node — cache.m5.large. Added 2026-08-23
    # specifically so the demo Database SP pool has a real Valkey resource
    # to show as eligible (the Redis node above correctly is NOT database-
    # SP-eligible, only RI-eligible).
    {"resource_id": "elasticache-prod-valkey-01", "resource_name": "prod-cache-valkey",
     "resource_type": "Amazon ElastiCache for Valkey",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "cache.m5.large",
     "payg_hourly_usd": 0.1248, "avg_daily_running_hours": 24,   # real On-Demand Valkey rate for cache.m5.large in us-east-1, confirmed against real downloaded price list data - genuinely cheaper than Redis at the identical instance type.
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Amazon MemoryDB for Redis Node — db.r6g.large. New service, added
    # 2026-08-23 - real Reserved Nodes product (confirmed via boto3), no
    # Savings Plan coverage (absent from the official Database Savings
    # Plans eligible-services table). Resource type kept flat "Amazon
    # MemoryDB" (not split by engine) - see aws/connector.py's MemoryDB
    # inventory block for why (the Reserved Node purchase record can't
    # specify engine, so splitting demand but not supply would permanently
    # break coverage matching).
    {"resource_id": "memorydb-prod-01", "resource_name": "prod-sessions-memorydb (Redis)",
     "resource_type": "Amazon MemoryDB",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "db.r6g.large",
     "payg_hourly_usd": 0.309, "avg_daily_running_hours": 24,   # real On-Demand Redis rate for db.r6g.large in us-east-1, confirmed against real downloaded AmazonMemoryDB price list data.
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
     "payg_hourly_usd": 0.186, "avg_daily_running_hours": 24,   # exact real AmazonES On-Demand rate for r5.large.search in us-east-1, confirmed 2026-08-22 - was a rounded $0.180 estimate before OpenSearch's live pricing lookup existed.
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},
]

# ── Amazon SageMaker AI (SageMaker Savings Plan eligible, not RI-eligible) ────
# Added 2026-08-23 after confirming feasibility: unlike classic Lambda (no
# running-resource state at all) or Lambda Managed Instances (RI/SP-eligible
# but AWS exposes no per-instance visibility, only pool-level CloudWatch
# aggregates), SageMaker's "always-on" resource types genuinely fit this
# app's inventory model - real, listable, persistent resources with an
# instance type and a running/stopped state:
#   - Real-Time Inference Endpoints (sagemaker:ListEndpoints/DescribeEndpoint)
#   - Notebook Instances (sagemaker:ListNotebookInstances/DescribeNotebookInstance)
# Training/Processing/Data Wrangler/Batch Transform jobs are deliberately NOT
# modeled - they're one-shot ephemeral executions with no persistent
# identity to track as an inventory row (same category as Lambda invocations
# or Glue jobs), not merely excluded from the SP baseline the way a
# business-hours VM would be.
# SageMaker AI Savings Plans are a genuinely separate, first-class Savings
# Plan type in AWS's own API - confirmed via boto3's savingsplans client
# service model, whose savingsPlanType enum is literally
# 'Compute'|'EC2Instance'|'SageMaker'|'Database' - not a Compute-SP subtype
# and not folded into it here. No Reserved Instance concept exists for
# SageMaker (confirmed: no ec2-style DescribeReservedInstances-equivalent
# anywhere in the sagemaker boto3 service model).
AWS_SAGEMAKER_INVENTORY = [
    # Real-Time Inference Endpoint - ml.m5.xlarge. Rate is the real live
    # AmazonSageMaker Price List On-Demand "Hosting" rate for us-east-1,
    # confirmed 2026-08-23 against real downloaded price list data
    # (component="Hosting", NOT the Studio/Training/Notebook components that
    # share the same instanceType attribute pattern in that data).
    {"resource_id": "sagemaker-endpoint-prod-fraud-model", "resource_name": "prod-fraud-detection-endpoint",
     "resource_type": "Amazon SageMaker Endpoint",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "ml.m5.xlarge",
     "payg_hourly_usd": 0.23, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},

    # Notebook Instance - ml.t3.medium. Same real-price-list-confirmed rate
    # (component="Notebook", distinct from the "Studio-Notebook" component
    # which shares the identical instanceType+region combination but is a
    # separate SKU/price entry - confirmed against real downloaded data).
    {"resource_id": "sagemaker-notebook-prod-data-science-01", "resource_name": "prod-datascience-notebook",
     "resource_type": "Amazon SageMaker Notebook Instance",
     "resource_state": "Running",
     "region": "us-east-1", "os": "N/A", "sku": "ml.t3.medium",
     "payg_hourly_usd": 0.05, "avg_daily_running_hours": 24,
     "subscription": "acc-aws-11223344", "provider": "AWS", "is_orphaned": False},
]

AWS_INVENTORY = AWS_COMPUTE_INVENTORY + AWS_DATABASE_INVENTORY + AWS_RI_ONLY_INVENTORY + AWS_FARGATE_INVENTORY + AWS_SAGEMAKER_INVENTORY


# ── AWS Commitments ────────────────────────────────────────────────────────────

AWS_COMMITMENTS = [
    # ── Reserved Instances — EC2 ────────────────────────────────────────────────
    {"commitment_id": "RI-EC2-M5-LARGE-USE1-LIN",
     "commitment_type": "Reserved Instance",
     "scope_sku": "m5.large", "scope_region": "us-east-1", "scope_os": "Linux",
     "hourly_usd_commitment": 0.060, "reserved_qty": 2,
     "term": "1-year", "expiry_date": "2026-12-01", "provider": "AWS",
     "offering_class": "standard"},

    {"commitment_id": "RI-EC2-C5-XLARGE-USE1-LIN",
     "commitment_type": "Reserved Instance",
     "scope_sku": "c5.xlarge", "scope_region": "us-east-1", "scope_os": "Linux",
     "hourly_usd_commitment": 0.110, "reserved_qty": 3,
     "term": "1-year", "expiry_date": "2026-10-15", "provider": "AWS",
     "offering_class": "convertible"},

    # ── Reserved Instance — RDS Database ────────────────────────────────────────
    {"commitment_id": "RI-RDS-M5-LARGE-USE1",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "db.m5.large", "scope_region": "us-east-1", "scope_os": "N/A",
     "hourly_usd_commitment": 0.115,
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2027-01-01", "provider": "AWS"},

    # ── Reserved Instance — ElastiCache Node ────────────────────────────────────
    # scope_resource_type set EXPLICITLY (unlike this demo file's other RI
    # entries, which mostly leave it blank) - reservation_analysis() treats
    # a blank scope_resource_type as "matches any Resource Type" (see
    # analysis/engine.py's supply-side merge), which was harmless while
    # ElastiCache was one flat resource type but would have ambiguously
    # matched BOTH the new Redis and Valkey demo rows below (same SKU,
    # cache.m5.large) once the 2026-08-23 engine split landed - explicit
    # here to keep this RI correctly scoped to Redis only, not "any engine".
    {"commitment_id": "RI-ELASTICACHE-M5-LARGE-USE1",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "cache.m5.large", "scope_region": "us-east-1", "scope_os": "N/A",
     "scope_resource_type": "Amazon ElastiCache for Redis",
     "hourly_usd_commitment": 0.088,
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-11-20", "provider": "AWS"},

    # ── Reserved Instance — MemoryDB Node ────────────────────────────────────
    # Added 2026-08-23 alongside MemoryDB's new live fetch (aws/connector.py).
    {"commitment_id": "RI-MEMORYDB-R6G-LARGE-USE1",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "db.r6g.large", "scope_region": "us-east-1", "scope_os": "N/A",
     "scope_resource_type": "Amazon MemoryDB",
     "hourly_usd_commitment": 0.195,   # ~37% off the $0.309/hr On-Demand Redis rate, in line with MemoryDB's real 1yr No-Upfront RI discount range - illustrative, not a live-fetched rate.
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2027-02-01", "provider": "AWS"},

    # ── Reserved Instance — OpenSearch Node ──────────────────────────────────
    # Added 2026-08-22 alongside OpenSearch's new live RI fetch
    # (aws/connector.py) - previously OpenSearch had demo inventory and was
    # listed as RI-eligible, but no demo commitment existed to show what
    # "Fully Covered" looks like for it.
    {"commitment_id": "RI-OPENSEARCH-R5-LARGE-USE1",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "r5.large.search", "scope_region": "us-east-1", "scope_os": "N/A",
     "hourly_usd_commitment": 0.121,   # ~35% off the $0.186/hr On-Demand rate, in line with OpenSearch's real 1yr No-Upfront RI discount range - illustrative, not a live-fetched rate.
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2027-01-10", "provider": "AWS"},

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

    # ── AWS Database Savings Plan (Aurora/RDS/DynamoDB/ElastiCache/DocumentDB) ─
    # Added 2026-08-22 after confirming Database Savings Plans are a real AWS
    # product (see AWS_DATABASE_SP_TYPES's comment above) - previously the
    # demo "database" SP pool only ever showed EC2 Instance Savings Plan
    # (SP-AWS-EC2-INSTANCE-001, now correctly moved to the Compute bucket),
    # which isn't a database commitment at all and left this pool
    # misleadingly non-empty with the wrong content.
    {"commitment_id": "SP-AWS-DATABASE-001",
     "commitment_type": "Database Savings Plan",
     "scope_sku": "Any Database", "scope_region": "Global", "scope_os": "N/A",
     "hourly_usd_commitment": 0.20,
     "reserved_qty": 0, "term": "1-year", "expiry_date": "2027-03-15", "provider": "AWS"},

    # ── AWS SageMaker Savings Plan (flexible $/hr across all SageMaker AI usage) ─
    # Added 2026-08-23 - a genuinely separate SP type (see AWS_SAGEMAKER_SP_TYPES
    # below), not part of the Compute or Database pools above.
    {"commitment_id": "SP-AWS-SAGEMAKER-001",
     "commitment_type": "SageMaker Savings Plan",
     "scope_sku": "Any SageMaker", "scope_region": "Global", "scope_os": "N/A",
     "hourly_usd_commitment": 0.15,
     "reserved_qty": 0, "term": "1-year", "expiry_date": "2027-05-01", "provider": "AWS"},
]


# ── AWS Eligibility & Coverage Notes ──────────────────────────────────────────

AWS_COMPUTE_SP_TYPES = {"Compute", "AWS Lambda", "AWS Fargate"}
# CORRECTED 2026-08-22: AWS genuinely DOES have a Database Savings Plan -
# confirmed via AWS's own FAQ (https://aws.amazon.com/savingsplans/faqs/)
# and, more authoritatively, the full AWS Savings Plans User Guide (PDF,
# checked 2026-08-22): "Database Savings Plans provide flexibility to use
# AWS database services while reducing costs by up to 35% on Aurora, RDS,
# DynamoDB, ElastiCache, DocumentDB, Timestream, Neptune, Keyspaces, DMS,
# and Amazon OpenSearch Service." This is a WIDER list than the FAQ page
# alone implied - the FAQ's shorter list (Aurora/RDS/DynamoDB/ElastiCache/
# DocumentDB only) had led to OpenSearch being wrongly excluded in an
# earlier pass of this fix; corrected here after reading the full guide.
# Redshift is still NOT covered by either source - confirmed absent from
# both lists, not an oversight.
# Previously this set was {"Compute"} (i.e. EC2) on the theory that AWS
# had no database-specific plan and EC2 Instance Savings Plans were the
# closest analog - that theory is now confirmed wrong, and
# pricing/aws_commitment_mapping.py / commitments/existing_commitments.py
# were corrected in the same round to stop bucketing EC2 Instance Savings
# Plans as "database" commitments.
# Timestream aren't listed below - this app still tracks no inventory
# resource type for it (fully usage-based billing per byte ingested/
# stored/scanned, confirmed via boto3's timestream-write service model to
# have no instance class or capacity-unit concept at all, unlike
# DocumentDB/Neptune/DMS/Keyspaces below - structurally incompatible with
# this app's resource model, not just unbuilt yet).
# DocumentDB/Neptune/DMS/Keyspaces ADDED 2026-08-22 alongside their new live
# fetch (aws/connector.py) and pricing lookups (pricing/aws_price_list.py) -
# confirmed real inventory resource types now exist for all four.
# Both real API resource_type conventions ARE listed for the services this
# app does track, since demo seed data (this file, below) and the real
# live fetch (aws/connector.py's _RDS_ENGINE_LABELS) use different naming
# ("AWS RDS MySQL" vs "Amazon RDS for MySQL") - a separate, pre-existing
# demo/live naming inconsistency also affecting AWS_RI_COVERAGE_NOTES
# below, not fully resolved here.
# CORRECTED 2026-08-23: flat "Amazon ElastiCache" removed, replaced with
# "Amazon ElastiCache for Valkey" ONLY - confirmed via the actual Database
# Savings Plans pricing table (aws.amazon.com/savingsplans/database-pricing),
# which explicitly lists "ElastiCache for Valkey Instances"/"...Serverless"
# and nothing for Redis or Memcached. The flat entry had wrongly treated
# ALL ElastiCache resources as Database-SP-eligible regardless of engine -
# Redis/Memcached remain Reserved-Instance-eligible only (see
# AWS_RI_COVERAGE_NOTES below), never Database-SP-eligible.
AWS_DATABASE_SP_TYPES = {
    "AWS RDS PostgreSQL", "AWS RDS MySQL", "Amazon Aurora", "Amazon DynamoDB", "Amazon ElastiCache for Valkey", "Amazon OpenSearch",   # demo seed naming
    "Amazon RDS for MySQL", "Amazon RDS for PostgreSQL", "Amazon RDS for MariaDB",                      # live fetch naming
    "Amazon RDS for Oracle", "Amazon RDS for SQL Server", "Amazon Aurora (MySQL)", "Amazon Aurora (PostgreSQL)",
    "Amazon DocumentDB", "Amazon Neptune", "AWS DMS Replication Instance", "Amazon Keyspaces",
    "Amazon DocumentDB Serverless",   # added 2026-08-23 - confirmed real via AWS's Database Savings Plans announcement, which explicitly calls out Serverless coverage alongside provisioned.
}

# SageMaker AI Savings Plans - a genuinely separate, first-class Savings Plan
# type (boto3 savingsplans client's savingsPlanType enum: 'Compute'|
# 'EC2Instance'|'SageMaker'|'Database'), not a Compute-SP subtype. Applies to
# ALL SageMaker AI instance usage "regardless of instance family, size,
# Region, or component" per AWS's own SP pricing page - no per-SKU/tier
# eligibility restriction the way e.g. an Azure App Service Basic plan has,
# so unlike compute_sp_eligible_types/db_eligible_types above, every
# resource_type entry here is unconditionally eligible.
AWS_SAGEMAKER_SP_TYPES = {"Amazon SageMaker Endpoint", "Amazon SageMaker Notebook Instance"}

AWS_RI_COVERAGE_NOTES = {
    "Compute":              ("EC2 On-Demand hourly compute rate (Standard: fixed family; Convertible: exchangeable family)", "EBS volumes, data transfer, OS licensing surcharges"),
    "AWS RDS PostgreSQL":   ("RDS DB instance hourly compute capacity (Single-AZ or Multi-AZ)", "Storage (GB-month), provisioned IOPS, automated backups"),
    "AWS RDS MySQL":        ("RDS DB instance hourly compute capacity (size-flexible within family)", "Storage, IOPS, automated backup storage"),
    "Amazon Aurora":        ("Aurora DB cluster instance compute capacity", "Storage per GB-month, I/O rate charges"),
    # Reserved Capacity is a real, current DynamoDB product (confirmed via
    # AWS's own docs - up to 54%/77% off) - genuinely eligible, NOT
    # excluded. But it's purchased and viewed Console-only: dynamodb:
    # DescribeReservedCapacity is a real IAM action with no SDK/CLI/API
    # binding at all, confirmed via multiple AWS SDKs' own tracked GitHub
    # issues since 2020. This app has no way to fetch what a tenant already
    # owns, so any gap shown for DynamoDB should be treated as unverified,
    # not a literal purchase recommendation - see the caveat in
    # _render_ri_coverage_tab().
    "Amazon DynamoDB":      ("Provisioned WCU & RCU throughput capacity", "Data storage per GB, backup/restore, data transfer, AND the reservation itself - purchase data isn't fetchable via any API (Console-only)"),
    # Split by engine 2026-08-23 (was one flat "Amazon ElastiCache" row) -
    # all three engines remain equally RI-eligible (Reserved Instance
    # discounts apply the same way regardless of engine), only the
    # Database Savings Plan side is Valkey-specific (see the "excludes"
    # column and AWS_DATABASE_SP_TYPES above).
    "Amazon ElastiCache for Redis":     ("Cache node hourly compute capacity", "Data storage overhead, snapshot backups. NOT Database-SP-eligible (Redis is RI-only - confirmed via the actual Database Savings Plans pricing table, which lists Valkey only)"),
    "Amazon ElastiCache for Memcached": ("Cache node hourly compute capacity", "Data storage overhead, snapshot backups. NOT Database-SP-eligible (Memcached is RI-only, same as Redis)"),
    "Amazon ElastiCache for Valkey":    ("Cache node hourly compute capacity", "Data storage overhead, snapshot backups. Also Database-SP-eligible (the only ElastiCache engine that is)"),
    "Amazon Redshift":      ("Redshift data warehouse node compute capacity", "Storage exceeding node capacity, concurrency scaling"),
    "Amazon OpenSearch":    ("Search cluster node instance compute capacity", "EBS volume storage, snapshot storage"),
    # New service, added 2026-08-23 - real Reserved Nodes product
    # (confirmed via boto3), NOT Database-SP-eligible (absent from the
    # official Database Savings Plans eligible-services table). Kept flat
    # (not split Redis/Valkey) - the ReservedNode purchase record carries
    # no engine field at all, unlike ElastiCache's, so a split here would
    # create a demand/supply mismatch coverage could never actually match -
    # see aws/connector.py's MemoryDB inventory block for the full reasoning.
    "Amazon MemoryDB":      ("Cache node hourly compute capacity (Redis or Valkey - engine not distinguished, see mapping notes)", "Data storage overhead, snapshot backups"),
    # Added 2026-08-23. PAYG rate here is MinCapacity (the configured DCU
    # floor) x the real $/DCU-hr Standard rate, NOT real-time variable
    # spend - AWS exposes actual live DCU usage only via a CloudWatch
    # time-series metric, not a static describable value, so this app
    # commits against the same conservative floor AWS's own Database
    # Savings Plans guidance recommends. NOT RI-eligible - no Reserved
    # Instance product exists for DocumentDB (Serverless or provisioned).
    "Amazon DocumentDB Serverless": ("DCU-hour capacity floor (MinCapacity, see mapping notes - not live variable usage)", "Storage per GB-month, I/O charges (Standard config), real-time DCU usage above the MinCapacity floor stays on-demand"),
    # Added 2026-08-23. No Reserved Instance concept exists for SageMaker at
    # all (confirmed: no equivalent of DescribeReservedInstances anywhere in
    # the sagemaker boto3 service model) - SageMaker-SP-eligible only, never
    # shown in the RI Coverage tab.
    "Amazon SageMaker Endpoint":          ("Real-time inference endpoint instance hourly compute capacity", "Data processed, model storage. NOT RI-eligible - no Reserved Instance product exists for SageMaker, SageMaker-SP-eligible only"),
    "Amazon SageMaker Notebook Instance": ("Notebook instance hourly compute capacity", "EBS volume storage. NOT RI-eligible - no Reserved Instance product exists for SageMaker, SageMaker-SP-eligible only"),
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

    seed_aws_demo_tenant_if_empty()


def seed_aws_demo_tenant_if_empty():
    """AWS counterpart to db/seed.py's seed_demo_tenant_if_empty() - without
    this, list_tenants("AWS", "demo") returns empty and the Dashboard button
    is unreachable in AWS Demo mode (Tenant Management shows "No demo tenant
    seeded yet." with no way to proceed, since "Add a new tenant" is
    Production-only). Confirmed real gap 2026-08-22: Azure demo has always
    had this, AWS never did.

    Deliberately simpler than the Azure version - no TenantSubscription rows
    (AWS has no sub-account-scope concept the way Azure has subscriptions
    under a tenant; one IAM credential set = one account) and no simulated
    tenant_permission_status/tenant_assigned_roles (confirmed via app.py's
    _manage_tenant_dialog: AWS's "Permissions" tab is hard-gated to "Not
    applicable - a demo tenant has no real AWS credentials behind it." for
    is_demo, and the AWS segmented-control tab icons only ever read
    last_sync_status, never tenant_permission_status - unlike Azure's tenant
    list row, which does read it. Setting those fields for AWS would be
    simulating a check the UI never displays.

    aws_account_id is set to AWS's own well-known placeholder account ID
    (used throughout AWS's official docs/examples, e.g. IAM policy
    examples) so the Tenant Management table's "Account ID" column shows
    something realistic rather than "—". Independently guarded (checks its
    own emptiness) so it's safe to call every startup."""
    from db.tenants import list_tenants, upsert_tenant, update_aws_account_id
    if list_tenants("AWS", "demo"):
        return
    tenant_db_id = upsert_tenant(
        provider="AWS", mode="demo", tenant_name="Demo AWS Tenant",
        tenant_id="us-east-1", subscription_id="us-east-1",
        client_id="AKIADEMOHASNOREALCREDS0",
        client_secret="demo-tenant-has-no-real-credentials",
    )
    update_aws_account_id("AWS", "demo", tenant_db_id, "123456789012")
    print("[OK] Seeded demo tenant 'Demo AWS Tenant'.")


if __name__ == "__main__":
    seed_aws_if_empty()
