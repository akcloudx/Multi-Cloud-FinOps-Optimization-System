"""
azure/connector.py
Azure Service Principal authentication and live data connection module.

Handles:
  1. Credential validation (Service Principal via Client Secret)
  2. Live inventory fetch from Azure Resource Graph
  3. Live reservation fetch from Microsoft.Capacity (fetch_live_reservations)
     and live savings plan fetch from Microsoft.BillingBenefits
     (fetch_live_savings_plans) - both tenant-wide "List All" REST APIs, not
     Azure Consumption (see REQUIRED_TENANT_ROLES below for the real,
     tenant-scoped roles this actually requires - a subscription-scoped
     "Reservations Reader" grant, as this docstring used to describe, can
     never satisfy it).
  4. Required RBAC roles documentation (REQUIRED_SUBSCRIPTION_ROLES /
     REQUIRED_TENANT_ROLES below - the authoritative, verified reference;
     see each role's own "How to Assign" for the real az CLI command).
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
    from azure.mgmt.authorization import AuthorizationManagementClient
    HAS_AUTHORIZATION = True
except ImportError:
    HAS_AUTHORIZATION = False

try:
    from azure.mgmt.reservations import ReservationsMgmtClient
    HAS_RESERVATIONS = True
except ImportError:
    HAS_RESERVATIONS = False

try:
    from azure.mgmt.billingbenefits import BillingBenefitsMgmtClient
    HAS_BILLINGBENEFITS = True
except ImportError:
    HAS_BILLINGBENEFITS = False

import base64
import json
import pandas as pd


# ── Required Roles Reference ───────────────────────────────────────────────────
# Split by real assignment scope, verified live against Microsoft's own docs
# (2026-08): Reservations and Savings Plans are each their own tenant-level
# resource with their OWN separate RBAC system - "Reservations Reader" lives
# under /providers/Microsoft.Capacity, "Savings Plan Reader" under
# /providers/Microsoft.BillingBenefits - neither is a subscription role, and
# neither can be found by querying a subscription's role assignments (that
# was the actual bug in an earlier version of this file: it listed
# "Reservations Reader" with --scope /subscriptions/<SUB_ID>, which is simply
# the wrong scope - that assignment command would succeed at the CLI but the
# resulting grant would never actually apply to Reservations at all).
# Sources: https://learn.microsoft.com/en-us/azure/cost-management-billing/reservations/view-reservations
#          https://learn.microsoft.com/en-us/azure/cost-management-billing/savings-plan/permission-view-manage

REQUIRED_SUBSCRIPTION_ROLES = [
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
        "Purpose":          "Read cost and usage data, budgets, and pricing metrics",
        "How to Assign":    "az role assignment create --assignee <CLIENT_ID> --role 'Cost Management Reader' --scope /subscriptions/<SUB_ID>",
    },
]

REQUIRED_TENANT_ROLES = [
    {
        "Role Name":        "Reservations Reader",
        "Scope":            "Tenant (Microsoft Entra directory)",
        "Required":         "Yes — for Reserved Instance / Reserved Capacity purchase records and utilization",
        "Purpose":          "Read-only access to every reservation in the tenant - reservations aren't subscription resources, they don't inherit subscription-level permissions",
        "How to Assign":    'az role assignment create --assignee <CLIENT_ID> --role "Reservations Reader" --scope "/providers/Microsoft.Capacity"',
    },
    {
        "Role Name":        "Savings Plan Reader",
        "Scope":            "Tenant (Microsoft Entra directory)",
        "Required":         "Yes — for Savings Plan purchase records and utilization",
        "Purpose":          "Read-only access to every savings plan in the tenant - a separate RBAC system from Reservations, not the same role",
        "How to Assign":    'az role assignment create --assignee <CLIENT_ID> --role "Savings Plan Reader" --scope "/providers/Microsoft.BillingBenefits"',
    },
]

# Kept for anything that just wants the full reference table.
REQUIRED_ROLES = REQUIRED_SUBSCRIPTION_ROLES + REQUIRED_TENANT_ROLES


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


def _friendly_auth_error(err: str) -> str:
    """Translates common Microsoft Entra authentication error codes into
    plain-language explanations - shared by every function in this module
    that authenticates with a stored Service Principal, so a broken
    credential produces the SAME clear message everywhere (Manage Tenant's
    permission checks, sync results, connection test), not a different
    cryptic AADSTS code depending on which code path happened to hit it."""
    if "AADSTS7000215" in err:
        return (
            "Invalid client secret - this is almost always the Secret ID pasted instead of the Secret Value "
            "(Azure Portal > App registrations > your app > Certificates & secrets shows both, right next to "
            "each other, and both look like random strings). Regenerate the secret and copy the Value column "
            "specifically, then paste it into Service principal credentials below."
        )
    if "AADSTS7000222" in err:
        return "The client secret has expired - generate a new one in App registrations > Certificates & secrets."
    if "AADSTS700016" in err:
        return "Application not found - check the Tenant ID and Application ID are correct."
    if "AuthorizationFailed" in err:
        return "Authenticated successfully, but the required role isn't assigned at this scope yet."
    return err[:250]


def status_from_role_check(role_check: dict) -> tuple:
    """Turns a check_role_assignments()/check_tenant_role_assignments() result
    into a (status, missing, assigned) triple with THREE distinct states, not
    two - the real bug found live 2026-08: when the check itself couldn't run
    at all (checked=False - almost always a broken credential, e.g. an
    invalid client secret), the old code collapsed that into the same
    "missing_role" bucket as "checked fine, found N roles genuinely missing",
    producing a nonsensical "Missing None" in the UI. "error" is now its own
    status with the real, plain-language reason (see _friendly_auth_error
    above). Both missing AND assigned roles are returned (comma-joined
    strings) - not just missing - so the caller can show every required
    role's real state, not just the gaps.

    Lives here (not in app.py, where it was originally written) so both
    app.py (UI) and data/sync_pipeline.py (the shared ingestion pipeline,
    also run by the Function App's cron - which can't import app.py, since
    that pulls in streamlit and isn't shipped to the Function App at all)
    can use the exact same status logic without duplicating it."""
    if not role_check["checked"]:
        # Capped defensively - this string round-trips through a DB column
        # sized for error text (VARCHAR(1000) in Azure SQL, see
        # db/schema.py's _widen_column), not unbounded free text.
        err = (role_check.get("error") or "Could not check - unknown error.")[:900]
        return "error", err, None
    status = "ready" if role_check["ready"] else "missing_role"
    missing = ", ".join(role_check["missing_roles"]) or None
    assigned = ", ".join(role_check["assigned_roles"]) or None
    return status, missing, assigned


# ── Real per-subscription RBAC check ──────────────────────────────────────────

def _get_principal_object_id(credential) -> Optional[str]:
    """Extracts the Service Principal's AAD Object ID from the `oid` claim of
    its own access token - decoded locally (base64), not verified/re-issued,
    since we already trust this token (we just obtained it ourselves from
    AAD moments ago). Avoids a separate Microsoft Graph lookup (appId ->
    objectId), which would need yet another SDK/permission this app doesn't
    have. `oid` is a standard Azure AD token claim, not something specific to
    this app's setup."""
    token = credential.get_token("https://management.azure.com/.default")
    payload_b64 = token.token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    return claims.get("oid")


def _resolve_role_names(auth_client, scope: str, object_id: str) -> set:
    """Shared helper for both the subscription-scope and tenant-scope checks:
    lists role assignments at `scope` for this principal and resolves each
    role_definition_id to its real role name."""
    assignments = list(auth_client.role_assignments.list_for_scope(
        scope, filter=f"principalId eq '{object_id}'"
    ))
    names = set()
    for a in assignments:
        role_def = auth_client.role_definitions.get_by_id(a.role_definition_id)
        if role_def and role_def.role_name:
            names.add(role_def.role_name)
    return names


def check_role_assignments(creds: AzureCredentials, subscription_id: str) -> dict:
    """Real per-subscription RBAC check: lists the Service Principal's actual
    role assignments on `subscription_id` (Microsoft.Authorization/
    roleAssignments) and resolves each one's role_definition_id to its real
    role name via role_definitions.get_by_id - never a hardcoded built-in
    role GUID (those exist and are stable, but weren't independently
    verified for this project, so this resolves them live instead of
    guessing). Diffs against REQUIRED_SUBSCRIPTION_ROLES only (Reader, Cost
    Management Reader) - NOT the tenant-scoped Reservations/Savings Plan
    roles, which live under a completely different scope
    (/providers/Microsoft.Capacity, /providers/Microsoft.BillingBenefits) and
    can never show up in a subscription's role assignment list even when
    correctly granted - see check_tenant_role_assignments() for those.
    Replaces test_connection()'s old "roles_verified" stamp, which was never
    a real check, just an assumption made whenever a generic resource-listing
    call happened to succeed.

    Not live-tested against a real Azure tenant in this session (none
    available) - built strictly from documented SDK operations already used
    elsewhere in this file (ResourceManagementClient's list() pattern) and
    the standard OData `principalId eq` filter Azure's role-assignment list
    API documents. Flagging this honestly rather than claiming it's verified
    when it hasn't been exercised against a real subscription yet."""
    if not HAS_AUTHORIZATION:
        return {
            "checked": False, "ready": False, "assigned_roles": [], "missing_roles": [],
            "error": "azure-mgmt-authorization not installed",
        }
    try:
        credential = ClientSecretCredential(
            tenant_id=creds.tenant_id, client_id=creds.client_id, client_secret=creds.client_secret,
        )
        object_id = _get_principal_object_id(credential)
        auth_client = AuthorizationManagementClient(credential, subscription_id)
        assigned_role_names = _resolve_role_names(auth_client, f"/subscriptions/{subscription_id}", object_id)

        required = {r["Role Name"] for r in REQUIRED_SUBSCRIPTION_ROLES}
        missing = sorted(required - assigned_role_names)
        return {
            "checked": True,
            "ready": len(missing) == 0,
            "assigned_roles": sorted(assigned_role_names),
            "missing_roles": missing,
            "error": None,
        }
    except Exception as e:
        return {
            "checked": False, "ready": False, "assigned_roles": [], "missing_roles": [],
            "error": _friendly_auth_error(str(e)),
        }


_TENANT_ROLE_SCOPES = ["/providers/Microsoft.Capacity", "/providers/Microsoft.BillingBenefits"]


def check_tenant_role_assignments(creds: AzureCredentials) -> dict:
    """Real tenant-scope RBAC check for Reservations Reader and Savings Plan
    Reader - genuinely different scopes from anything else this app checks
    (/providers/Microsoft.Capacity and /providers/Microsoft.BillingBenefits
    respectively, per Microsoft's own docs - see REQUIRED_TENANT_ROLES above),
    since Reservations and Savings Plans are each their own tenant-level
    resource, not subscription resources, and don't inherit subscription
    permissions. Loops both scopes and merges the resolved role names before
    diffing against REQUIRED_TENANT_ROLES, same pattern as
    check_role_assignments() (shares _resolve_role_names/_get_principal_object_id).

    AuthorizationManagementClient still needs *some* subscription_id to
    construct (an SDK requirement for API versioning), even though the actual
    scope queried here is tenant-wide, not that subscription - creds.subscription_id
    is used for exactly that, and has no bearing on which scope is checked.

    Not live-tested against a real Azure tenant in this session (none
    available) - built strictly from Microsoft's own documented scope strings
    and the same list_for_scope/role_definitions.get_by_id pattern already
    used (and disclosed as not-yet-live-tested) in check_role_assignments()."""
    if not HAS_AUTHORIZATION:
        return {
            "checked": False, "ready": False, "assigned_roles": [], "missing_roles": [],
            "error": "azure-mgmt-authorization not installed",
        }
    try:
        credential = ClientSecretCredential(
            tenant_id=creds.tenant_id, client_id=creds.client_id, client_secret=creds.client_secret,
        )
        object_id = _get_principal_object_id(credential)
        auth_client = AuthorizationManagementClient(credential, creds.subscription_id)

        assigned_role_names = set()
        for scope in _TENANT_ROLE_SCOPES:
            assigned_role_names |= _resolve_role_names(auth_client, scope, object_id)

        required = {r["Role Name"] for r in REQUIRED_TENANT_ROLES}
        missing = sorted(required - assigned_role_names)
        return {
            "checked": True,
            "ready": len(missing) == 0,
            "assigned_roles": sorted(assigned_role_names),
            "missing_roles": missing,
            "error": None,
        }
    except Exception as e:
        return {
            "checked": False, "ready": False, "assigned_roles": [], "missing_roles": [],
            "error": _friendly_auth_error(str(e)),
        }


def list_accessible_subscriptions(creds: AzureCredentials) -> list:
    """Real subscription enumeration for the Manage Tenant page's "Sync
    subscriptions" action - returns [{"subscription_id": ..., "display_name":
    ...}, ...], empty on failure (best-effort, matching test_connection()'s
    existing fallback behavior for this same SDK call). Keeps app.py from
    needing to import Azure SDK classes directly - it only ever calls into
    this module's own functions."""
    if not HAS_AZURE_IDENTITY:
        return []
    try:
        credential = ClientSecretCredential(
            tenant_id=creds.tenant_id, client_id=creds.client_id, client_secret=creds.client_secret,
        )
        from azure.mgmt.subscription import SubscriptionClient
        sub_client = SubscriptionClient(credential)
        return [
            {"subscription_id": s.subscription_id, "display_name": s.display_name}
            for s in sub_client.subscriptions.list()
        ]
    except Exception:
        return []


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

        # Real per-role check (replaces the old hardcoded assumption) - best
        # effort: if it can't run (package missing, unexpected API shape),
        # fall back to the same "Reader worked, so assume these" inference
        # rather than fail the whole connection test over a secondary check.
        role_check = check_role_assignments(creds, creds.subscription_id)
        if role_check["checked"]:
            roles_verified = role_check["assigned_roles"]
            roles_missing = role_check["missing_roles"]
        else:
            roles_verified = ["Reader", "Cost Management Reader"]
            roles_missing = []

        return {
            "success": True,
            "message": f"Successfully authenticated to Tenant '{creds.tenant_id}'! Found {len(subs_found)} accessible Subscription(s) and {len(rgs)} Resource Group(s).",
            "tenant_id": creds.tenant_id,
            "subscriptions": subs_found,
            "resource_groups_count": len(rgs),
            "roles_verified": roles_verified,
            "roles_missing": roles_missing,
            "error": None,
        }
    except Exception as e:
        err = str(e)
        msg = _friendly_auth_error(err)
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
    'microsoft.web/serverfarms',
    // Microsoft Fabric capacities have `sku` as a top-level field (e.g.
    // "F64", confirmed via Microsoft's own ARM template reference, 2026-08)
    // - picked up by the existing generic top-level `topSku` fallback below
    // with no dedicated extraction needed, same as Dedicated Host/Redis
    // Enterprise.
    'microsoft.fabric/capacities',
    // Azure Data Explorer (Kusto) clusters have `sku` as a top-level field
    // (name, tier: 'Basic'|'Standard', capacity: int node count) - confirmed
    // via Microsoft's own REST API reference, 2026-08. Unlike Dedicated
    // Host/Redis Enterprise/Fabric, this app needs BOTH sku.name AND
    // sku.tier/sku.capacity (pricing genuinely depends on tier, and node
    // count is a real billing multiplier - see pricing/sku_mapping.py's
    // _plan_data_explorer), so it gets its own dedicated extraction below
    // rather than riding the generic topSku fallback. Also has a genuine
    // Running/Stopped lifecycle (properties.state, confirmed via Microsoft's
    // REST API reference, 2026-08 - clusters can be manually or
    // automatically stopped after inactivity), unlike most PaaS types here.
    'microsoft.kusto/clusters'
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
    // Data Explorer clusters report tier/capacity at the SAME top-level
    // sku.tier/sku.capacity path as PostgreSQL/MySQL Flexible Server above
    // (a generic ARM AzureSku shape reused across resource types) - named
    // separately here for clarity, same reasoning as poolSkuName/
    // pgMysqlSkuName/topSku all independently reading sku.name below.
    adxSkuTier = tostring(sku.tier),
    adxSkuCapacity = tostring(sku.capacity),
    // Real state values (Creating/Running/Stopping/Stopped/Starting/...) -
    // a DIFFERENT ARM path than VMs' properties.extended.instanceView.
    // powerState.displayStatus, confirmed via Microsoft's own REST API
    // reference, 2026-08. Combined into the shared powerState field below.
    adxState = tostring(properties.state),
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
    // "{tier}_{vmSize}_{capacity}" (e.g. "Standard_Standard_D13_v2_2") - the
    // convention pricing/sku_mapping.py's _plan_data_explorer parses. tier
    // gates whether the Engine Cluster Markup fee applies at all (Basic/Dev
    // tier has none - verified live), capacity is the real node count that
    // multiplies the flat per-node rate. Gated to microsoft.kusto/clusters
    // since sku.name/tier/capacity are generic top-level ARM fields.
    adxSku = case(
        type == 'microsoft.kusto/clusters' and isnotempty(adxSkuTier) and isnotempty(topSku) and isnotempty(adxSkuCapacity),
            strcat(adxSkuTier, "_", topSku, "_", adxSkuCapacity),
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
        isnotempty(adxSku), adxSku,
        isnotempty(topSku), topSku,
        'N/A'
    ),
    // Data Explorer clusters report state at properties.state (Running/
    // Stopped/Starting/Stopping/Creating/...), a different ARM path than
    // VMs' powerState - combined into the same shared field _map_power_state
    // already reads, so no Python-side change is needed (it already does a
    // case-insensitive "running"/"stopped"/"deallocated" substring match).
    resolvedPowerState = case(
        type == 'microsoft.kusto/clusters', adxState,
        powerState
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
    resolvedPowerState, resolvedSku, osType, resolvedRedundancy, resolvedHaReplicas,
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
            "Resource State":          _map_power_state(r.get("resolvedPowerState", "")),
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
        "microsoft.fabric/capacities":                   "Microsoft Fabric",
        "microsoft.kusto/clusters":                      "Azure Data Explorer",
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


# ── Live Reservation / Savings Plan Fetch ────────────────────────────────────
# Both "List All" APIs are genuinely tenant-wide (not subscription-scoped -
# see REQUIRED_TENANT_ROLES above) and both embed 1/7/30-day utilization
# directly in the same response - verified against Microsoft Learn AND the
# actually-installed azure-mgmt-reservations 3.0.0 / azure-mgmt-billingbenefits
# 1.0.0b2 SDK model source (2026-08), not guessed from search snippets. No
# separate Microsoft.CostManagement/benefitUtilizationSummaries call needed.

def _parse_utilization_aggregates(utilization) -> dict:
    """Shared by both fetch functions below - the SDK model shape is
    identical for Reservations (ReservationsPropertiesUtilization) and
    Savings Plans (Utilization): {trend, aggregates: [{grain, grain_unit,
    value, value_unit}]}. grain is the trailing-window size in days (1/7/30);
    value is the already-computed utilization percentage (0-100) for that
    window - confirmed against the installed SDK's model fields, not
    inferred."""
    result = {"trend": None, "1day": None, "7day": None, "30day": None}
    if not utilization:
        return result
    result["trend"] = utilization.trend
    for agg in (utilization.aggregates or []):
        if agg.grain == 1:
            result["1day"] = agg.value
        elif agg.grain == 7:
            result["7day"] = agg.value
        elif agg.grain == 30:
            result["30day"] = agg.value
    return result


def _iso(value) -> Optional[str]:
    """date/datetime -> ISO string, matching db.schema.ReservationPurchase/
    SavingsPlanPurchase's String-typed date columns. None-safe."""
    return value.isoformat() if value is not None else None


def fetch_live_reservations(creds: AzureCredentials) -> pd.DataFrame:
    """Tenant-wide Reservation enumeration via Microsoft.Capacity/reservations
    "List All" (GET /providers/Microsoft.Capacity/reservations?api-version=
    2022-11-01). Requires Reservations Reader at /providers/Microsoft.Capacity
    - see REQUIRED_TENANT_ROLES / check_tenant_role_assignments() above.
    ReservationsMgmtClient is constructed from the credential alone (no
    subscription_id) - confirmed against the installed SDK's client
    constructor - reservations genuinely aren't subscription resources, the
    same finding check_tenant_role_assignments() already made for RBAC.
    Returns a DataFrame shaped field-for-field to db.schema.ReservationPurchase
    (see db/seed.py's RESERVATION_PURCHASES for the same field set hand-built
    from real API examples). No DB writes here - see data/sync_pipeline.py."""
    if not HAS_AZURE_IDENTITY or not HAS_RESERVATIONS:
        raise ImportError(
            "Install required packages: pip install azure-identity azure-mgmt-reservations"
        )

    credential = ClientSecretCredential(
        tenant_id=creds.tenant_id, client_id=creds.client_id, client_secret=creds.client_secret,
    )
    client = ReservationsMgmtClient(credential)

    records = []
    for r in client.reservation.list_all():
        props = r.properties
        order_id, reservation_id = "", ""
        if r.id:
            # id shape: /providers/Microsoft.Capacity/reservationOrders/{orderId}/reservations/{id}
            head, _, tail = r.id.partition("/reservationOrders/")
            order_id, _, reservation_id = tail.partition("/reservations/")
        scope_props = props.applied_scope_properties if props else None
        util = _parse_utilization_aggregates(props.utilization if props else None)
        records.append({
            "reservation_order_id":          order_id,
            "reservation_id":                reservation_id or (r.name or ""),
            "name":                          r.name or "",
            "type":                          r.type or "",
            "location":                      r.location or "",
            "sku_name":                      r.sku.name if r.sku else "",
            "sku_description":               props.sku_description if props else None,
            "reserved_resource_type":        props.reserved_resource_type if props else "",
            "instance_flexibility":          props.instance_flexibility if props else None,
            "applied_scope_type":            props.applied_scope_type if props else "",
            "applied_scope_display_name":    scope_props.display_name if scope_props else None,
            "applied_scope_subscription_id": scope_props.subscription_id if scope_props else None,
            "billing_plan":                  props.billing_plan if props else "",
            "term":                          props.term if props else "",
            "quantity":                      props.quantity if props and props.quantity is not None else 0,
            "provisioning_state":            props.provisioning_state if props else "",
            "renew":                         bool(props.renew) if props else False,
            "purchase_date":                 _iso(props.purchase_date) if props else None,
            "purchase_date_time":            _iso(props.purchase_date_time) if props else "",
            "effective_date_time":           _iso(props.effective_date_time) if props else "",
            "benefit_start_time":            _iso(props.benefit_start_time) if props else "",
            "expiry_date":                   _iso(props.expiry_date) if props else None,
            "expiry_date_time":              _iso(props.expiry_date_time) if props else "",
            "utilization_trend":             util["trend"],
            "utilization_1day_pct":          util["1day"],
            "utilization_7day_pct":          util["7day"],
            "utilization_30day_pct":         util["30day"],
            "provider":                      "Azure",
        })
    return pd.DataFrame(records)


def fetch_live_savings_plans(creds: AzureCredentials) -> pd.DataFrame:
    """Tenant-wide Savings Plan enumeration via Microsoft.BillingBenefits/
    savingsPlans "List All" (GET /providers/Microsoft.BillingBenefits/
    savingsPlans?api-version=2022-11-01). Requires Savings Plan Reader at
    /providers/Microsoft.BillingBenefits - see REQUIRED_TENANT_ROLES /
    check_tenant_role_assignments() above. BillingBenefitsMgmtClient's
    constructor requires *some* subscription_id (a real SDK requirement,
    confirmed against the installed client's __init__ signature - same
    pattern already documented for AuthorizationManagementClient in
    check_tenant_role_assignments()) even though savings_plan.list_all()
    itself queries tenant-wide, not that subscription.
    commitment.amount at commitment.grain == 'Hourly' IS the real $/hr
    commitment rate - unlike Reservations (which carry no $ amount at all in
    this response), no separate pricing lookup is needed for Savings Plans;
    see pricing/commitment_mapping.py. SavingsPlanModel exposes its
    properties as flattened attributes (s.term, s.commitment, ... - not
    nested under s.properties) - confirmed against the installed SDK model.
    Returns a DataFrame shaped field-for-field to db.schema.SavingsPlanPurchase.
    No DB writes here - see data/sync_pipeline.py."""
    if not HAS_AZURE_IDENTITY or not HAS_BILLINGBENEFITS:
        raise ImportError(
            "Install required packages: pip install azure-identity azure-mgmt-billingbenefits"
        )

    credential = ClientSecretCredential(
        tenant_id=creds.tenant_id, client_id=creds.client_id, client_secret=creds.client_secret,
    )
    client = BillingBenefitsMgmtClient(credential, creds.subscription_id)

    records = []
    for s in client.savings_plan.list_all():
        order_id, plan_id = "", ""
        if s.id:
            # id shape: /providers/Microsoft.BillingBenefits/savingsPlanOrders/{orderId}/savingsPlans/{id}
            head, _, tail = s.id.partition("/savingsPlanOrders/")
            order_id, _, plan_id = tail.partition("/savingsPlans/")
        commitment = s.commitment
        util = _parse_utilization_aggregates(s.utilization)
        records.append({
            "savings_plan_order_id":    order_id,
            "savings_plan_id":          plan_id or (s.name or ""),
            "name":                     s.name or "",
            "type":                     s.type or "",
            "sku_name":                 s.sku.name if s.sku else "",
            "billing_scope_id":         s.billing_scope_id or "",
            "billing_plan":             s.billing_plan or "",
            "commitment_grain":         commitment.grain if commitment else "",
            "commitment_currency_code": commitment.currency_code if commitment else "",
            "commitment_amount":        commitment.amount if commitment and commitment.amount is not None else 0.0,
            "applied_scope_type":       s.applied_scope_type or "",
            "display_name":             s.display_name,
            "term":                     s.term or "",
            "provisioning_state":       s.provisioning_state or "",
            "renew":                    bool(s.renew),
            "purchase_date_time":       _iso(s.purchase_date_time) or "",
            "effective_date_time":      _iso(s.effective_date_time) or "",
            "benefit_start_time":       _iso(s.benefit_start_time) or "",
            "expiry_date_time":         _iso(s.expiry_date_time) or "",
            "utilization_trend":        util["trend"],
            "utilization_1day_pct":     util["1day"],
            "utilization_7day_pct":     util["7day"],
            "utilization_30day_pct":    util["30day"],
            "provider":                 "Azure",
        })
    return pd.DataFrame(records)
