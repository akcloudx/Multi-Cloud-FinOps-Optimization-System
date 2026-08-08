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
from db.schema import init_db, get_engine, CloudInventory, Commitment


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
     "payg_hourly_usd": 0.526, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Managed Instance — General Purpose, 8 vCores
    # RI covers: compute costs only. Not: software license, networking, storage.
    {"resource_id": "SQLMI-Prod-01", "resource_name": "prod-reporting-sqlmi",
     "resource_type": "Azure SQL Managed Instance",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Gen5_8",
     "payg_hourly_usd": 1.008, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Database for PostgreSQL — General Purpose, 4 vCores
    # RI covers: compute costs only. Not: software, networking, storage.
    {"resource_id": "PG-Prod-01", "resource_name": "prod-analytics-postgres",
     "resource_type": "Azure Database for PostgreSQL",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Gen5_4",
     "payg_hourly_usd": 0.396, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Database for MySQL — General Purpose, 4 vCores
    # RI covers: compute costs only. Not: software, networking, storage.
    {"resource_id": "MYSQL-Prod-01", "resource_name": "prod-cms-mysql",
     "resource_type": "Azure Database for MySQL",
     "resource_state": "Running",
     "region": "australiasoutheast", "os": "N/A", "sku": "GP_Gen5_4",
     "payg_hourly_usd": 0.396, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure Cosmos DB — 400 RU/s provisioned throughput
    # RI covers: throughput only. Not: storage, networking.
    {"resource_id": "COSMOS-Prod-01", "resource_name": "prod-catalog-cosmosdb",
     "resource_type": "Azure Cosmos DB",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Cosmos_400RU",
     "payg_hourly_usd": 0.032, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Database Serverless — Dev/Test
    {"resource_id": "SQLDB-Dev-01", "resource_name": "dev-test-serverless-sqldb",
     "resource_type": "Azure SQL Database",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Serverless_4",
     "payg_hourly_usd": 0.263, "avg_daily_running_hours": 10,
     "subscription": "sub-dev-002", "provider": "Azure", "is_orphaned": False},
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
     "scope_sku": "Standard_D4ds_v5", "scope_region": "australiaeast", "scope_os": "Windows",
     "hourly_usd_commitment": 0.20, "reserved_qty": 2,
     "term": "1-year", "expiry_date": "2026-11-01", "provider": "Azure"},

    {"commitment_id": "RI-VM-D4DS-V4-AE-WIN",
     "commitment_type": "Reserved Instance",
     "scope_sku": "Standard_D4ds_v4", "scope_region": "australiaeast", "scope_os": "Windows",
     "hourly_usd_commitment": 0.19, "reserved_qty": 3,
     "term": "1-year", "expiry_date": "2026-09-01", "provider": "Azure"},

    # ── Reserved Capacity — Azure SQL Database (covers compute costs ONLY) ─────
    {"commitment_id": "RI-SQLDB-GP-GEN5-4-AE",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "GP_Gen5_4", "scope_region": "australiaeast", "scope_os": "N/A",
     "hourly_usd_commitment": 0.340,   # ~35% saving vs 0.526 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-12-01", "provider": "Azure"},

    # ── Reserved Capacity — Azure Cosmos DB (covers throughput ONLY) ───────────
    {"commitment_id": "RI-COSMOS-400RU-AE",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "Cosmos_400RU", "scope_region": "australiaeast", "scope_os": "N/A",
     "hourly_usd_commitment": 0.020,   # ~37% saving vs 0.032 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2027-02-01", "provider": "Azure"},

    # ── Reserved Capacity — Azure Blob Storage (covers storage capacity ONLY) ──
    {"commitment_id": "RI-BLOB-LRS-HOT-AE",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "LRS_Hot_100TB", "scope_region": "australiaeast", "scope_os": "N/A",
     "hourly_usd_commitment": 0.140,   # ~25% saving vs 0.188 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-10-01", "provider": "Azure"},

    # ── Savings Plan for Compute (flexible $/hr pool, 1-yr or 3-yr) ────────────
    # Covers: VMs, App Service, Functions Premium, Container Instances,
    #         Dedicated Host, Container Apps, Azure Spring Apps
    {"commitment_id": "SP-COMPUTE-001",
     "commitment_type": "Savings Plan for Compute",
     "scope_sku": "Any Compute", "scope_region": "Global", "scope_os": "Any",
     "hourly_usd_commitment": 0.80,
     "reserved_qty": 0, "term": "1-year", "expiry_date": "2027-01-15", "provider": "Azure"},

    # ── Savings Plan for Databases (flexible $/hr pool, 1-year ONLY) ───────────
    # Covers: SQL DB, SQL MI, SQL Hyperscale, Serverless, PostgreSQL, MySQL,
    #         Cosmos DB, DocumentDB, DMS, SQL Server on VMs/Arc (hourly)
    {"commitment_id": "SP-DATABASE-001",
     "commitment_type": "Savings Plan for Databases",
     "scope_sku": "Any Database", "scope_region": "Global", "scope_os": "N/A",
     "hourly_usd_commitment": 1.20,
     "reserved_qty": 0, "term": "1-year", "expiry_date": "2027-03-01", "provider": "Azure"},
]


# ── SP and RI eligibility maps ─────────────────────────────────────────────────

COMPUTE_SP_ELIGIBLE_TYPES = {
    "Compute", "App Service", "Azure Functions",
    "Container Instances", "Dedicated Host", "Container Apps", "Spring Apps",
}

DATABASE_SP_ELIGIBLE_TYPES = {
    "Azure SQL Database", "Azure SQL Managed Instance",
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
    "Azure SQL Managed Instance":       ("Compute costs only (~35% saving)", "Software license, networking, storage"),
    "Azure Database for PostgreSQL":    ("Compute costs only (~35% saving)", "Software, networking, storage"),
    "Azure Database for MySQL":         ("Compute costs only (~35% saving)", "Software, networking, storage"),
    "Azure Cosmos DB":                  ("Provisioned throughput (RU/s)", "Storage, networking"),
    "Azure Blob Storage":               ("Storage capacity (GiB) for Blob and Data Lake Gen2", "Bandwidth, transaction rates"),
    "Azure Files":                      ("Storage capacity for Azure Files (hot/cool)", "Bandwidth, transaction rates"),
    "Azure Cache for Redis":            ("Compute costs only", "Networking, storage"),
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
            session.commit()
            print(f"[OK] Seeded {len(INVENTORY)} resources, {len(COMMITMENTS)} commitments.")
        else:
            print("[INFO] Already seeded -- skipping.")


if __name__ == "__main__":
    seed_if_empty()
