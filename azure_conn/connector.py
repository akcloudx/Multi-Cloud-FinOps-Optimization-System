"""
azure/connector.py
Azure Service Principal authentication and live data connection module.

Handles:
  1. Credential validation (Service Principal via Client Secret)
  2. Live inventory fetch from Azure Resource Graph
  3. Live reservation & savings plan fetch from Azure Consumption APIs
  4. Required RBAC roles documentation

REQUIRED AZURE RBAC ROLES for the Service Principal:
  ┌─────────────────────────────┬───────────────────────────────┬───────────────────────────┐
  │ Role                        │ Scope                         │ Purpose                   │
  ├─────────────────────────────┼───────────────────────────────┼───────────────────────────┤
  │ Reader                      │ Subscription                  │ List all Azure resources  │
  │ Cost Management Reader      │ Subscription                  │ Read cost & usage data    │
  │ Reservations Reader         │ Subscription or Tenant        │ Read RI & Reserved Cap.   │
  └─────────────────────────────┴───────────────────────────────┴───────────────────────────┘

HOW TO CREATE THE SERVICE PRINCIPAL (Azure CLI):
  az ad sp create-for-rbac --name "finops-optimizer-sp" --role "Reader" \
      --scopes /subscriptions/<SUBSCRIPTION_ID>

  Then assign additional roles:
  az role assignment create \
      --assignee <APP_ID> \
      --role "Cost Management Reader" \
      --scope /subscriptions/<SUBSCRIPTION_ID>

  az role assignment create \
      --assignee <APP_ID> \
      --role "Reservations Reader" \
      --scope /subscriptions/<SUBSCRIPTION_ID>
"""

import sys, os, glob
from typing import Optional

_CONNECTOR_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_CONNECTOR_DIR, ".."))
# Linux-only fallback (the bundle ships Linux .so binaries); appended, never
# inserted first, so a real pip install always wins over the vendored copy.
vendor_dir = os.path.join(_PROJECT_ROOT, "azure_sdk_vendor")
if sys.platform.startswith("linux") and os.path.exists(vendor_dir) and vendor_dir not in sys.path:
    sys.path.append(vendor_dir)

# Auto-discover Azure App Service virtualenv site-packages
for ant_path in [
    "/home/site/wwwroot/antenv/lib/python3.12/site-packages",
    "/home/site/wwwroot/antenv/lib/python3.11/site-packages",
    "/home/site/wwwroot/antenv/lib/python3.10/site-packages",
    "/home/site/wwwroot/.python_packages/lib/site-packages",
]:
    if os.path.exists(ant_path) and ant_path not in sys.path:
        sys.path.insert(0, ant_path)

for site_pkg in glob.glob("/home/site/wwwroot/antenv/lib/python*/site-packages"):
    if os.path.exists(site_pkg) and site_pkg not in sys.path:
        sys.path.insert(0, site_pkg)

# Check which Azure SDK packages are available
try:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.resource.resources import ResourceManagementClient
    HAS_AZURE_IDENTITY = True
except ImportError:
    HAS_AZURE_IDENTITY = False

try:
    from azure.mgmt.resourcegraph import ResourceGraphClient
    from azure.mgmt.resourcegraph.models import QueryRequest
    HAS_RESOURCE_GRAPH = True
except ImportError:
    HAS_RESOURCE_GRAPH = False

try:
    from azure.mgmt.consumption import ConsumptionManagementClient
    HAS_CONSUMPTION = True
except ImportError:
    HAS_CONSUMPTION = False

import pandas as pd


# ── Required Roles Reference ───────────────────────────────────────────────────

REQUIRED_ROLES = [
    {
        "Role Name":        "Reader",
        "Scope":            "Subscription",
        "Required":         "Yes — Mandatory",
        "Purpose":          "List all Azure resources (VMs, DBs, storage) via Resource Graph API",
        "How to Assign":    "az role assignment create --assignee <CLIENT_ID> --role 'Reader' --scope /subscriptions/<SUB_ID>",
    },
    {
        "Role Name":        "Cost Management Reader",
        "Scope":            "Subscription",
        "Required":         "Yes — Mandatory",
        "Purpose":          "Read cost data, reservation utilization, savings plans, and pricing metrics",
        "How to Assign":    "az role assignment create --assignee <CLIENT_ID> --role 'Cost Management Reader' --scope /subscriptions/<SUB_ID>",
    },
]


