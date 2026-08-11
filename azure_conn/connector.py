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
    'microsoft.dbformysql/servers',
    'microsoft.dbformysql/flexibleservers',
    'microsoft.dbforpostgresql/servers',
    'microsoft.dbforpostgresql/flexibleservers',
    'microsoft.documentdb/databaseaccounts',
    'microsoft.storage/storageaccounts',
    'microsoft.cache/redis',
    'microsoft.synapse/workspaces',
    'microsoft.databricks/workspaces',
    'microsoft.web/serverfarms'
)
// Every Azure SQL logical server auto-creates a "master" system database -
// it's not billable and not user-managed, so exclude it from inventory.
// https://learn.microsoft.com/en-us/azure/azure-sql/database/resource-graph-samples
| where not(type == 'microsoft.sql/servers/databases' and name == 'master')
| extend
    powerState = tostring(properties.extended.instanceView.powerState.displayStatus),
    vmSize     = tostring(properties.hardwareProfile.vmSize),
    osType     = tostring(properties.storageProfile.osDisk.osType),
    sqlSkuName = tostring(properties.currentSku.name),
    sqlSkuCapacity = tostring(properties.currentSku.capacity),
    poolSkuName = tostring(sku.name),
    poolSkuCapacity = tostring(sku.capacity),
    topSku     = tostring(sku.name),
    redisSkuName  = tostring(properties.sku.name),
    redisFamily   = tostring(properties.sku.family),
    redisCapacity = tostring(properties.sku.capacity)
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
    // Azure Cache for Redis has no top-level sku.name - its tier/size is
    // properties.sku.name ("Basic"/"Standard"/"Premium") + .family ("C"/"P")
    // + .capacity (an int), combined here into "{family}{capacity}_{tier}"
    // (e.g. "P2_Premium") to match this app's demo-data convention and what
    // pricing/sku_mapping.py's Redis lookup expects.
    redisSku = case(
        isnotempty(redisFamily) and isnotempty(redisCapacity) and isnotempty(redisSkuName),
            strcat(redisFamily, redisCapacity, "_", redisSkuName),
        ""
    )
| extend
    resolvedSku = case(
        isnotempty(vmSize), vmSize,
        isnotempty(sqlSku), sqlSku,
        isnotempty(poolSku), poolSku,
        isnotempty(redisSku), redisSku,
        isnotempty(topSku), topSku,
        'N/A'
    )
| project
    id, name, type, location, subscriptionId,
    powerState, resolvedSku, osType,
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
        "microsoft.dbformysql/servers":                 "Azure Database for MySQL",
        "microsoft.dbformysql/flexibleservers":         "Azure Database for MySQL",
        "microsoft.dbforpostgresql/servers":            "Azure Database for PostgreSQL",
        "microsoft.dbforpostgresql/flexibleservers":    "Azure Database for PostgreSQL",
        "microsoft.documentdb/databaseaccounts":        "Azure Cosmos DB",
        "microsoft.storage/storageaccounts":            "Azure Blob Storage",
        "microsoft.cache/redis":                        "Azure Cache for Redis",
        "microsoft.synapse/workspaces":                 "Azure Synapse Analytics",
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
