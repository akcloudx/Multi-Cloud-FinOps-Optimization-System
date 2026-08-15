"""
db/seed.py — Mock Azure infrastructure seed data.

COMPUTE (Savings Plan for Compute  OR  Reserved Instance):
  - Prod VMs 24x7 (Standard_D4ds_v5, D4ds_v4)  → RI candidates
  - Dev  VMs 10 hrs/day (B2ms, B2s)             → Compute SP candidates
  - Stopped legacy VM with active RI             → Orphaned

DATABASES (Savings Plan for Databases — 1-year ONLY  AND/OR  Reserved Capacity):
  Azure SQL Database, SQL Managed Instance, PostgreSQL, MySQL, Cosmos DB

RI-ONLY SERVICES (Reserved Capacity only — no SP coverage):
  Azure Blob Storage, Azure Files, Azure Synapse Analytics,
  Azure Databricks, Azure Cache for Redis, Azure Disk Storage

COMMITMENTS:
  - 2 × Reserved Instances for Compute (VM SKU-level)
  - 1 × Reserved Capacity for Azure SQL Database
  - 1 × Reserved Capacity for Cosmos DB
  - 1 × Reserved Capacity for Azure Blob Storage
  - 1 × Savings Plan for Compute (1-yr or 3-yr)
  - 1 × Savings Plan for Databases (1-year ONLY per Azure policy)

Reference: https://learn.microsoft.com/en-us/azure/cost-management-billing/reservations/save-compute-costs-reservations
"""

import sys, os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy.orm import Session
from db.schema import (
    init_db, get_engine, CloudInventory, Commitment,
    ReservationPurchase, SavingsPlanPurchase,
)


# ── Compute Inventory ──────────────────────────────────────────────────────────