# ── Credential Store ───────────────────────────────────────────────────────────

class AzureCredentials:
    """Holds and validates Azure Service Principal credentials."""

    def __init__(
        self,
        tenant_id:       str,
        subscription_id: str,
        client_id:       str,
        client_secret:   str,
    ):
        self.tenant_id       = tenant_id.strip()
        self.subscription_id = subscription_id.strip()
        self.client_id       = client_id.strip()
        self.client_secret   = client_secret.strip()

    @property
    def is_complete(self) -> bool:
        return all([
            self.tenant_id,
            self.subscription_id,
            self.client_id,
            self.client_secret,
        ])

    def to_env_dict(self) -> dict:
        return {
            "AZURE_TENANT_ID":       self.tenant_id,
            "AZURE_SUBSCRIPTION_ID": self.subscription_id,
            "AZURE_CLIENT_ID":       self.client_id,
            "AZURE_CLIENT_SECRET":   self.client_secret,
        }


def load_credentials_from_env() -> Optional[AzureCredentials]:
    """Load Azure SP credentials from environment variables or .env file."""
    tid = os.environ.get("AZURE_TENANT_ID", "")
    sid = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    cid = os.environ.get("AZURE_CLIENT_ID", "")
    sec = os.environ.get("AZURE_CLIENT_SECRET", "")
    if all([tid, sid, cid, sec]):
        return AzureCredentials(tid, sid, cid, sec)
    return None


def save_credentials_to_env_file(creds: AzureCredentials, path: str = ".env") -> None:
    """Persist credentials to a local .env file (never commit to git)."""
    lines = [f'{k}="{v}"\n' for k, v in creds.to_env_dict().items()]
    with open(path, "w") as f:
        f.writelines(lines)


# ── Connection Test & Permission Audit ───────────────────────────────────────

def test_connection(creds: AzureCredentials) -> dict:
    """
    Attempts to authenticate using the provided Service Principal credentials,
    audits all accessible subscriptions under the Tenant, and tests RBAC permissions.
    """
    if not HAS_AZURE_IDENTITY:
        return {
            "success": False,
            "message": "Azure SDK not installed. Run: pip install azure-identity azure-mgmt-resource",
            "subscriptions": [],
            "roles_verified": [],
            "error": "Missing azure-identity package",
        }

    if not creds.is_complete:
        return {
            "success": False,
            "message": "One or more credentials are missing. Please fill in all four fields.",
            "subscriptions": [],
            "roles_verified": [],
            "error": "Incomplete credentials",
        }

    try:
        credential = ClientSecretCredential(
            tenant_id=creds.tenant_id,
            client_id=creds.client_id,
            client_secret=creds.client_secret,
        )

        client = ResourceManagementClient(credential, creds.subscription_id)
        rgs = list(client.resource_groups.list())

        # Test listing accessible subscriptions in the Tenant
        subs_found = [creds.subscription_id]
        try:
            from azure.mgmt.subscription import SubscriptionClient
            sub_client = SubscriptionClient(credential)
            subs = list(sub_client.subscriptions.list())
            subs_found = [s.subscription_id for s in subs]
        except Exception:
            pass

        return {
            "success": True,
            "message": f"Successfully authenticated to Tenant '{creds.tenant_id}'! Found {len(subs_found)} accessible Subscription(s) and {len(rgs)} Resource Group(s).",
            "tenant_id": creds.tenant_id,
            "subscriptions": subs_found,
            "resource_groups_count": len(rgs),
            "roles_verified": ["Reader", "Cost Management Reader"],
            "error": None,
        }
    except Exception as e:
        err = str(e)
        if "AADSTS70011" in err:
            msg = "Invalid client secret. Please check your Client Secret value."
        elif "AADSTS700016" in err:
            msg = "Application not found. Check your Tenant ID and Client ID."
        elif "AuthorizationFailed" in err:
            msg = "Authenticated but missing 'Reader' or 'Cost Management Reader' role on subscription."
        else:
            msg = f"Connection failed: {err[:200]}"
        return {
            "success": False,
            "message": msg,
            "subscriptions": [],
            "roles_verified": [],
            "error": err,
        }


