"""
db/seed.py — Mock Azure infrastructure seed data.

COMPUTE (Savings Plan for Compute  OR  Reserved Instance):
  - 2 Prod VMs, always Running, distinct SKU families (Standard_D4ds_v5,
    Standard_E4s_v5) - no partial-hours modeling, just Running or Stopped
  - 1 Dev VM (Standard_B2ms), also always Running - deliberately left
    uncommitted to demonstrate a real "not yet committed" gap
  - 1 more Dev VM (Standard_D4ds_v5, sub-dev-002) - added 2026-08-23,
    identical SKU/region/OS to VM-Prod-01 but a different subscription,
    specifically to demonstrate real Azure Reservation/Savings Plan scope
    restriction (Single subscription/resource group vs Shared - see
    COMMITMENTS' RI-VM-D4DS-V5-AE-WIN below): this VM must show as its own
    uncovered gap, not silently covered by a reservation scoped to a
    different subscription.
  - 1 Stopped legacy VM (Standard_D4ds_v4) with an active RI it no longer
    uses  → Orphaned

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
    ReservationPurchase, SavingsPlanPurchase, AzureVmFlexibilityGroup,
)


# ── Compute Inventory ──────────────────────────────────────────────────────────

COMPUTE_INVENTORY = [
    # Prod VMs — always Running, no partial-hours modeling (24 hrs/day is the
    # only value a Running VM ever carries here) → Reserved Instance and/or
    # Compute SP candidates. Each on a genuinely different VM family/size, not
    # near-duplicates of each other.
    # avg/p95_cpu_percent and avg/p95_memory_percent below are VM
    # Rightsizing's demo utilization data (added 2026-08-26) - real Azure
    # Monitor metric shapes (Percentage CPU, Available Memory Percentage -
    # the latter is *free* memory, not used, see analysis/rightsizing.py),
    # deliberately varied across these 5 VMs so the tab demonstrates every
    # classification case under the default settings, not just one.
    {"resource_id": "VM-Prod-01", "resource_name": "app-server-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_D4ds_v5",
     "payg_hourly_usd": 0.284, "avg_daily_running_hours": 24,
     "avg_cpu_percent": 45.0, "p95_cpu_percent": 65.0,
     "avg_memory_percent": 70.0, "p95_memory_percent": 55.0,   # -> Optimal
     "subscription": "sub-prod-001", "resource_group": "rg-prod", "provider": "Azure", "is_orphaned": False},

    # Same SKU/region/OS as VM-Prod-01 above, but in a DIFFERENT subscription
    # (sub-dev-002, same one VM-Dev-01 already uses below) - added 2026-08-23
    # specifically to exercise real Azure Reservation scope restriction (see
    # COMMITMENTS' RI-VM-D4DS-V5-AE-WIN below and analysis/engine.py's
    # _azure_scope_matches): that RI is purchased with Single-subscription
    # scope against sub-prod-001 ONLY, so in real Azure it can NEVER cover
    # this VM despite the identical SKU/region/OS - proves the coverage
    # table correctly shows VM-Prod-01 as covered and this VM as its own,
    # separate uncovered gap, not silently pooled together.
    {"resource_id": "VM-Dev-02", "resource_name": "dev-app-clone-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_D4ds_v5",
     "payg_hourly_usd": 0.284, "avg_daily_running_hours": 24,
     "avg_cpu_percent": 8.0, "p95_cpu_percent": 15.0,
     "avg_memory_percent": 88.0, "p95_memory_percent": 80.0,   # -> Underutilized
     "subscription": "sub-dev-002", "resource_group": "rg-dev", "provider": "Azure", "is_orphaned": False},

    # Memory-optimized batch/reporting workload — real live-fetched australiaeast
    # Windows rate (Retail Prices API, "Virtual Machines Esv5 Series Windows"
    # base meter, verified 2026-08), a genuinely different VM family from the
    # general-purpose Ddsv5 above, not just a bigger/smaller size of the same one.
    {"resource_id": "VM-Prod-02", "resource_name": "batch-worker-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_E4s_v5",
     "payg_hourly_usd": 0.486, "avg_daily_running_hours": 24,
     "avg_cpu_percent": 78.0, "p95_cpu_percent": 95.0,
     # Memory deliberately left unset (no avg/p95_memory_percent keys at
     # all) - the "no monitoring agent installed" demo case: this VM
     # still classifies correctly (Overutilized by CPU alone), and the
     # tab should show "N/A" + a caveat for its memory columns instead of
     # silently looking fully analyzed. See analysis/rightsizing.py.
     "subscription": "sub-prod-001", "resource_group": "rg-prod", "provider": "Azure", "is_orphaned": False},

    # Dev/test box — runs full-time like the prod VMs (no reduced-hours
    # modeling), so it counts in the same steady-state Compute SP pool. It
    # stays uncommitted deliberately (no RI/SP purchased against it yet) to
    # give the RI Coverage and Savings Plan tabs a real "not yet committed"
    # example to recommend against.
    {"resource_id": "VM-Dev-01", "resource_name": "dev-sandbox-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_B2ms",
     "payg_hourly_usd": 0.106, "avg_daily_running_hours": 24,
     "avg_cpu_percent": 5.0, "p95_cpu_percent": 12.0,
     "avg_memory_percent": 92.0, "p95_memory_percent": 85.0,   # -> Underutilized
     "subscription": "sub-dev-002", "resource_group": "rg-dev", "provider": "Azure", "is_orphaned": False},

    # Stopped — Orphaned (RI still active but VM is deallocated). Kept on
    # D4ds_v4 (one generation behind the Prod-01's D4ds_v5) rather than moving
    # to a different "legacy" SKU: checked live against the Retail Prices API
    # and older Dsv3-generation sizes (e.g. D2s_v3) have ZERO
    # priceType=Reservation entries in any region - Azure doesn't sell new RIs
    # against them at all, which would make an "orphaned RI" scenario on that
    # SKU fictional. D4ds_v4 is confirmed reservable (see COMMITMENTS below).
    {"resource_id": "VM-Legacy-01", "resource_name": "legacy-server-01",
     "resource_type": "Compute", "resource_state": "Stopped (deallocated)",
     "region": "australiaeast", "os": "Windows", "sku": "Standard_D4ds_v4",
     "payg_hourly_usd": 0.284, "avg_daily_running_hours": 0,
     "subscription": "sub-prod-001", "resource_group": "rg-prod", "provider": "Azure", "is_orphaned": True},

    # Azure VM Reserved Instance instance-size-flexibility demo (2026-08-29,
    # analysis/engine.py::_apply_azure_vm_size_flexibility) - a bigger
    # Ddsv5 Series VM running against a Shared-scope RI purchased for a
    # SMALLER size in the SAME real flexibility group (see
    # RI-VM-D8DS-V5-AE-LINUX below). Mirrors Microsoft's own worked
    # partial-coverage example shape (learn.microsoft.com/.../
    # reserved-vm-instance-size-flexibility, "Scenario 3": a bigger VM
    # against a half-ratio reservation = 50% covered) but with THIS app's
    # own real, eligible SKUs and real ratios - the doc's own DSv2-series
    # example SKUs (Standard_DS1_v2.._v5) turned out to have ZERO real
    # Reservation offering left in Azure's live catalog (confirmed while
    # testing this exact feature, see analysis/ri_eligibility.py's DS-series
    # exclusion), so they'd show gap=0 regardless of this feature working -
    # deliberately NOT used here. Ratio (Ddsv5 Series: D8ds_v5=4,
    # D16ds_v5=8) verified 2026-08-29 against Microsoft's own real
    # InstanceSizeFlexibilityGroup reference CSV (aka.ms/isf, still live as
    # of this date despite its deprecation notice). Deliberately does NOT
    # reuse Standard_D4ds_v5 (also Ddsv5 Series, ratio 2) even though it's
    # already in this demo's inventory - VM-Dev-02's existing "uncovered
    # gap" scenario specifically demonstrates real Azure scope restriction
    # (see RI-VM-D4DS-V5-AE-WIN's comment above) and pooling it into this
    # NEW flexibility group would silently resolve that gap via ISF,
    # breaking that separate, deliberate lesson - see
    # AZURE_VM_FLEXIBILITY_GROUPS below, which caches ONLY D8ds_v5/D16ds_v5
    # for exactly this reason. payg_hourly_usd verified live against the
    # real Azure Retail Prices API (2026-08-29): armSkuName=
    # Standard_D16ds_v5, armRegionName=australiaeast, meterName="D16ds v5"
    # (base Linux Consumption meter) = $1.136/hr.
    {"resource_id": "VM-Analytics-01", "resource_name": "analytics-node-01",
     "resource_type": "Compute", "resource_state": "Running",
     "region": "australiaeast", "os": "Linux", "sku": "Standard_D16ds_v5",
     "payg_hourly_usd": 1.136, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod", "provider": "Azure", "is_orphaned": False},
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
     "payg_hourly_usd": 0.724552, "avg_daily_running_hours": 24,   # real live-fetched rate, refreshed 2026-08 (was stale at 0.526)
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

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
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

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
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Managed Instance — General Purpose, 8 vCores
    # RI covers: compute costs only. Not: software license, networking, storage.
    {"resource_id": "SQLMI-Prod-01", "resource_name": "prod-reporting-sqlmi",
     "resource_type": "Azure SQL Managed Instance",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Gen5_8",
     "redundancy": "Locally Redundant",
     "payg_hourly_usd": 1.449112, "avg_daily_running_hours": 24,   # real live-fetched rate, refreshed 2026-08 (was stale at 1.008)
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

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
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Managed Instance Pool — General Purpose, Standard-series
    # (Gen5), 8 vCores. A genuinely different resource from standalone
    # Managed Instance above, even though it shares this app's SKU
    # convention: pools are billed via a SIZED "{vCores} vCore" meter, NOT
    # the standalone instance's flat per-vCore rate scaled up (they only
    # happen to coincide at this small vCore count - verified live they
    # diverge sharply at larger sizes, e.g. 80 vCore Premium-series pool is
    # priced at a real, separate, non-linear meter - see
    # pricing/sku_mapping.py's _plan_mi_instance_pool). Real live-fetched
    # australiaeast rate for the 8-vCore sized meter.
    {"resource_id": "SQLMIPOOL-Prod-01", "resource_name": "prod-migration-mipool",
     "resource_type": "Azure SQL Managed Instance Pool",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Gen5_8",
     "payg_hourly_usd": 1.449112, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure DocumentDB — vCore-based Cosmos DB for MongoDB, General Purpose,
    # M30 tier, single shard. NOT RI-eligible (no Reservation product for
    # Worker Node - see analysis/ri_eligibility.py); IS Savings Plan
    # eligible. SKU convention: "{tier}_{shardCount}" - real ARM fields
    # (properties.compute.tier + properties.sharding.shardCount - see
    # pricing/sku_mapping.py's _plan_documentdb). Coordinator Node cost
    # deliberately excluded (not ARM-observable - see that resolver's
    # comment). Real live-fetched australiaeast rate: Worker Node 2 vCore
    # x 1 shard = $0.3429/hr.
    {"resource_id": "DOCUMENTDB-Prod-01", "resource_name": "prod-catalog-documentdb",
     "resource_type": "Azure DocumentDB",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "M30_1",
     "payg_hourly_usd": 0.3429, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure Database for PostgreSQL — General Purpose, 4 vCores
    # RI covers: compute costs only. Not: software, networking, storage.
    {"resource_id": "PG-Prod-01", "resource_name": "prod-analytics-postgres",
     "resource_type": "Azure Database for PostgreSQL",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GeneralPurpose_Standard_D4ds_v5",
     "payg_hourly_usd": 0.488, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure Database for MySQL — General Purpose, 4 vCores
    # RI covers: compute costs only. Not: software, networking, storage.
    {"resource_id": "MYSQL-Prod-01", "resource_name": "prod-cms-mysql",
     "resource_type": "Azure Database for MySQL",
     "resource_state": "Running",
     "region": "australiasoutheast", "os": "N/A", "sku": "GeneralPurpose_Standard_D4ds_v5",
     "payg_hourly_usd": 0.47, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure Cosmos DB — Standard Provisioned, General Purpose, 400 RU/s
    # RI covers: throughput only. Not: storage, networking.
    {"resource_id": "COSMOS-Prod-01", "resource_name": "prod-catalog-cosmosdb",
     "resource_type": "Azure Cosmos DB",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Standard_GeneralPurpose_400",
     "payg_hourly_usd": 0.0368, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure SQL Database Serverless — Dev/Test
    {"resource_id": "SQLDB-Dev-01", "resource_name": "dev-test-serverless-sqldb",
     "resource_type": "Azure SQL Database",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GP_Serverless_4",
     "redundancy": "Locally Redundant",
     # Real live-fetched rate, refreshed 2026-08 (was stale at 0.263 - that
     # value predated this app's Serverless resolver being built and looked
     # hand-picked to represent "cheap because it auto-pauses", a concept
     # this app's flat avg_daily_running_hours model doesn't actually
     # represent anywhere else). Serverless genuinely IS billed at a premium
     # per-active-vCore-hour vs Provisioned at the same vCore count -
     # confirmed real, not a bug: the tradeoff only pays off if the resource
     # actually pauses during idle time, which this app doesn't model.
     "payg_hourly_usd": 2.483568, "avg_daily_running_hours": 10,
     "subscription": "sub-dev-002", "resource_group": "rg-dev", "provider": "Azure", "is_orphaned": False},

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
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure Database Migration Service — General Purpose, 4 vCores, real
    # ARM sku.tier/sku.capacity convention stored as "{Tier}_{N}vCores" (see
    # pricing/sku_mapping.py's _plan_database_migration_service). Live-
    # verified australiaeast rate for "4 vCore" on the General Purpose
    # Compute product.
    {"resource_id": "DMS-Prod-01", "resource_name": "prod-sqlmigration-dms",
     "resource_type": "Azure Database Migration Service",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "GeneralPurpose_4vCores",
     "payg_hourly_usd": 0.2025, "avg_daily_running_hours": 10,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},
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
     "subscription": "sub-prod-001", "resource_group": "rg-prod-storage", "provider": "Azure", "is_orphaned": False},

    # Azure Files — Reserved Capacity
    # RI covers: storage capacity for Azure Files.
    # NOT covered: bandwidth or transaction rates (hot/cool tiers).
    {"resource_id": "FILES-Prod-01", "resource_name": "prod-shared-azurefiles",
     "resource_type": "Azure Files",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "LRS_Hot_10TB",
     "payg_hourly_usd": 0.055, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-storage", "provider": "Azure", "is_orphaned": False},

    # Azure Cache for Redis — Reserved Capacity
    # RI covers: compute costs only. NOT: networking or storage.
    {"resource_id": "REDIS-Prod-01", "resource_name": "prod-session-redis",
     "resource_type": "Azure Cache for Redis",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "C2_Standard",
     "payg_hourly_usd": 0.088, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure Cache for Redis Enterprise (aka "Azure Managed Redis") — a
    # genuinely separate ARM resource type/SKU taxonomy from classic Redis
    # above. RI covers: compute costs only. NOT: networking or storage.
    {"resource_id": "REDISENT-Prod-01", "resource_name": "prod-cache-redisent",
     "resource_type": "Azure Cache for Redis Enterprise",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Balanced_B10",
     "payg_hourly_usd": 0.391, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-data", "provider": "Azure", "is_orphaned": False},

    # Azure Synapse Analytics — Reserved Capacity (cDWU)
    # RI covers: cDWU usage only. NOT: storage or networking.
    {"resource_id": "SYNAPSE-Prod-01", "resource_name": "prod-dw-synapse",
     "resource_type": "Azure Synapse Analytics",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "DW500c",
     "payg_hourly_usd": 8.45, "avg_daily_running_hours": 10,   # real live-fetched rate, refreshed 2026-08 (was stale at 6.000)
     "subscription": "sub-prod-001", "resource_group": "rg-prod-analytics", "provider": "Azure", "is_orphaned": False},

    # Azure Databricks — Reserved Capacity (DBU)
    # RI covers: DBU usage only. NOT: compute, storage, networking.
    {"resource_id": "ADB-Prod-01", "resource_name": "prod-etl-databricks",
     "resource_type": "Azure Databricks",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Premium_DBU",
     "payg_hourly_usd": 0.550, "avg_daily_running_hours": 10,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-analytics", "provider": "Azure", "is_orphaned": False},

    # Microsoft Fabric — Reserved Capacity (per-CU), F64 (64 Capacity Units).
    # RI covers: capacity usage only. NOT: storage or networking. Not
    # Savings-Plan-eligible at all (see analysis/sp_eligibility.py). Real
    # live-fetched australiaeast rate: 64 x $0.21/CU-hr.
    {"resource_id": "FABRIC-Prod-01", "resource_name": "prod-analytics-fabric",
     "resource_type": "Microsoft Fabric",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "F64",
     "payg_hourly_usd": 13.44, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-analytics", "provider": "Azure", "is_orphaned": False},

    # Azure Data Explorer — Reserved Capacity (Engine Cluster Markup fee only).
    # RI covers: the markup fee only, NOT cluster compute/storage/networking
    # (billed and reserved separately - see RI_COVERAGE_NOTES below). SKU
    # convention: "{tier}_{vmSize}_{capacity}" - real example from Microsoft's
    # own REST API reference (2026-08): tier=Standard, vmSize=Standard_L16as_v3,
    # capacity=2 nodes. Real live-fetched rate: 2 x $0.11/hr markup (australiaeast).
    {"resource_id": "ADX-Prod-01", "resource_name": "prod-telemetry-adx",
     "resource_type": "Azure Data Explorer",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Standard_Standard_L16as_v3_2",
     "payg_hourly_usd": 0.22, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-analytics", "provider": "Azure", "is_orphaned": False},

    # Azure-SSIS Integration Runtime — Consumption only, no RI or SP of any
    # kind exists for this (see analysis/ri_eligibility.py + sp_eligibility.py).
    # SKU convention: "{nodeSize}_{nodeCount}_{edition}_{licenseType}" - real
    # example nodeSize from Microsoft's own ARM template reference (2026-08).
    # Real live-fetched australiaeast rate: D8 v3 AHB = $1.158/hr x 1 node.
    {"resource_id": "SSIS-Prod-01", "resource_name": "prod-etl-ssisir",
     "resource_type": "Azure-SSIS Integration Runtime",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Standard_D8_v3_1_Standard_BasePrice",
     "payg_hourly_usd": 1.158, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-analytics", "provider": "Azure", "is_orphaned": False},

    # Azure Disk Storage — Reserved (P30+ Premium SSD only)
    # RI covers: Premium SSD P30 and larger. NOT: other disk types or smaller sizes.
    # SKU convention: "{family}_{redundancy}_{diskSizeGB}" - real ARM fields
    # (sku.name does NOT encode size at all; diskSizeGB is separate - see
    # pricing/sku_mapping.py's _plan_disk_storage). 1024 GiB = P30 exactly.
    # Real live-fetched australiaeast rate: $135.17/mo capacity meter only
    # (Disk Mount fee deliberately excluded, see resolver comment) / 730 =
    # $0.1852/hr.
    {"resource_id": "DISK-Prod-01", "resource_name": "prod-vm-disk-p30",
     "resource_type": "Azure Disk Storage",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Premium_LRS_1024",
     "payg_hourly_usd": 0.1852, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-storage", "provider": "Azure", "is_orphaned": False},

    # A second, smaller Premium disk below the P30 Reservation threshold -
    # demonstrates the real "PAYG-priceable but not RI-eligible" split this
    # round's ri_eligibility.py fix now correctly derives from diskSizeGB
    # (previously defaulted to eligible=True for ANY Premium disk regardless
    # of size - a real, self-caught bug, see analysis/ri_eligibility.py).
    # 256 GiB = P15. Real live-fetched australiaeast rate: $38.01/mo / 730.
    {"resource_id": "DISK-Prod-02", "resource_name": "prod-vm-disk-p15",
     "resource_type": "Azure Disk Storage",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Premium_LRS_256",
     "payg_hourly_usd": 0.0521, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod-storage", "provider": "Azure", "is_orphaned": False},
]


# ── Compute Savings Plan inventory (non-VM) ─────────────────────────────────────
# Azure Dedicated Host: RI AND Savings Plan for Compute both eligible (same
# "Virtual Machines" Retail catalog Dedicated Host pricing lives under).
# Azure Container Instances: Savings Plan for Compute eligible, but has NO
# Reservation offering at all - verified live, zero Reservation entries exist.
# Rates below are real australiaeast Consumption prices, live-queried 2026-08-15.

COMPUTE_SP_INVENTORY = [
    # Azure Dedicated Host — DSv3 Type3, $5.28/hr (australiaeast, live-verified)
    {"resource_id": "DEDHOST-Prod-01", "resource_name": "prod-dedicated-host-01",
     "resource_type": "Azure Dedicated Host",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "DSv3-Type3",
     "payg_hourly_usd": 5.280, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod", "provider": "Azure", "is_orphaned": False},

    # Azure Container Instances — 2 vCPU / 4 GB group, live-verified australiaeast
    # rates: Standard vCPU $0.0486/hr x 2 + Standard Memory $0.00532/GB-hr x 4
    {"resource_id": "ACI-Prod-01", "resource_name": "prod-batch-containergroup",
     "resource_type": "Azure Container Instances",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "vCPU2_Mem4",
     "payg_hourly_usd": 0.118, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod", "provider": "Azure", "is_orphaned": False},

    # Azure Container Apps — Dedicated profile, D4 node (4 vCPU / 16 GiB,
    # General Purpose), live-verified australiaeast rates: Dedicated vCPU
    # $0.080859/hr x 4 + Dedicated Memory $0.006638/hr x 16. Real per-node
    # billing model (NOT per-container-request like Container Instances) -
    # see pricing/sku_mapping.py's _plan_container_apps for the real
    # workload-profile billing research this SKU convention is based on.
    # Live-tenant ingestion of this resource type isn't built yet (the
    # billable unit is a node inside a managedEnvironment's workloadProfiles
    # array, not a standalone ARM resource Resource Graph can list the way
    # Dedicated Host's hostGroups/hosts can) - this demo row exists to prove
    # the pricing math end-to-end, not to represent a live-scannable resource.
    {"resource_id": "ACA-Prod-01", "resource_name": "prod-api-containerapps-d4",
     "resource_type": "Azure Container Apps",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Dedicated_D4",
     "payg_hourly_usd": 0.4296, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod", "provider": "Azure", "is_orphaned": False},

    # Azure Spring Apps Enterprise — one app instance within the base bundle
    # (<=6 vCPU, <=12 GB), live-verified australiaeast flat rate $0.8408/hr
    # ("Enterprise vCPU and Memory Group Duration"). See pricing/sku_mapping.py's
    # _plan_spring_apps_enterprise for why this app only prices the in-bundle
    # case (overage-above-threshold pricing isn't built). Live-tenant
    # ingestion isn't built yet either - same disclosed-gap status as
    # Container Apps' demo row.
    {"resource_id": "SPRING-Prod-01", "resource_name": "prod-orders-springapps",
     "resource_type": "Azure Spring Apps Enterprise",
     "resource_state": "Running",
     "region": "australiaeast", "os": "N/A", "sku": "Enterprise",
     "payg_hourly_usd": 0.8408, "avg_daily_running_hours": 24,
     "subscription": "sub-prod-001", "resource_group": "rg-prod", "provider": "Azure", "is_orphaned": False},
]

INVENTORY = COMPUTE_INVENTORY + DATABASE_INVENTORY + RI_ONLY_INVENTORY + COMPUTE_SP_INVENTORY


# ── Commitment Contracts ───────────────────────────────────────────────────────

COMMITMENTS = [
    # ── Reserved Instances — Compute (VM SKU-level) ────────────────────────────
    # reserved_qty=1 matches VM-Prod-01 exactly - fully covered, no gap, a
    # clean "matched" example. Purchased with real Azure "Single subscription"
    # scope against sub-prod-001 (matches RESERVATION_PURCHASES' real
    # applied_scope_subscription_id for this same reservation below) -
    # scope_subscription_id added 2026-08-23 so this RI does NOT also cover
    # VM-Dev-02 (identical SKU/region/OS, but a different subscription) the
    # way this app incorrectly let it before - see analysis/engine.py's
    # _azure_scope_matches.
    {"commitment_id": "RI-VM-D4DS-V5-AE-WIN",
     "commitment_type": "Reserved Instance",
     "scope_sku": "Standard_D4ds_v5", "scope_resource_type": "Compute",
     "scope_region": "australiaeast", "scope_os": "Windows", "scope_redundancy": "N/A",
     "scope_subscription_id": "sub-prod-001",
     "hourly_usd_commitment": 0.20, "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-11-01", "provider": "Azure"},

    # reserved_qty=1 - the only D4ds_v4 resource left is VM-Legacy-01, which is
    # Stopped, so this entire reservation is now wasted spend (orphaned-RI-drain
    # demo). See COMPUTE_INVENTORY's comment on why this SKU was kept rather
    # than moved to a different "legacy" one.
    {"commitment_id": "RI-VM-D4DS-V4-AE-WIN",
     "commitment_type": "Reserved Instance",
     "scope_sku": "Standard_D4ds_v4", "scope_resource_type": "Compute",
     "scope_region": "australiaeast", "scope_os": "Windows", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 0.19, "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-09-01", "provider": "Azure"},

    # Instance-size-flexibility demo (2026-08-29) - purchased for the
    # SMALLER Standard_D8ds_v5 (real Ddsv5 Series ratio 4, verified against
    # Microsoft's own aka.ms/isf reference CSV), Shared scope (no
    # scope_subscription_id - runs through the main tenant-wide layer, not
    # the Single-subscription scoped layer the two D4ds_v5/D4ds_v4 RIs
    # above use), covers half of VM-Analytics-01's real D16ds_v5 (ratio 8) -
    # see AZURE_VM_FLEXIBILITY_GROUPS below for the cache rows this needs,
    # and VM-Analytics-01's own comment above for why D4ds_v5 wasn't reused
    # for this instead. hourly_usd_commitment ~30% off D8ds_v5's real Linux
    # PAYG rate (verified live: $0.568/hr, same Retail Prices API check as
    # VM-Analytics-01's payg_hourly_usd above).
    {"commitment_id": "RI-VM-D8DS-V5-AE-LINUX",
     "commitment_type": "Reserved Instance",
     "scope_sku": "Standard_D8ds_v5", "scope_resource_type": "Compute",
     "scope_region": "australiaeast", "scope_os": "Linux", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 0.40, "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-11-01", "provider": "Azure",
     "instance_flexibility": "On"},

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

    # ── Reserved Instance — Azure Dedicated Host (covers physical host compute
    #    costs only). Rate is the real live-fetched 1yr RI rate for DSv3-Type3.
    {"commitment_id": "RI-DEDHOST-DSV3T3-AE",
     "commitment_type": "Reserved Instance",
     "scope_sku": "DSv3-Type3", "scope_resource_type": "Azure Dedicated Host",
     "scope_region": "australiaeast", "scope_os": "N/A", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 3.1378995433789956,   # ~40% saving vs 5.28 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-10-15", "provider": "Azure"},

    # ── Reserved Instance — Azure Cache for Redis Enterprise (covers compute
    #    costs only). Real live-fetched 1yr RI rate for Balanced_B10.
    {"commitment_id": "RI-REDISENT-B10-AE",
     "commitment_type": "Reserved Instance",
     "scope_sku": "Balanced_B10", "scope_resource_type": "Azure Cache for Redis Enterprise",
     "scope_region": "australiaeast", "scope_os": "N/A", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 0.2541095890410959,   # ~35% saving vs 0.391 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2027-01-10", "provider": "Azure"},

    # ── Reserved Capacity — Azure Synapse Analytics (covers cDWU usage ONLY).
    #    Real live-fetched 1yr rate for DW500c (5 x DW100c reservation units,
    #    already scaled - see pricing/sku_mapping.py's reservation_multiplier).
    {"commitment_id": "RI-SYNAPSE-DW500C-AE",
     "commitment_type": "Reserved Capacity",
     "scope_sku": "DW500c", "scope_resource_type": "Azure Synapse Analytics",
     "scope_region": "australiaeast", "scope_os": "N/A", "scope_redundancy": "N/A",
     "hourly_usd_commitment": 5.323630136986301,   # ~37% saving vs 8.45 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-11-20", "provider": "Azure"},

    # ── Reserved Instance — Azure SQL Managed Instance (covers compute costs
    #    ONLY). Deliberately covers SQLMI-Prod-01 (GP_Gen5_8) only - SQLMI-Prod-02
    #    (BC Premium-series, Zone Redundant) is intentionally left uncovered as a
    #    realistic gap, matching the SQL Database ZR pattern already established.
    {"commitment_id": "RI-SQLMI-GP-GEN5-8-AE",
     "commitment_type": "Reserved Instance",
     "scope_sku": "GP_Gen5_8", "scope_resource_type": "Azure SQL Managed Instance",
     "scope_region": "australiaeast", "scope_os": "N/A", "scope_redundancy": "Locally Redundant",
     "hourly_usd_commitment": 0.9415525114155251,   # ~35% saving vs 1.449112 PAYG
     "reserved_qty": 1,
     "term": "1-year", "expiry_date": "2026-12-15", "provider": "Azure"},

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
    # Corresponds to Commitment "RI-VM-D4DS-V5-AE-WIN" - covers VM-Prod-01
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
        "quantity": 1,
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
    # Corresponds to Commitment "RI-VM-D4DS-V4-AE-WIN" - reserved_qty=1, and
    # the only D4ds_v4 resource left in inventory is VM-Legacy-01, which is
    # Stopped (deallocated). So this reservation's single unit is genuinely
    # unused right now - analysis/engine.py's orphaned-RI-drain check flags
    # exactly this (a stopped resource matching an active RI's scope).
    # Utilization trending down reflects the VM having recently been
    # decommissioned rather than always having been idle.
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
        "quantity": 1,
        "provisioning_state": "Succeeded",
        "renew": False,
        "purchase_date": "2025-09-01",
        "purchase_date_time": "2025-09-01T14:02:11.0000000Z",
        "effective_date_time": "2025-09-01T14:02:11.0000000Z",
        "benefit_start_time": "2025-09-01T14:02:11.0000000Z",
        "expiry_date": "2026-09-01",
        "expiry_date_time": "2026-09-01T14:02:11.0000000Z",
        "utilization_trend": "Down",
        "utilization_1day_pct": 0.0,
        "utilization_7day_pct": 12.5,
        "utilization_30day_pct": 48.0,
        "provider": "Azure",
    },

    # Corresponds to Commitment "RI-VM-D8DS-V5-AE-LINUX" - instance-size-
    # flexibility demo, see that Commitment's own comment and
    # AZURE_VM_FLEXIBILITY_GROUPS below. applied_scope_type "Shared" (no
    # subscription/resource-group restriction) - a real, valid Azure
    # purchase choice (the default, in fact - "Single subscription" the
    # other two demo RIs use is the one that requires opting IN).
    {
        "reservation_order_id": "a1b2c3d4-0009-4a1a-9c1a-000000000009",
        "reservation_id": "a1b2c3d4-0009-4a1a-9c1a-100000000009",
        "name": "a1b2c3d4-0009-4a1a-9c1a-100000000009",
        "type": "Microsoft.Capacity/reservationOrders/reservations",
        "location": "australiaeast",
        "sku_name": "Standard_D8ds_v5",
        "sku_description": "D8ds v5",
        "reserved_resource_type": "VirtualMachines",
        "instance_flexibility": "On",
        "applied_scope_type": "Shared",
        "billing_plan": "Upfront",
        "term": "P1Y",
        "quantity": 1,
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

    # Corresponds to Commitment "RI-DEDHOST-DSV3T3-AE" - covers DEDHOST-Prod-01.
    # reserved_resource_type "DedicatedHost" and sku_name "DSv3 Type3" (space,
    # not hyphen) both verified against the real ReservedResourceType enum
    # (https://learn.microsoft.com/en-us/rest/api/reserved-vm-instances/reservation/get)
    # and this app's own live-verified pricing/sku_mapping.py reservation_match_value.
    # instance_flexibility is None - that field is documented as only applying
    # to the VirtualMachines reserved resource type.
    {
        "reservation_order_id": "a1b2c3d4-0005-4a1a-9c1a-000000000005",
        "reservation_id": "a1b2c3d4-0005-4a1a-9c1a-100000000005",
        "name": "a1b2c3d4-0005-4a1a-9c1a-100000000005",
        "type": "Microsoft.Capacity/reservationOrders/reservations",
        "location": "australiaeast",
        "sku_name": "DSv3 Type3",
        "sku_description": "DSv3-Type3 Dedicated Host",
        "reserved_resource_type": "DedicatedHost",
        "instance_flexibility": None,
        "applied_scope_type": "Single",
        "applied_scope_display_name": "sub-prod-001",
        "applied_scope_subscription_id": "/subscriptions/sub-prod-001",
        "billing_plan": "Upfront",
        "term": "P1Y",
        "quantity": 1,
        "provisioning_state": "Succeeded",
        "renew": True,
        "purchase_date": "2025-10-15",
        "purchase_date_time": "2025-10-15T11:30:00.0000000Z",
        "effective_date_time": "2025-10-15T11:30:00.0000000Z",
        "benefit_start_time": "2025-10-15T11:30:00.0000000Z",
        "expiry_date": "2026-10-15",
        "expiry_date_time": "2026-10-15T11:30:00.0000000Z",
        "utilization_trend": "Flat",
        "utilization_1day_pct": 100.0,
        "utilization_7day_pct": 100.0,
        "utilization_30day_pct": 100.0,
        "provider": "Azure",
    },

    # Corresponds to Commitment "RI-REDISENT-B10-AE" - covers REDISENT-Prod-01.
    # reserved_resource_type "RedisCache" - the real ReservedResourceType enum
    # has only ONE Redis value (no separate Enterprise variant); Enterprise vs
    # classic is distinguished by sku_name shape alone ("B10" bare size code,
    # matching Enterprise's real armSkuName convention - see pricing/
    # sku_mapping.py's _plan_redis_enterprise - vs classic Redis's "P2"/"C1"-
    # style codes).
    {
        "reservation_order_id": "a1b2c3d4-0006-4a1a-9c1a-000000000006",
        "reservation_id": "a1b2c3d4-0006-4a1a-9c1a-100000000006",
        "name": "a1b2c3d4-0006-4a1a-9c1a-100000000006",
        "type": "Microsoft.Capacity/reservationOrders/reservations",
        "location": "australiaeast",
        "sku_name": "B10",
        "sku_description": "Balanced B10 (Azure Managed Redis)",
        "reserved_resource_type": "RedisCache",
        "instance_flexibility": None,
        "applied_scope_type": "Single",
        "applied_scope_display_name": "sub-prod-001",
        "applied_scope_subscription_id": "/subscriptions/sub-prod-001",
        "billing_plan": "Monthly",
        "term": "P1Y",
        "quantity": 1,
        "provisioning_state": "Succeeded",
        "renew": False,
        "purchase_date": "2026-01-10",
        "purchase_date_time": "2026-01-10T13:45:00.0000000Z",
        "effective_date_time": "2026-01-10T13:45:00.0000000Z",
        "benefit_start_time": "2026-01-10T13:45:00.0000000Z",
        "expiry_date": "2027-01-10",
        "expiry_date_time": "2027-01-10T13:45:00.0000000Z",
        "utilization_trend": "Flat",
        "utilization_1day_pct": 100.0,
        "utilization_7day_pct": 100.0,
        "utilization_30day_pct": 100.0,
        "provider": "Azure",
    },

    # Corresponds to Commitment "RI-SYNAPSE-DW500C-AE" - covers SYNAPSE-Prod-01.
    # reserved_resource_type "SqlDataWarehouse" (Synapse Dedicated SQL Pool's
    # real enum value, matching the classic "SQL Data Warehouse" product name
    # this service is built on). sku_name "DW100c" - the ONLY real Reservation
    # unit sold (verified live earlier this session), NOT "DW500c" - quantity=5
    # represents the real Azure purchase quantity (5 x DW100c = 500 DWU),
    # matching the same "app's simplified reserved_qty=1 resource vs real
    # Azure's scaled unit quantity" distinction already established for SQL DB.
    {
        "reservation_order_id": "a1b2c3d4-0007-4a1a-9c1a-000000000007",
        "reservation_id": "a1b2c3d4-0007-4a1a-9c1a-100000000007",
        "name": "a1b2c3d4-0007-4a1a-9c1a-100000000007",
        "type": "Microsoft.Capacity/reservationOrders/reservations",
        "location": "australiaeast",
        "sku_name": "DW100c",
        "sku_description": "Synapse Analytics Dedicated SQL Pool - DW100c",
        "reserved_resource_type": "SqlDataWarehouse",
        "instance_flexibility": None,
        "applied_scope_type": "Single",
        "applied_scope_display_name": "sub-prod-001",
        "applied_scope_subscription_id": "/subscriptions/sub-prod-001",
        "billing_plan": "Upfront",
        "term": "P1Y",
        "quantity": 5,
        "provisioning_state": "Succeeded",
        "renew": True,
        "purchase_date": "2025-11-20",
        "purchase_date_time": "2025-11-20T09:00:00.0000000Z",
        "effective_date_time": "2025-11-20T09:00:00.0000000Z",
        "benefit_start_time": "2025-11-20T09:00:00.0000000Z",
        "expiry_date": "2026-11-20",
        "expiry_date_time": "2026-11-20T09:00:00.0000000Z",
        "utilization_trend": "Flat",
        "utilization_1day_pct": 100.0,
        "utilization_7day_pct": 100.0,
        "utilization_30day_pct": 100.0,
        "provider": "Azure",
    },

    # Corresponds to Commitment "RI-SQLMI-GP-GEN5-8-AE" - covers SQLMI-Prod-01
    # ONLY (SQLMI-Prod-02, BC Premium-series Zone Redundant, is deliberately
    # left uncovered as a realistic gap). sku_name "SQLMI_GP_Compute_Gen5" and
    # reserved_resource_type "SqlDatabases" (no separate "SqlManagedInstance"
    # enum value exists - same real-world quirk already confirmed for SQL
    # Elastic Pool) both verified against pricing/sku_mapping.py's live-tested
    # reservation_match_value. quantity=8 is the real vCore count, matching
    # the SQL Database record's quantity=4-is-vCores convention above.
    {
        "reservation_order_id": "a1b2c3d4-0008-4a1a-9c1a-000000000008",
        "reservation_id": "a1b2c3d4-0008-4a1a-9c1a-100000000008",
        "name": "a1b2c3d4-0008-4a1a-9c1a-100000000008",
        "type": "Microsoft.Capacity/reservationOrders/reservations",
        "location": "australiaeast",
        "sku_name": "SQLMI_GP_Compute_Gen5",
        "sku_description": "SQL Managed Instance General Purpose - Gen5",
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
        "purchase_date": "2025-12-15",
        "purchase_date_time": "2025-12-15T10:15:00.0000000Z",
        "effective_date_time": "2025-12-15T10:15:00.0000000Z",
        "benefit_start_time": "2025-12-15T10:15:00.0000000Z",
        "expiry_date": "2026-12-15",
        "expiry_date_time": "2026-12-15T10:15:00.0000000Z",
        "utilization_trend": "Flat",
        "utilization_1day_pct": 100.0,
        "utilization_7day_pct": 96.8,
        "utilization_30day_pct": 95.2,
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
    "Azure Container Instances", "Azure Dedicated Host",
    "Azure Container Apps", "Azure Spring Apps Enterprise",
}

DATABASE_SP_ELIGIBLE_TYPES = {
    "Azure SQL Database", "Azure SQL Elastic Pool", "Azure SQL Managed Instance",
    "Azure SQL Managed Instance Pool",
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
    "Microsoft Fabric",
    "App Service",          # App Service stamp fee RI
}

# What each RI-only service type's reservation COVERS and EXCLUDES
RI_COVERAGE_NOTES = {
    "Compute":                          ("VM compute costs (~30-40% saving vs PAYG)", "Software, Windows licensing, networking, storage"),
    "Azure SQL Database":               ("Compute costs only (~35% saving)", "Software license, networking, storage"),
    "Azure SQL Elastic Pool":           ("Pooled compute costs only (~35% saving) - same rule as Single Database", "Software license, networking, storage"),
    "Azure SQL Managed Instance":       ("Compute costs only (~35% saving)", "Software license, networking, storage"),
    "Azure SQL Managed Instance Pool":  ("Compute costs only (~35%/~55% saving, 1yr/3yr) - billed as one pool-wide charge, not per pooled instance", "Software license, networking, storage"),
    "Azure Database for PostgreSQL":    ("Compute costs only (~35% saving)", "Software, networking, storage"),
    "Azure Database for MySQL":         ("Compute costs only (~35% saving)", "Software, networking, storage"),
    "Azure Cosmos DB":                  ("Provisioned throughput (RU/s)", "Storage, networking"),
    "Azure Blob Storage":               ("Storage capacity (GiB) for Blob and Data Lake Gen2", "Bandwidth, transaction rates"),
    "Azure Files":                      ("Storage capacity for Azure Files (hot/cool)", "Bandwidth, transaction rates"),
    "Azure Cache for Redis":            ("Compute costs only", "Networking, storage"),
    "Azure Cache for Redis Enterprise": ("Compute costs only (all service tiers eligible)", "Networking, storage"),
    "Azure Synapse Analytics":          ("cDWU usage", "Storage, networking"),
    "Azure Databricks":                 ("DBU usage only - Reserved Capacity (DBCU) is real and priced, but applies as a subscription-wide pooled discount across all workloads/VM SKUs, not matched to this specific resource - not yet represented in RI Coverage below, see pricing/sku_mapping.py", "Compute, storage, networking (charged separately)"),
    "Azure Disk Storage":               ("Premium SSD P30 and larger only", "Other disk types, sizes smaller than P30"),
    "Azure Dedicated Host":              ("Physical host compute costs only (~40-55% saving)", "Software, networking, storage"),
    "Azure Data Factory":               ("Data Flow compute only - Reservation is real and priced per-vCore across 3 compute tiers, but applies automatically to ANY pipeline run's ephemeral compute, not a standing resource this app can inventory - not yet represented in RI Coverage below, see pricing/sku_mapping.py", "Data movement, storage, Integration Runtime nodes (see Azure-SSIS Integration Runtime)"),
    "Azure Data Explorer":              ("Services markup fee only (subscription-wide, ~30% saving)", "Cluster compute, networking, storage - billed and reserved separately"),
    "Azure Backup Storage":              ("Vault-standard tier backup data (100 TiB/1 PiB blocks)", "Vault-archive tier, Protected Instance cost, early deletion/bandwidth charges"),
    "Azure NetApp Files":                ("Standard/Premium/Ultra capacity pools, hot tier only (100 TiB/1 PiB blocks)", "Flexible service level, cool-tier consumption, cross-region replication, backup add-ons"),
    "Microsoft Fabric":                  ("Capacity Unit (CU) usage, 1yr/3yr give the same ~40% discount", "Storage, networking - not eligible for any Savings Plan"),
    "App Service":                      ("Stamp fee / compute", "Workers and associated resources"),
}

# Azure VM Reserved Instance instance-size-flexibility group/ratio cache
# (2026-08-29, mirrors AzureVmFlexibilityGroup - see
# pricing/azure_vm_flexibility.py/azure_conn/connector.py::
# fetch_vm_flexibility_groups). Real Ddsv5 Series ratios, verified
# 2026-08-29 directly against Microsoft's own authoritative
# InstanceSizeFlexibilityGroup reference (aka.ms/isf CSV - still live as of
# this date, ahead of its own deprecation notice) - NOT Microsoft's doc
# examples (those illustrate the concept using the DSv2 series, which has
# zero real Reservation offering left in Azure's live catalog, see
# analysis/ri_eligibility.py's DS-series exclusion and VM-Analytics-01's
# own comment above). Deliberately caches ONLY D8ds_v5/D16ds_v5, not the
# D4ds_v5 already used elsewhere in this demo's inventory/commitments (same
# group in reality, ratio 2) - pooling it in here would silently resolve
# VM-Dev-02's deliberately-uncovered scope-restriction demo gap, breaking
# that separate lesson; a live tenant's real sync would cache every SKU
# actually present in inventory, this demo narrows it on purpose instead.
# A live tenant would fetch this from the real Reservations Catalog API
# instead; demo/benchmark mode has no live Azure credentials to call it
# with, so these rows are seeded directly, same "no live API access in
# demo mode" pattern already used for every other cache table here (see
# seed_if_empty's own refresh_commitment_prices try/except below, which
# likewise only runs for real network-reachable public APIs, not
# tenant-authenticated ones).
AZURE_VM_FLEXIBILITY_GROUPS = [
    {"region": "australiaeast", "sku": "Standard_D8ds_v5",  "flexibility_group": "Ddsv5 Series", "ratio": 4.0, "fetched_at": "2026-08-29 00:00:00 UTC"},
    {"region": "australiaeast", "sku": "Standard_D16ds_v5", "flexibility_group": "Ddsv5 Series", "ratio": 8.0, "fetched_at": "2026-08-29 00:00:00 UTC"},
]


def seed_if_empty(engine=None):
    # Demo/benchmark data always lives in the "demo" scope - never the caller's
    # choice, since this function's entire purpose is seeding THAT scope specifically.
    if engine is None:
        engine = get_engine("Azure", "demo")
    init_db("Azure", "demo")
    with Session(engine) as session:
        if session.query(CloudInventory).filter_by(provider="Azure").count() == 0:
            session.bulk_insert_mappings(CloudInventory, INVENTORY)
            session.bulk_insert_mappings(Commitment, COMMITMENTS)
            session.bulk_insert_mappings(ReservationPurchase, RESERVATION_PURCHASES)
            session.bulk_insert_mappings(SavingsPlanPurchase, SAVINGS_PLAN_PURCHASES)
            session.bulk_insert_mappings(AzureVmFlexibilityGroup, AZURE_VM_FLEXIBILITY_GROUPS)
            session.commit()
            print(f"[OK] Seeded {len(INVENTORY)} resources, {len(COMMITMENTS)} commitments, "
                  f"{len(RESERVATION_PURCHASES)} reservation purchases, {len(SAVINGS_PLAN_PURCHASES)} savings plan purchases, "
                  f"{len(AZURE_VM_FLEXIBILITY_GROUPS)} VM flexibility-group cache rows.")

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

    seed_demo_tenant_if_empty()


def seed_demo_tenant_if_empty():
    """Seeds one demo tenant ("Demo Tenant 1") with 2 subscriptions in the
    demo scope, so the Home page has a real CloudTenant/TenantSubscription
    row to list even in Demo Mode - not a UI mockup. Permission statuses are
    pre-set to realistic simulated values (one ready, one missing a role)
    since no real Azure tenant exists behind a Demo entry to actually check -
    see azure_conn/connector.py's check_role_assignments for the real,
    Production-only version of this check. Independently guarded (checks its
    own emptiness) so it's safe to call every startup, not just on first-ever
    inventory seed."""
    from db.tenants import list_tenants, upsert_tenant, upsert_subscription, update_tenant_permission_status
    if list_tenants("Azure", "demo"):
        return
    tenant_db_id = upsert_tenant(
        provider="Azure", mode="demo", tenant_name="Demo Tenant 1",
        tenant_id="11111111-2222-3333-4444-555555555555",
        subscription_id="66666666-7777-8888-9999-000000000000",
        client_id="77777777-8888-9999-0000-111111111111",
        client_secret="demo-tenant-has-no-real-credentials",
        domain="demo.onmicrosoft.com",
    )
    # Tenant-wide (Reservations Reader / Savings Plan Reader) status,
    # simulated the same way as the per-subscription ones below. assigned_roles
    # must agree with the status - the per-role checklist reads assigned_roles
    # directly, so a "ready" status with no assigned_roles renders every role
    # as missing despite the green tab icon (a real bug caught in browser
    # verification 2026-08).
    update_tenant_permission_status(
        "Azure", "demo", tenant_db_id, "ready", None,
        assigned_roles="Reservations Reader, Savings Plan Reader",
    )
    upsert_subscription(
        provider="Azure", mode="demo", tenant_db_id=tenant_db_id,
        subscription_id="66666666-7777-8888-9999-000000000000",
        subscription_name="Production", permission_status="ready",
        assigned_roles="Reader, Cost Management Reader",
    )
    upsert_subscription(
        provider="Azure", mode="demo", tenant_db_id=tenant_db_id,
        subscription_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        subscription_name="Sandbox", permission_status="missing_role",
        missing_role="Cost Management Reader", assigned_roles="Reader",
    )
    print("[OK] Seeded demo tenant 'Demo Tenant 1' with 2 subscriptions.")


if __name__ == "__main__":
    seed_if_empty()