COMPUTE_INVENTORY = [
    # Prod VMs — 24x7 → Reserved Instance OR Compute SP candidates
    {"resource_id": "VM-Prod-01", "resource_name": "app-server-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_D4ds_v5",
     "payg_hourly_usd": 0.284, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    {"resource_id": "VM-Prod-02", "resource_name": "app-server-02",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_D4ds_v5",
     "payg_hourly_usd": 0.284, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    {"resource_id": "VM-Prod-03", "resource_name": "batch-worker-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_D4ds_v4",
     "payg_hourly_usd": 0.284, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    {"resource_id": "VM-Prod-04", "resource_name": "batch-worker-02",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_D4ds_v4",
     "payg_hourly_usd": 0.284, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    {"resource_id": "VM-Prod-05", "resource_name": "reporting-svc",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_D4ds_v4",
     "payg_hourly_usd": 0.284, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Dev VMs — 10 hrs/day → Savings Plan for Compute candidates
    {"resource_id": "VM-Dev-01", "resource_name": "dev-sandbox-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_B2ms",
     "payg_hourly_usd": 0.106, "avg_daily_running_hours": 10,
     "subscription": "sub-dev-002", "provider": "Azure", "is_orphaned": False},

    {"resource_id": "VM-Dev-02", "resource_name": "dev-sandbox-02",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiasoutheast", "os": "Windows", "sku": "Standard_B2s",
     "payg_hourly_usd": 0.053, "avg_daily_running_hours": 10,
     "subscription": "sub-dev-002", "provider": "Azure", "is_orphaned": False},

    # Stopped — Orphaned (RI active but VM is deallocated)
    {"resource_id": "VM-Legacy-01", "resource_name": "legacy-server-01",
     "resource_type": "Compute", "resource_state": "Stopped (deallocated)",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_D4ds_v4",
     "payg_hourly_usd": 0.284, "avg_daily_running_hours": 0,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": True},
]


# ── Database Inventory (Savings Plan for Databases AND/OR Reserved Capacity) ───
# Both SP and RI can cover these services.
# SP = flexible $/hr pool (1-year only)
# RI = reserved capacity — covers COMPUTE COSTS ONLY (not storage/networking/licensing)

DATABASE_INVENTORY = [
    # Azure SQL Database — General Purpose, 4 vCores
    # RI covers: compute costs only. Not: software license, networking, storage.
    {"resource_id": "SQLDB-Prod-01", "resource_name": "prod-orders-sqldb",
     "resource_type": "Azure SQL Database",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Gen5_4",
     "redundancy": "Locally Redundant",
     "payg_hourly_usd": 0.526, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Database — same SKU/region as SQLDB-Prod-01 above, but Zone
    # Redundant - deliberately included to prove the redundancy-aware
    # pricing pipeline end-to-end. payg_hourly_usd is the REAL live-fetched
    # Zone-Redundant rate ($0.434732/hr, verified 2026-08), genuinely
    # CHEAPER than the Standard variant's $0.724552/hr for this SKU - not a
    # typo, Zone-Redundant meters aren't simply "Standard + surcharge".
    {"resource_id": "SQLDB-Prod-02", "resource_name": "prod-payments-sqldb-zr",
     "resource_type": "Azure SQL Database",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Gen5_4",
     "redundancy": "Zone Redundant",
     "payg_hourly_usd": 0.434732, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Database — Hyperscale, Premium-series, 4 vCores, WITH 1 High
    # Availability secondary replica. Exercises NEW billing dimension
    # (2026-08): each HA replica bills at the SAME per-vCore rate as the
    # primary Compute meter (verified live against the real Azure pricing
    # calculator - a database with 1 replica costs exactly 2x one with 0),
    # confirmed via the real ARM property properties.highAvailabilityReplicaCount
    # (0-4, Hyperscale/Business Critical only - see analysis/commitment_economics.py's
    # _hyperscale_replica_multiplier). payg_hourly_usd is the real
    # live-fetched PRIMARY-only rate ($0.73064/hr, eastus) - the multiplier
    # is applied on top of this by the pricing pipeline, not baked in here,
    # so this row also proves the multiplier logic is live and correct
    # (not silently a no-op because it was pre-multiplied in the seed data).
    {"resource_id": "SQLDB-Prod-03", "resource_name": "prod-catalog-sqldb-hs-ha",
     "resource_type": "Azure SQL Database",
     "resource_state": "Running",
     "region": "eastus", "os": "N/A", "sku": "HS_Premium_4",
     "redundancy": "Locally Redundant", "ha_replica_count": 1,
     "payg_hourly_usd": 0.73064, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Managed Instance — General Purpose, 8 vCores
    # RI covers: compute costs only. Not: software license, networking, storage.
    {"resource_id": "SQLMI-Prod-01", "resource_name": "prod-reporting-sqlmi",
     "resource_type": "Azure SQL Managed Instance",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Gen5_8",
     "redundancy": "Locally Redundant",
     "payg_hourly_usd": 1.008, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Managed Instance — Business Critical, Premium-series, 4 vCores,
    # Zone Redundant. Exercises NEW hardware-generation support (2026-08):
    # Premium-series splits Zone-Redundant pricing into a genuinely separate
    # armSkuName ("SQLMI_BC_Compute_Premium_ZR", not a meterName-tagged
    # variant of the base SKU the way Gen5 works) - see pricing/sku_mapping.py's
    # _plan_sql_family. payg_hourly_usd is the real live-fetched eastus2 rate
    # for this exact SKU+redundancy combo: 0.211 $/vCore/hr x 4 vCores.
    {"resource_id": "SQLMI-Prod-02", "resource_name": "prod-finance-sqlmi-premium",
     "resource_type": "Azure SQL Managed Instance",
     "resource_state": "Running",
     "region": "eastus2", "os": "N/A", "sku": "BC_Premium_4",
     "redundancy": "Zone Redundant",
     "payg_hourly_usd": 0.844, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Database for PostgreSQL — General Purpose, 4 vCores
    # RI covers: compute costs only. Not: software, networking, storage.
    {"resource_id": "PG-Prod-01", "resource_name": "prod-analytics-postgres",
     "resource_type": "Azure Database for PostgreSQL",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GeneralPurpose_Standard_D4ds_v5",
     "payg_hourly_usd": 0.488, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Database for MySQL — General Purpose, 4 vCores
    # RI covers: compute costs only. Not: software, networking, storage.
    {"resource_id": "MYSQL-Prod-01", "resource_name": "prod-cms-mysql",
     "resource_type": "Azure Database for MySQL",
     "resource_state": "Running",
     "region": "australiasoutheast", "os": "N/A", "sku": "GeneralPurpose_Standard_D4ds_v5",
     "payg_hourly_usd": 0.47, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Cosmos DB — Standard Provisioned, General Purpose, 400 RU/s
    # RI covers: throughput only. Not: storage, networking.
    {"resource_id": "COSMOS-Prod-01", "resource_name": "prod-catalog-cosmosdb",
     "resource_type": "Azure Cosmos DB",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Standard_GeneralPurpose_400",
     "payg_hourly_usd": 0.0368, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Database Serverless — Dev/Test
    {"resource_id": "SQLDB-Dev-01", "resource_name": "dev-test-serverless-sqldb",
     "resource_type": "Azure SQL Database",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Serverless_4",
     "redundancy": "Locally Redundant",
     "payg_hourly_usd": 0.263, "avg_daily_running_hours": 10,
     "subscription": "sub-dev-002", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Elastic Pool — General Purpose, Gen5, 8 vCores shared across
    # multiple databases. Billed as its own resource (the pool, not the
    # member databases) - verified live (2026-08) that Azure prices vCore
    # Elastic Pools against the exact same Retail API catalog entries as
    # Single Database, so payg_hourly_usd here is the real live-fetched rate
    # (0.181138/vCore-hr x 8), not an illustrative placeholder.
    {"resource_id": "SQLPOOL-Prod-01", "resource_name": "prod-shared-sqlpool",
     "resource_type": "Azure SQL Elastic Pool",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Gen5_8",
     "redundancy": "Locally Redundant",
     "payg_hourly_usd": 1.449104, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},
]


# ── RI-Only Services (Reserved Capacity only — no Savings Plan coverage) ───────
# These services support Azure Reserved Capacity but are NOT covered by any SP.

RI_ONLY_INVENTORY = [
    # Azure Blob Storage — Reserved Capacity (GiB/month)
    # RI covers: storage capacity for Blob and Data Lake Gen2.
    # NOT covered: bandwidth or transaction rates.
    {"resource_id": "BLOB-Prod-01", "resource_name": "prod-data-blobstorage",
     "resource_type": "Azure Blob Storage",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "LRS_Hot_100TB",
     "payg_hourly_usd": 0.188, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Files — Reserved Capacity
    # RI covers: storage capacity for Azure Files.
    # NOT covered: bandwidth or transaction rates (hot/cool tiers).
    {"resource_id": "FILES-Prod-01", "resource_name": "prod-shared-azurefiles",
     "resource_type": "Azure Files",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "LRS_Hot_10TB",
     "payg_hourly_usd": 0.055, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Cache for Redis — Reserved Capacity
    # RI covers: compute costs only. NOT: networking or storage.
    {"resource_id": "REDIS-Prod-01", "resource_name": "prod-session-redis",
     "resource_type": "Azure Cache for Redis",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "C2_Standard",
     "payg_hourly_usd": 0.088, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Cache for Redis Enterprise (aka "Azure Managed Redis") — a
    # genuinely separate ARM resource type/SKU taxonomy from classic Redis
    # above. RI covers: compute costs only. NOT: networking or storage.
    {"resource_id": "REDISENT-Prod-01", "resource_name": "prod-cache-redisent",
     "resource_type": "Azure Cache for Redis Enterprise",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Balanced_B10",
     "payg_hourly_usd": 0.391, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Synapse Analytics — Reserved Capacity (cDWU)
    # RI covers: cDWU usage only. NOT: storage or networking.
    {"resource_id": "SYNAPSE-Prod-01", "resource_name": "prod-dw-synapse",
     "resource_type": "Azure Synapse Analytics",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "DW500c",
     "payg_hourly_usd": 6.000, "avg_daily_running_hours": 10,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Databricks — Reserved Capacity (DBU)
    # RI covers: DBU usage only. NOT: compute, storage, networking.
    {"resource_id": "ADB-Prod-01", "resource_name": "prod-etl-databricks",
     "resource_type": "Azure Databricks",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Premium_DBU",
     "payg_hourly_usd": 0.550, "avg_daily_running_hours": 10,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Disk Storage — Reserved (P30+ Premium SSD only)
    # RI covers: Premium SSD P30 and larger. NOT: other disk types or smaller sizes.
    {"resource_id": "DISK-Prod-01", "resource_name": "prod-vm-disk-p30",
     "resource_type": "Azure Disk Storage",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "P30_Premium_SSD",
     "payg_hourly_usd": 0.140, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},
]

INVENTORY = COMPUTE_INVENTORY + DATABASE_INVENTORY + RI_ONLY_INVENTORY


# ── Commitment Contracts ───────────────────────────────────────────────────────

COMMITMENTS = [
    # ── Reserved Instances — Compute (VM SKU-level) ────────────────────────────
    {"commitment_id": "RI-VM-D4DS-V5-AE-WIN",
     "commitment_type": "Reserved Instance",
     "scope_sku": "Standard_D4ds_v5", "scope_resource_type": "Compute",
     "scope_region": "australiaeast", "scope_os": "Windows", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 0.20, "reserved_qty": 2,
     "term": "1-year", "expiry_date": "2026-11-01", "provider": "Azure"},

    {"commitment_id": "RI-VM-D4DS-V4-AE-WIN",
     "commitment_type": "Reserved Instance",
     "scope_sku": "Standard_D4ds_v4", "scope_resource_type": "Compute",
     "scope_region": "australiaeast", "scope_os": "Windows", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 0.19, "reserved_qty": 3,
     "term": "1-year", "expiry_date": "2026-09-01", "provider": "Azure"},

    # ── Reserved Capacity — Azure SQL Database (covers compute costs ONLY) ─────
    # scope_redundancy="Locally Redundant" - this reservation covers
    # SQLDB-Prod-01 (Standard) specifically, NOT SQLDB-Prod-02 (Zone
    # Redundant, added 2026-08) - they're genuinely different priced meters,
    # so one reservation doesn't cover both. That's intentional: it means
    # SQLDB-Prod-02 correctly shows as an uncovered gap in the RI Coverage
    # tab, which is accurate (it really isn't covered by any reservation).
    {"commitment_id": "RI-SQLDB-GP-GEN5-4-AE",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "GP_Gen5_4", "scope_resource_type": "Azure SQL Database",
     "scope_region": "australiaeast", "scope_os": "N/A", "scope_redundancy": "Locally Redundant",
     "hourly_usd_commitment": 0.340,   # ~35% saving vs 0.526 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-12-01", "provider": "Azure"},

    # ── Reserved Capacity — Azure SQL Elastic Pool (covers pooled compute ONLY,
    #    same rule as Single Database) ───────────────────────────────────────
    {"commitment_id": "RI-SQLPOOL-GP-GEN5-8-AE",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "GP_Gen5_8", "scope_resource_type": "Azure SQL Elastic Pool",
     "scope_region": "australiaeast", "scope_os": "N/A", "scope_redundancy": "Locally Redundant",
     "hourly_usd_commitment": 0.9415525114155251,   # real live-fetched RI 1yr rate, ~35% saving vs 1.449104 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-12-01", "provider": "Azure"},

    # ── Reserved Capacity — Azure Cosmos DB (covers throughput ONLY) ───────────
    {"commitment_id": "RI-COSMOS-400RU-AE",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "Standard_GeneralPurpose_400", "scope_resource_type": "Azure Cosmos DB",
     "scope_region": "australiaeast", "scope_os": "N/A", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 0.023,   # ~37% saving vs 0.0368 PAYG (matches the real ~1yr-reservation
                                        # discount for the smallest 100 RU/s tier - see pricing/sku_mapping.py)
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2027-02-01", "provider": "Azure"},

    # ── Reserved Capacity — Azure Blob Storage (covers storage capacity ONLY) ──
    {"commitment_id": "RI-BLOB-LRS-HOT-AE",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "LRS_Hot_100TB", "scope_resource_type": "Azure Blob Storage",
     "scope_region": "australiaeast", "scope_os": "N/A", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 0.140,   # ~25% saving vs 0.188 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-10-01", "provider": "Azure"},

    # ── Savings Plan for Compute (flexible $/hr pool, 1-yr or 3-yr) ────────────
    # Covers: VMs, App Service, Functions Premium, Container Instances,
    #         Dedicated Host, Container Apps, Azure Spring Apps
    # scope_resource_type=None - this is a pooled $/hr commitment spanning
    # MULTIPLE resource types by design (not tied to one SKU/service), so
    # unlike the per-resource RIs above there's no single Resource Type to
    # assign; analysis/engine.py's per-resource coverage matching doesn't
    # apply to Savings Plans anyway (they're read via
    # commitments/existing_commitments.py's get_compute_savings_plans()).
    {"commitment_id": "SP-COMPUTE-001",
     "commitment_type": "Savings Plan for Compute",
     "scope_sku": "Any Compute", "scope_resource_type": None,
     "scope_region": "Global", "scope_os": "Any", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 0.80,
     "reserved_qty": 0, "term": "1-year", "expiry_date": "2027-01-15", "provider": "Azure"},

    # ── Savings Plan for Databases (flexible $/hr pool, 1-year ONLY) ───────────
    # Covers: SQL DB, SQL MI, SQL Hyperscale, Serverless, PostgreSQL, MySQL,
    #         Cosmos DB, DocumentDB, DMS, SQL Server on VMs/Arc (hourly)
    {"commitment_id": "SP-DATABASE-001",
     "commitment_type": "Savings Plan for Databases",
     "scope_sku": "Any Database", "scope_resource_type": None,
     "scope_region": "Global", "scope_os": "N/A", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 1.20,
     "reserved_qty": 0, "term": "1-year", "expiry_date": "2027-03-01", "provider": "Azure"},
]


# ── Real-schema purchase records (Compute only, for now) ───────────────────────
# Schema-matched field-for-field to Azure's real REST API responses, built
# service-by-service starting with Compute (see MEMORY.md / project notes) -
# every field below is independently verified against Microsoft's own API
# docs, not guessed. Do NOT add other services here until each one is
# verified the same way; a partially-verified row is worse than no row.
#
# Reservation fields verified against:
#   https://learn.microsoft.com/en-us/rest/api/reserved-vm-instances/reservation/get
# Savings Plan fields verified against:
#   https://learn.microsoft.com/en-us/rest/api/billingbenefits/savings-plan/get

RESERVATION_PURCHASES = [
    # Corresponds to Commitment "RI-VM-D4DS-V5-AE-WIN" - covers VM-Prod-01/02
    {
        "reservation_order_id": "a1b2c3d4-0001-4a1a-9c1a-000000000001",
        "reservation_id": "a1b2c3d4-0001-4a1a-9c1a-100000000001",
        "name": "a1b2c3d4-0001-4a1a-9c1a-100000000001",
        "type": "Microsoft.Capacity/reservationOrders/reservations",
        "location": "australiaeast",
        "sku_name": "Standard_D4ds_v5",
        "sku_description": "D4ds v5",
        "reserved_resource_type": "VirtualMachines",
        "instance_flexibility": "On",
        "applied_scope_type": "Single",
        "applied_scope_display_name": "sub-prod-001",
        "applied_scope_subscription_id": "/subscriptions/sub-prod-001",
        "billing_plan": "Upfront",
        "term": "P1Y",
        "quantity": 2,
        "provisioning_state": "Succeeded",
        "renew": False,
        "purchase_date": "2025-11-01",
        "purchase_date_time": "2025-11-01T09:14:02.0000000Z",
        "effective_date_time": "2025-11-01T09:14:02.0000000Z",
        "benefit_start_time": "2025-11-01T09:14:02.0000000Z",
        "expiry_date": "2026-11-01",
        "expiry_date_time": "2026-11-01T09:14:02.0000000Z",
        "utilization_trend": "Up",
        "utilization_1day_pct": 100.0,
        "utilization_7day_pct": 96.5,
        "utilization_30day_pct": 94.2,
        "provider": "Azure",
    },
    # Corresponds to Commitment "RI-VM-D4DS-V4-AE-WIN" - reserved_qty=3 exactly
    # matches the 3 Running D4ds_v4 VMs (Prod-03/04/05), so this reservation
    # itself is fully utilized. VM-Legacy-01 (stopped, same SKU/region/OS) is
    # a SEPARATE, additional D4ds_v4 resource beyond what's reserved here -
    # analysis/engine.py's orphaned-RI-drain check flags it independently
    # (a stopped resource matching an RI's scope), not as reduced utilization
    # of this reservation's own 3 units.
    {
        "reservation_order_id": "a1b2c3d4-0002-4a1a-9c1a-000000000002",
        "reservation_id": "a1b2c3d4-0002-4a1a-9c1a-100000000002",
        "name": "a1b2c3d4-0002-4a1a-9c1a-100000000002",
        "type": "Microsoft.Capacity/reservationOrders/reservations",
        "location": "australiaeast",
        "sku_name": "Standard_D4ds_v4",
        "sku_description": "D4ds v4",
        "reserved_resource_type": "VirtualMachines",
        "instance_flexibility": "On",
        "applied_scope_type": "Single",
        "applied_scope_display_name": "sub-prod-001",
        "applied_scope_subscription_id": "/subscriptions/sub-prod-001",
        "billing_plan": "Monthly",
        "term": "P1Y",
        "quantity": 3,
        "provisioning_state": "Succeeded",
        "renew": False,
        "purchase_date": "2025-09-01",
        "purchase_date_time": "2025-09-01T14:02:11.0000000Z",
        "effective_date_time": "2025-09-01T14:02:11.0000000Z",
        "benefit_start_time": "2025-09-01T14:02:11.0000000Z",
        "expiry_date": "2026-09-01",
        "expiry_date_time": "2026-09-01T14:02:11.0000000Z",
        "utilization_trend": "Flat",
        "utilization_1day_pct": 100.0,
        "utilization_7day_pct": 100.0,
        "utilization_30day_pct": 100.0,
        "provider": "Azure",
    },

    # Corresponds to Commitment "RI-SQLDB-GP-GEN5-4-AE". reserved_resource_type
    # "SqlDatabases" and the "SQLDB_GP_Compute_Gen5" sku_name prefix (no vCore
    # suffix) were both live-verified against the Retail Prices API during the
    # earlier SKU-mapping work (pricing/sku_mapping.py's _SQL_RESERVATION_PREFIX).
    # NOTE: quantity=4 here is the real Azure unit (vCores purchased) - this
    # intentionally differs from this app's own simplified Commitment table,
    # where "RI-SQLDB-GP-GEN5-4-AE".reserved_qty=1 counts "1 resource covered",
    # not vCores. Both are internally consistent within their own schema; they
    # just count different things, which is itself worth knowing when reading
    # real Azure reservation quantities against this app's simplified model.
    {
        "reservation_order_id": "a1b2c3d4-0003-4a1a-9c1a-000000000003",
        "reservation_id": "a1b2c3d4-0003-4a1a-9c1a-100000000003",
        "name": "a1b2c3d4-0003-4a1a-9c1a-100000000003",
        "type": "Microsoft.Capacity/reservationOrders/reservations",
        "location": "australiaeast",
        "sku_name": "SQLDB_GP_Compute_Gen5",
        "sku_description": "SQL Database General Purpose - Gen5",
        "reserved_resource_type": "SqlDatabases",
        "instance_flexibility": None,
        "applied_scope_type": "Single",
        "applied_scope_display_name": "sub-prod-001",
        "applied_scope_subscription_id": "/subscriptions/sub-prod-001",
        "billing_plan": "Upfront",
        "term": "P1Y",
        "quantity": 4,
        "provisioning_state": "Succeeded",
        "renew": True,
        "purchase_date": "2025-12-01",
        "purchase_date_time": "2025-12-01T08:00:00.0000000Z",
        "effective_date_time": "2025-12-01T08:00:00.0000000Z",
        "benefit_start_time": "2025-12-01T08:00:00.0000000Z",
        "expiry_date": "2026-12-01",
        "expiry_date_time": "2026-12-01T08:00:00.0000000Z",
        "utilization_trend": "Flat",
        "utilization_1day_pct": 100.0,
        "utilization_7day_pct": 100.0,
        "utilization_30day_pct": 99.1,
        "provider": "Azure",
    },

    # Corresponds to Commitment "RI-SQLPOOL-GP-GEN5-8-AE". sku_name and
    # reserved_resource_type are DELIBERATELY IDENTICAL to the Single
    # Database record above ("SqlDatabases", not a separate "SqlElasticPools"
    # value - that enum value doesn't exist) - verified live that Azure's
    # Reservation catalog prices vCore Elastic Pools against the exact same
    # entries as Single Database. quantity=8 matches SQLPOOL-Prod-01's 8
    # pooled vCores.
    {
        "reservation_order_id": "a1b2c3d4-0004-4a1a-9c1a-000000000004",
        "reservation_id": "a1b2c3d4-0004-4a1a-9c1a-100000000004",
        "name": "a1b2c3d4-0004-4a1a-9c1a-100000000004",
        "type": "Microsoft.Capacity/reservationOrders/reservations",
        "location": "australiaeast",
        "sku_name": "SQLDB_GP_Compute_Gen5",
        "sku_description": "SQL Database General Purpose - Gen5",
        "reserved_resource_type": "SqlDatabases",
        "instance_flexibility": None,
        "applied_scope_type": "Single",
        "applied_scope_display_name": "sub-prod-001",
        "applied_scope_subscription_id": "/subscriptions/sub-prod-001",
        "billing_plan": "Upfront",
        "term": "P1Y",
        "quantity": 8,
        "provisioning_state": "Succeeded",
        "renew": True,
        "purchase_date": "2025-12-01",
        "purchase_date_time": "2025-12-01T08:00:00.0000000Z",
        "effective_date_time": "2025-12-01T08:00:00.0000000Z",
        "benefit_start_time": "2025-12-01T08:00:00.0000000Z",
        "expiry_date": "2026-12-01",
        "expiry_date_time": "2026-12-01T08:00:00.0000000Z",
        "utilization_trend": "Flat",
        "utilization_1day_pct": 100.0,
        "utilization_7day_pct": 100.0,
        "utilization_30day_pct": 98.4,
        "provider": "Azure",
    },
]

# Azure SQL Database's own Savings Plan purchase record is deliberately NOT
# included here. "Savings plan for databases" is a real, distinct Azure
# product (launched 2026-03-18) and its PRICING is already fetched correctly
# (pricing/commitment_pricing.py, verified live), but its purchase-time
# sku.name value is not published anywhere in Microsoft's own API docs as of
# this writing - every example across both the 2022-11-01 and 2026-06-01
# Billing Benefits API versions shows only "Compute_Savings_Plan". Guessing
# a value here would misrepresent it as verified when it isn't. Add it once
# a real example (Microsoft docs, or a real purchased order) confirms the
# actual sku.name string.

SAVINGS_PLAN_PURCHASES = [
    # Corresponds to Commitment "SP-COMPUTE-001". sku_name "Compute_Savings_Plan"
    # is Microsoft's own documented example value, verified verbatim.
    {
        "savings_plan_order_id": "b1c2d3e4-0001-4b1b-9d1b-000000000001",
        "savings_plan_id": "b1c2d3e4-0001-4b1b-9d1b-200000000001",
        "name": "b1c2d3e4-0001-4b1b-9d1b-000000000001/b1c2d3e4-0001-4b1b-9d1b-200000000001",
        "type": "Microsoft.BillingBenefits/savingsPlanOrders/savingsPlans",
        "sku_name": "Compute_Savings_Plan",
        "billing_scope_id": "/subscriptions/sub-prod-001",
        "billing_plan": "P1M",
        "commitment_grain": "Hourly",
        "commitment_currency_code": "USD",
        "commitment_amount": 0.80,
        "applied_scope_type": "Shared",
        "display_name": "Compute_SavingsPlan_Prod",
        "term": "P1Y",
        "provisioning_state": "Succeeded",
        "renew": True,
        "purchase_date_time": "2026-01-15T10:00:00.0000000Z",
        "effective_date_time": "2026-01-15T10:00:00.0000000Z",
        "benefit_start_time": "2026-01-15T10:00:00.0000000Z",
        "expiry_date_time": "2027-01-15T10:00:00.0000000Z",
        "utilization_trend": "",
        "utilization_1day_pct": 100.0,
        "utilization_7day_pct": 91.4,
        "utilization_30day_pct": 89.7,
        "provider": "Azure",
    },
]


# ── SP and RI eligibility maps ─────────────────────────────────────────────────

COMPUTE_SP_ELIGIBLE_TYPES = {
    "Compute", "App Service", "Azure Functions",
    "Container Instances", "Dedicated Host", "Container Apps", "Spring Apps",
}

DATABASE_SP_ELIGIBLE_TYPES = {
    "Azure SQL Database", "Azure SQL Elastic Pool", "Azure SQL Managed Instance",
    "Azure SQL Database Hyperscale", "Azure SQL Database Serverless",
    "Azure Database for PostgreSQL", "Azure Database for MySQL",
    "Azure Cosmos DB", "Azure DocumentDB",
    "Azure Database Migration Service", "Database",
}

# Services that support Azure Reserved Capacity (RI/RC) but NOT Savings Plans
RI_ONLY_ELIGIBLE_TYPES = {
    "Azure Blob Storage",
    "Azure Files",
    "Azure Cache for Redis",
    "Azure Cache for Redis Enterprise",
    "Azure Synapse Analytics",
    "Azure Databricks",
    "Azure Disk Storage",
    "Azure Backup Storage",
    "Azure NetApp Files",
    "Azure Data Explorer",
    "Azure Data Factory",
    "App Service",          # App Service stamp fee RI
}

# What each RI-only service type's reservation COVERS and EXCLUDES
RI_COVERAGE_NOTES = {
    "Compute":                          ("VM compute costs (~30-40% saving vs PAYG)", "Software, Windows licensing, networking, storage"),
    "Azure SQL Database":               ("Compute costs only (~35% saving)", "Software license, networking, storage"),
    "Azure SQL Elastic Pool":           ("Pooled compute costs only (~35% saving) - same rule as Single Database", "Software license, networking, storage"),
    "Azure SQL Managed Instance":       ("Compute costs only (~35% saving)", "Software license, networking, storage"),
    "Azure Database for PostgreSQL":    ("Compute costs only (~35% saving)", "Software, networking, storage"),
    "Azure Database for MySQL":         ("Compute costs only (~35% saving)", "Software, networking, storage"),
    "Azure Cosmos DB":                  ("Provisioned throughput (RU/s)", "Storage, networking"),
    "Azure Blob Storage":               ("Storage capacity (GiB) for Blob and Data Lake Gen2", "Bandwidth, transaction rates"),
    "Azure Files":                      ("Storage capacity for Azure Files (hot/cool)", "Bandwidth, transaction rates"),
    "Azure Cache for Redis":            ("Compute costs only", "Networking, storage"),
    "Azure Cache for Redis Enterprise": ("Compute costs only (all service tiers eligible)", "Networking, storage"),
    "Azure Synapse Analytics":          ("cDWU usage", "Storage, networking"),
    "Azure Databricks":                 ("DBU usage only", "Compute, storage, networking (charged separately)"),
    "Azure Disk Storage":               ("Premium SSD P30 and larger only", "Other disk types, sizes smaller than P30"),
    "Azure Data Factory":               ("Integration runtime compute cost", "Data movement, storage"),
    "App Service":                      ("Stamp fee / compute", "Workers and associated resources"),
}


def seed_if_empty(engine=None):
    if engine is None:
        engine = get_engine()
    init_db()
    with Session(engine) as session:
        if session.query(CloudInventory).filter_by(provider="Azure").count() == 0:
            session.bulk_insert_mappings(CloudInventory, INVENTORY)
            session.bulk_insert_mappings(Commitment, COMMITMENTS)
            session.bulk_insert_mappings(ReservationPurchase, RESERVATION_PURCHASES)
            session.bulk_insert_mappings(SavingsPlanPurchase, SAVINGS_PLAN_PURCHASES)
            session.commit()
            print(f"[OK] Seeded {len(INVENTORY)} resources, {len(COMMITMENTS)} commitments, "
                  f"{len(RESERVATION_PURCHASES)} reservation purchases, {len(SAVINGS_PLAN_PURCHASES)} savings plan purchases.")

            # Demo data deserves real SP/RI economics too, not just Live
            # tenants - fetch real 1yr/3yr rates for every demo SKU/region/OS
            # once, on first-ever seed. Public API, no credentials needed.
            # Best-effort: a network hiccup here shouldn't break app startup.
            try:
                from pricing.commitment_pricing import refresh_commitment_prices
                refresh_commitment_prices(engine, INVENTORY, provider="Azure")
                print("[OK] Cached real Savings Plan / Reserved Instance rates for demo inventory.")
            except Exception as e:
                print(f"[Warning] Could not pre-fetch demo commitment pricing: {e}")
        else:
            print("[INFO] Already seeded -- skipping.")


if __name__ == "__main__":
    seed_if_empty()