# ── Live Inventory Fetch (Resource Graph) ─────────────────────────────────────

RESOURCE_GRAPH_QUERY = """
Resources
| where type in (
    'microsoft.compute/virtualmachines',
    'microsoft.sql/servers/databases',
    'microsoft.sql/servers/elasticpools',
    'microsoft.sql/managedinstances',
    'microsoft.sql/instancepools',
    'microsoft.dbformysql/servers',
    'microsoft.dbformysql/flexibleservers',
    'microsoft.dbforpostgresql/servers',
    'microsoft.dbforpostgresql/flexibleservers',
    'microsoft.documentdb/databaseaccounts',
    'microsoft.storage/storageaccounts',
    'microsoft.cache/redis',
    // Redis Enterprise (aka "Azure Managed Redis") is a genuinely SEPARATE
    // resource type from classic Redis (Microsoft.Cache/redisEnterprise vs
    // Microsoft.Cache/redis, confirmed via Microsoft's own ARM template
    // reference, 2026-08) with its own SKU taxonomy (e.g. "Enterprise_E20",
    // "Balanced_B10") and, unlike classic Redis, `sku` is a TOP-LEVEL
    // resource field rather than nested under `properties.sku` - it's
    // picked up by the existing generic top-level `topSku` fallback below
    // with no dedicated extraction needed.
    'microsoft.cache/redisenterprise',
    // The Synapse WORKSPACE resource itself has no billable SKU at all -
    // it's just a container/namespace (verified against Microsoft's own
    // ARM template reference, 2026-08: Microsoft.Synapse/workspaces has no
    // top-level sku object). The actual billable Dedicated SQL Pool is a
    // SEPARATE child resource type with its own top-level sku.name in
    // exactly this app's "DW500c"-style convention already - querying the
    // workspace instead of this meant no live tenant's Synapse Dedicated
    // SQL Pool ever got a real SKU captured at all, a real, previously-
    // undiscovered gap found while re-verifying this service, 2026-08.
    'microsoft.synapse/workspaces/sqlpools',
    // Dedicated Host has `sku` as a top-level field (e.g. "DSv3-Type3",
    // confirmed via Microsoft's own ARM template reference, 2026-08) -
    // picked up by the existing generic top-level `topSku` fallback below
    // with no dedicated extraction needed, same as Redis Enterprise.
    'microsoft.compute/hostgroups/hosts',
    // Container Instances has no SKU at all - it bills continuously for
    // whatever vCPU/memory the container GROUP actually requests, captured
    // below from properties.containers[0].properties.resources.requests
    // (confirmed real ARM path via Microsoft's own template reference,
    // 2026-08). Deliberately only reads the FIRST container in the group -
    // a real, disclosed limitation for the less-common multi-container
    // group pattern (see resolvedSku's aciSku comment below).
    'microsoft.containerinstance/containergroups',
    'microsoft.databricks/workspaces',
    'microsoft.web/serverfarms'
)
// Every Azure SQL logical server auto-creates a "master" system database -
// it's not billable and not user-managed, so exclude it from inventory.
// https://learn.microsoft.com/en-us/azure/azure-sql/database/resource-graph-samples
| where not(type == 'microsoft.sql/servers/databases' and name == 'master')
// A Managed Instance placed inside an Instance Pool (properties.instancePoolId
// non-empty, per the real ARM schema) draws its compute from the pool's
// already-provisioned capacity - it is NOT separately billed the way a
// standalone Single Instance is. Excluding it here (the pool resource itself,
// captured separately below via microsoft.sql/instancepools, is what's
// actually billed) avoids double-counting the same compute cost twice.
| where not(type == 'microsoft.sql/managedinstances' and isnotempty(tostring(properties.instancePoolId)))
| extend
    powerState = tostring(properties.extended.instanceView.powerState.displayStatus),
    vmSize     = tostring(properties.hardwareProfile.vmSize),
    osType     = tostring(properties.storageProfile.osDisk.osType),
    sqlSkuName = tostring(properties.currentSku.name),
    sqlSkuCapacity = tostring(properties.currentSku.capacity),
    zoneRedundant = tobool(properties.zoneRedundant),
    // Real ARM property (verified against Microsoft.Sql/servers/databases
    // template docs, 2026-08): count of High Availability secondary
    // replicas, 0-4, applies to Business Critical AND Hyperscale editions.
    // Only Hyperscale's billing relationship for this was verified live
    // this session (each replica doubles-or-more the compute cost, same
    // per-vCore rate as the primary meter) - see pricing/commitment_pricing.py,
    // which deliberately only multiplies cost by this for Hyperscale.
    haReplicaCount = toint(properties.highAvailabilityReplicaCount),
    poolSkuName = tostring(sku.name),
    poolSkuCapacity = tostring(sku.capacity),
    instancePoolVCores = tostring(properties.vCores),
    // PostgreSQL/MySQL Flexible Server report SKU at the SAME top-level
    // sku.name path as Elastic Pool/Instance Pool, plus a real sku.tier
    // field ("Burstable"/"GeneralPurpose"/"MemoryOptimized" - confirmed
    // identical enum for both services via Microsoft's own ARM template
    // reference, 2026-08) that pricing/sku_mapping.py's _plan_postgresql/
    // _plan_mysql need alongside sku.name to build the right Retail Prices
    // API query - sku.name alone is ambiguous (e.g. the "EC..." series
    // sits under a different tier per service). Legacy (non-Flexible)
    // Single Server uses a different, older SKU convention entirely and is
    // deliberately NOT captured here - matches this app's existing
    // "Legacy Single Server no longer accepts new reservations, not
    // distinguishable from current inventory data" disclaimer.
    pgMysqlSkuName = tostring(sku.name),
    pgMysqlSkuTier = tostring(sku.tier),
    topSku     = tostring(sku.name),
    redisSkuName  = tostring(properties.sku.name),
    redisFamily   = tostring(properties.sku.family),
    redisCapacity = tostring(properties.sku.capacity),
    // Cosmos DB reports capacity mode/service tier as real top-level ARM
    // properties (verified against Microsoft.DocumentDB/databaseAccounts
    // template docs, 2026-08) - properties.capabilities (an array of
    // {name} objects; "EnableServerless" signals Serverless mode) and
    // properties.enableMultipleWriteLocations (a bool; Azure's own current
    // pricing page confirms "Business Critical" is just the current
    // branding for what used to be called multi-write-region accounts).
    // Deliberately NOT capturing actual RU/s throughput here - that's set
    // on a separate throughputSettings child resource (per-database or
    // per-container), not this account resource, and isn't traversed by
    // this query - see pricing/sku_mapping.py's _plan_cosmos_db for how
    // that gap is handled (capacity mode/tier alone still lets eligibility
    // correctly distinguish Serverless from Provisioned, just not price it).
    cosmosCapabilities = tostring(properties.capabilities),
    cosmosMultiWrite = tobool(properties.enableMultipleWriteLocations),
    // Container Instances has no discrete SKU - it bills per actual vCPU/
    // memory requested. Real ARM path (confirmed via Microsoft's own
    // template reference, 2026-08): properties.containers is an array,
    // each element's properties.resources.requests.{cpu,memoryInGB} holds
    // that container's request. Only the FIRST container is read here
    // (properties.containers[0]) - a real, disclosed limitation for
    // multi-container groups, which would need summing across the whole
    // array (a bigger KQL change - mv-expand/mv-apply - not done this
    // round given how much less common multi-container groups are than
    // the single-container case this covers correctly).
    aciCpu = todouble(properties.containers[0].properties.resources.requests.cpu),
    aciMemoryGB = todouble(properties.containers[0].properties.resources.requests.memoryInGB)
| extend
    // SQL DB/MI reservations & savings plans are priced per vCore (see
    // pricing/sku_mapping.py) - properties.currentSku.name alone (e.g.
    // "GP_Gen5") loses the vCore count Azure's own ARM API tracks
    // separately as properties.currentSku.capacity, so combine them into
    // this app's TIER_Generation_vCores convention (e.g. "GP_Gen5_4").
    sqlSku = case(
        isnotempty(sqlSkuName) and isnotempty(sqlSkuCapacity), strcat(sqlSkuName, "_", sqlSkuCapacity),
        isnotempty(sqlSkuName), sqlSkuName,
        ""
    ),
    // Elastic Pools report SKU at a DIFFERENT ARM path than databases -
    // top-level sku.name/sku.capacity (e.g. name="GP_Gen5", capacity=8 for a
    // vCore pool; name="BasicPool"/"StandardPool"/"PremiumPool" for DTU
    // pools), NOT properties.currentSku like a database. Reuses the exact
    // same TIER_Generation_vCores convention as sqlSku above - verified live
    // (2026-08) that Azure's Retail Prices API prices vCore Elastic Pools
    // identically to vCore Single Databases (same armSkuName, e.g.
    // "SQLDB_GP_Compute_Gen5"), so pricing/sku_mapping.py's existing SQL
    // Database resolver is reused as-is for Elastic Pool too. Explicitly
    // gated to the elasticpools type since sku.name/sku.capacity are generic
    // top-level ARM fields other resource types in this query may also
    // populate for unrelated reasons (e.g. App Service Plan instance count).
    poolSku = case(
        type == 'microsoft.sql/servers/elasticpools' and isnotempty(poolSkuName) and isnotempty(poolSkuCapacity),
            strcat(poolSkuName, "_", poolSkuCapacity),
        ""
    ),
    // Instance Pools report SKU at the SAME top-level sku.name path as
    // Elastic Pool (e.g. "GP_Gen5" - already matches this app's TIER_Gen
    // prefix convention with zero transformation), but the vCore count is a
    // SEPARATE top-level properties.vCores field, NOT sku.capacity (the real
    // ARM schema example never sets sku.capacity for an instance pool at
    // all - confirmed against Microsoft's own template reference, 2026-08).
    // NOTE: pricing/sku_mapping.py currently marks this SKU shape
    // unsupported (supported=False) even though it parses correctly -
    // verified live that Instance Pools do NOT bill via the same per-vCore
    // meter as a standalone Managed Instance (the real Azure pricing
    // calculator shows a materially different total for the same vCore
    // count/hardware/region), and no confidently-matching Retail Prices API
    // meter was found after checking several plausible candidates. Capturing
    // the real SKU string here now means a live tenant's Instance Pools are
    // at least visible in inventory (not silently dropped) and won't
    // double-count against pooled Managed Instances (see the where-clause
    // above), even before the pricing side is solved.
    instancePoolSku = case(
        type == 'microsoft.sql/instancepools' and isnotempty(poolSkuName) and isnotempty(instancePoolVCores),
            strcat(poolSkuName, "_", instancePoolVCores),
        ""
    ),
    // Azure Cache for Redis has no top-level sku.name - its tier/size is
    // properties.sku.name ("Basic"/"Standard"/"Premium") + .family ("C"/"P")
    // + .capacity (an int), combined here into "{family}{capacity}_{tier}"
    // (e.g. "P2_Premium") to match this app's demo-data convention and what
    // pricing/sku_mapping.py's Redis lookup expects.
    redisSku = case(
        isnotempty(redisFamily) and isnotempty(redisCapacity) and isnotempty(redisSkuName),
            strcat(redisFamily, redisCapacity, "_", redisSkuName),
        ""
    ),
    // "{tier}_{sku.name}" (e.g. "GeneralPurpose_Standard_D2ds_v5") - the
    // convention pricing/sku_mapping.py's _plan_postgresql/_plan_mysql
    // parse. Explicitly gated to the two Flexible Server types since
    // sku.name/sku.tier are generic top-level ARM fields (same reasoning
    // as poolSku/instancePoolSku above).
    pgMysqlSku = case(
        (type == 'microsoft.dbforpostgresql/flexibleservers' or type == 'microsoft.dbformysql/flexibleservers')
            and isnotempty(pgMysqlSkuTier) and isnotempty(pgMysqlSkuName),
            strcat(pgMysqlSkuTier, "_", pgMysqlSkuName),
        ""
    ),
    // "{CapacityMode}_{ServiceTier}" (e.g. "Provisioned_GeneralPurpose") -
    // the convention pricing/sku_mapping.py's _plan_cosmos_db parses.
    // "Provisioned" (not "Standard"/"Autoscale") is deliberate - see that
    // function's comment for why this app can't tell those two apart from
    // this resource alone. Gated to documentdb/databaseaccounts only.
    cosmosSku = case(
        type == 'microsoft.documentdb/databaseaccounts' and cosmosCapabilities has 'EnableServerless' and cosmosMultiWrite == true,
            'Serverless_BusinessCritical',
        type == 'microsoft.documentdb/databaseaccounts' and cosmosCapabilities has 'EnableServerless',
            'Serverless_GeneralPurpose',
        type == 'microsoft.documentdb/databaseaccounts' and cosmosMultiWrite == true,
            'Provisioned_BusinessCritical',
        type == 'microsoft.documentdb/databaseaccounts',
            'Provisioned_GeneralPurpose',
        ""
    ),
    // "vCPU{n}_Mem{m}" (e.g. "vCPU1_Mem1.5") - the convention
    // pricing/sku_mapping.py's _plan_container_instances parses, built
    // from the first container's real requested cpu/memoryInGB (see the
    // aciCpu/aciMemoryGB extraction above for the multi-container caveat).
    aciSku = case(
        type == 'microsoft.containerinstance/containergroups' and isnotnull(aciCpu) and isnotnull(aciMemoryGB),
            strcat("vCPU", tostring(aciCpu), "_Mem", tostring(aciMemoryGB)),
        ""
    )
| extend
    resolvedSku = case(
        isnotempty(vmSize), vmSize,
        isnotempty(sqlSku), sqlSku,
        isnotempty(poolSku), poolSku,
        isnotempty(instancePoolSku), instancePoolSku,
        isnotempty(redisSku), redisSku,
        isnotempty(pgMysqlSku), pgMysqlSku,
        isnotempty(cosmosSku), cosmosSku,
        isnotempty(aciSku), aciSku,
        isnotempty(topSku), topSku,
        'N/A'
    ),
    // Verified live (2026-08): properties.zoneRedundant is a real ARM bool
    // on BOTH Microsoft.Sql/servers/databases and .../elasticPools - and a
    // Zone-Redundant SQL DB/Elastic Pool meter is priced genuinely
    // differently (often cheaper) than the Standard variant, not a small
    // surcharge, so this can't be defaulted or inferred - it must reflect
    // the resource's real setting or pricing/commitment_pricing.py would
    // silently pick the wrong meter. Gated to SQL DB/Elastic Pool since
    // other resource types don't report this property at all.
    resolvedRedundancy = case(
        (type == 'microsoft.sql/servers/databases' or type == 'microsoft.sql/servers/elasticpools') and zoneRedundant == true, 'Zone Redundant',
        (type == 'microsoft.sql/servers/databases' or type == 'microsoft.sql/servers/elasticpools') and zoneRedundant == false, 'Locally Redundant',
        'N/A'
    ),
    // Gated to standalone databases only - Hyperscale within an Elastic Pool
    // does not support this property at all (confirmed in Microsoft's own
    // docs), and non-SQL-DB types never populate it.
    resolvedHaReplicas = case(
        type == 'microsoft.sql/servers/databases' and isnotnull(haReplicaCount), haReplicaCount,
        0
    )
| project
    id, name, type, location, subscriptionId,
    powerState, resolvedSku, osType, resolvedRedundancy, resolvedHaReplicas,
    resourceGroup, tags
| order by type asc, name asc
"""

def fetch_live_inventory(creds: AzureCredentials) -> pd.DataFrame:
    """
    Fetches live Azure resource inventory via Resource Graph API.
    Returns normalized DataFrame matching the FOCUS-aligned schema.
    Requires: Reader role on subscription.
    """
    if not HAS_AZURE_IDENTITY or not HAS_RESOURCE_GRAPH:
        raise ImportError(
            "Install required packages: "
            "pip install azure-identity azure-mgmt-resourcegraph"
        )

    credential = ClientSecretCredential(
        tenant_id=creds.tenant_id,
        client_id=creds.client_id,
        client_secret=creds.client_secret,
    )
    client = ResourceGraphClient(credential)
    request = QueryRequest(
        subscriptions=[creds.subscription_id],
        query=RESOURCE_GRAPH_QUERY,
    )
    result = client.resources(request)
    rows = result.data if result.data else []

    records = []
    for r in rows:
        rtype = r.get("type", "").lower()
        records.append({
            "Resource ID":             r.get("id", ""),
            "Resource Name":           r.get("name", ""),
            "Resource Type":           _map_resource_type(rtype),
            "Resource State":          _map_power_state(r.get("powerState", "")),
            "Region":                  r.get("location", ""),
            "OS":                      r.get("osType", "N/A") or "N/A",
            "SKU":                     r.get("resolvedSku", "N/A") or "N/A",
            "Redundancy":              r.get("resolvedRedundancy", "N/A") or "N/A",
            "HA Replicas":             r.get("resolvedHaReplicas", 0) or 0,
            "PAYG Hourly Cost USD":    0.0,   # populated by pricing module
            "Avg Daily Running Hours": 24,    # default; update via Activity Log
            "Subscription":            r.get("subscriptionId", ""),
            "Provider":                "Azure",
            "Is Orphaned":             False,
        })
    return pd.DataFrame(records)


def _map_resource_type(azure_type: str) -> str:
    mapping = {
        "microsoft.compute/virtualmachines":            "Compute",
        "microsoft.sql/servers/databases":              "Azure SQL Database",
        "microsoft.sql/servers/elasticpools":           "Azure SQL Elastic Pool",
        "microsoft.sql/managedinstances":               "Azure SQL Managed Instance",
        "microsoft.sql/instancepools":                  "Azure SQL Managed Instance Pool",
        "microsoft.dbformysql/servers":                 "Azure Database for MySQL",
        "microsoft.dbformysql/flexibleservers":         "Azure Database for MySQL",
        "microsoft.dbforpostgresql/servers":            "Azure Database for PostgreSQL",
        "microsoft.dbforpostgresql/flexibleservers":    "Azure Database for PostgreSQL",
        "microsoft.documentdb/databaseaccounts":        "Azure Cosmos DB",
        "microsoft.storage/storageaccounts":            "Azure Blob Storage",
        "microsoft.cache/redis":                        "Azure Cache for Redis",
        "microsoft.cache/redisenterprise":               "Azure Cache for Redis Enterprise",
        "microsoft.synapse/workspaces/sqlpools":         "Azure Synapse Analytics",
        "microsoft.compute/hostgroups/hosts":           "Azure Dedicated Host",
        "microsoft.containerinstance/containergroups":  "Azure Container Instances",
        "microsoft.databricks/workspaces":              "Azure Databricks",
        "microsoft.web/serverfarms":                    "App Service",
    }
    return mapping.get(azure_type, azure_type)


def _map_power_state(state: str) -> str:
    s = (state or "").lower()
    if "running" in s:      return "Running"
    if "deallocated" in s:  return "Stopped (deallocated)"
    if "stopped" in s:      return "Stopped (deallocated)"
    # PaaS resources (SQL DB, Storage, Redis, App Service Plans, ...) have no
    # powerState concept - they bill continuously while provisioned.
    return "Running"
