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

try:
    from azure.mgmt.datafactory import DataFactoryManagementClient
    HAS_DATAFACTORY = True
except ImportError:
    HAS_DATAFACTORY = False

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

# Deliberately NOT part of REQUIRED_SUBSCRIPTION_ROLES / REQUIRED_ROLES below -
# check_role_assignments() treats every entry in REQUIRED_SUBSCRIPTION_ROLES
# as mandatory (diffs the whole list against the Service Principal's actual
# assignments with no "optional" filtering at all), so adding this there
# would make every tenant without it show "missing_role" even though nothing
# else in this app depends on it. This is narrow and genuinely optional: it
# only enables Started/Stopped status for Azure-SSIS Integration Runtime
# nodes specifically (azure_conn/connector.py's _fetch_ssis_ir_states) -
# every other capability in this app keeps working without it.
#
# No built-in Azure role covers this at Reader scope - verified live against
# Microsoft's own built-in role reference (2026-08): the ONLY built-in role
# with Microsoft.DataFactory/factories/integrationRuntimes/getStatus/action
# is "Data Factory Contributor", a full create/modify/delete role, because
# getStatus is an "/action"-suffixed RPC operation, not "/read" - Azure's
# generic Reader role's "*/read" wildcard genuinely does not match it. A
# custom role scoped to just this one action is the least-privileged option.
OPTIONAL_SUBSCRIPTION_ROLES = [
    {
        "Role Name":        "FinOps SSIS IR Status Reader",
        "Scope":            "Subscription (custom role - must be created once before it can be assigned)",
        "Required":         "No — only for Azure-SSIS Integration Runtime Started/Stopped status; everything else in this app works without it",
        "Purpose":          "Read-only access to a single narrow action (getStatus) - no built-in role exists at Reader scope for this",
        "How to Assign":    (
            'az role definition create --role-definition \'{"Name": "FinOps SSIS IR Status Reader", '
            '"Description": "Read-only Started/Stopped status for Azure-SSIS Integration Runtimes.", '
            '"Actions": ["Microsoft.DataFactory/factories/integrationRuntimes/getStatus/action"], '
            '"AssignableScopes": ["/subscriptions/<SUB_ID>"]}\' '
            "&& az role assignment create --assignee <CLIENT_ID> --role \"FinOps SSIS IR Status Reader\" --scope /subscriptions/<SUB_ID>"
        ),
    },
]

# Kept for anything that just wants the full reference table. Deliberately
# excludes OPTIONAL_SUBSCRIPTION_ROLES - see its own comment above.
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
    # Widened 250 -> 450 chars (2026-08-30, real bug caught live on a
    # production deployment: a genuine, unrecognized BadRequest from Azure
    # got cut off mid-sentence at 250 chars, right where the actual
    # diagnostic detail - the real Code/Message the user would need to
    # actually fix the problem - would have started). Azure SDK
    # HttpResponseError's own str() is genuinely verbose (often repeats
    # its own "Message:"/support-timestamp boilerplate), so a short cap
    # here isn't fixing that verbosity, just hiding the useful part along
    # with it. 450 leaves headroom under the real DB column size this
    # ultimately lands in (cloud_tenants.last_sync_message, VARCHAR(500) -
    # see db/schema.py) once the "API Sync Failed: " prefix is added back
    # by the caller.
    return err[:450]


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

# Split into 9 independent, self-contained queries (2026-08-30, real
# production regression + direct user request: two separate obscure KQL
# parser failures in one giant 437-line query, each requiring several
# round-trips through Resource Graph Explorer to even locate, blocking
# ALL inventory - not just the one affected resource type - the whole
# time). Each group below scopes its own `where type`, only the `extend`
# fields that group needs, and its own resolvedSku logic - reusing the
# exact field-extraction paths already verified in the prior single-query
# version (not rewritten), just partitioned so a problem in one group can
# never block the others, and so any one group is small enough to paste
# into Resource Graph Explorer and get a useful answer in seconds. Every
# group verified independently (paren/bracket balance + a live Resource
# Graph Explorer run, all 9 passing) before being wired in here.
# fetch_live_inventory() below calls each group separately and
# concatenates the results - same final row shape (id/name/type/location/
# subscriptionId/resolvedPowerState/resolvedSku/osType/resolvedRedundancy/
# resolvedHaReplicas/resourceGroup/tags) as the old single query produced,
# so nothing downstream of the API call needed to change.
RESOURCE_GRAPH_QUERIES = {
    "vms": """
Resources
| where type == 'microsoft.compute/virtualmachines'
| extend
    powerState = tostring(properties.extended.instanceView.powerState.displayStatus),
    vmSize     = tostring(properties.hardwareProfile.vmSize),
    osType     = tostring(properties.storageProfile.osDisk.osType)
| project
    id, name, type, location, subscriptionId,
    resolvedPowerState = powerState, resolvedSku = vmSize, osType,
    resolvedRedundancy = 'N/A', resolvedHaReplicas = 0,
    resourceGroup, tags
""",

    # Azure SQL Database/Elastic Pool/Managed Instance/Instance Pool -
    # sqlSku uses properties.currentSku (DB), poolSku/instancePoolSku use
    # top-level sku (Pool/Instance Pool). zoneRedundant/haReplicaCount only
    # apply to standalone databases. master DB and pooled-MI exclusions kept
    # exactly as before.
    "sql": """
Resources
| where type in (
    'microsoft.sql/servers/databases',
    'microsoft.sql/servers/elasticpools',
    'microsoft.sql/managedinstances',
    'microsoft.sql/instancepools'
)
| where not(type == 'microsoft.sql/servers/databases' and name == 'master')
| where not(type == 'microsoft.sql/managedinstances' and isnotempty(tostring(properties.instancePoolId)))
| extend
    sqlSkuName = tostring(properties.currentSku.name),
    sqlSkuCapacity = tostring(properties.currentSku.capacity),
    zoneRedundant = tobool(properties.zoneRedundant),
    haReplicaCount = toint(properties.highAvailabilityReplicaCount),
    poolSkuName = tostring(sku.name),
    poolSkuCapacity = tostring(sku.capacity),
    instancePoolVCores = tostring(properties.vCores)
| extend
    sqlSku = case(
        isnotempty(sqlSkuName) and isnotempty(sqlSkuCapacity), strcat(sqlSkuName, "_", sqlSkuCapacity),
        isnotempty(sqlSkuName), sqlSkuName,
        ""
    ),
    poolSku = case(
        type == 'microsoft.sql/servers/elasticpools' and isnotempty(poolSkuName) and isnotempty(poolSkuCapacity),
            strcat(poolSkuName, "_", poolSkuCapacity),
        ""
    ),
    instancePoolSku = case(
        type == 'microsoft.sql/instancepools' and isnotempty(poolSkuName) and isnotempty(instancePoolVCores),
            strcat(poolSkuName, "_", instancePoolVCores),
        ""
    )
| extend
    resolvedSku = case(
        isnotempty(sqlSku), sqlSku,
        isnotempty(poolSku), poolSku,
        isnotempty(instancePoolSku), instancePoolSku,
        'N/A'
    ),
    resolvedRedundancy = case(
        (type == 'microsoft.sql/servers/databases' or type == 'microsoft.sql/servers/elasticpools') and zoneRedundant == true, 'Zone Redundant',
        (type == 'microsoft.sql/servers/databases' or type == 'microsoft.sql/servers/elasticpools') and zoneRedundant == false, 'Locally Redundant',
        'N/A'
    ),
    resolvedHaReplicas = case(
        type == 'microsoft.sql/servers/databases' and isnotnull(haReplicaCount), haReplicaCount,
        0
    )
| project
    id, name, type, location, subscriptionId,
    resolvedPowerState = 'Running', resolvedSku, osType = 'N/A',
    resolvedRedundancy, resolvedHaReplicas,
    resourceGroup, tags
""",

    # MySQL/PostgreSQL Flexible Server (tier+name combined) and their
    # legacy (non-Flexible) predecessors, which report SKU at the same
    # top-level sku.name path with no tier concept.
    "flexible_servers": """
Resources
| where type in (
    'microsoft.dbformysql/servers',
    'microsoft.dbformysql/flexibleservers',
    'microsoft.dbforpostgresql/servers',
    'microsoft.dbforpostgresql/flexibleservers'
)
| extend
    pgMysqlSkuName = tostring(sku.name),
    pgMysqlSkuTier = tostring(sku.tier)
| extend
    resolvedSku = case(
        (type == 'microsoft.dbforpostgresql/flexibleservers' or type == 'microsoft.dbformysql/flexibleservers')
            and isnotempty(pgMysqlSkuTier) and isnotempty(pgMysqlSkuName),
            strcat(pgMysqlSkuTier, "_", pgMysqlSkuName),
        isnotempty(pgMysqlSkuName), pgMysqlSkuName,
        'N/A'
    )
| project
    id, name, type, location, subscriptionId,
    resolvedPowerState = 'Running', resolvedSku, osType = 'N/A',
    resolvedRedundancy = 'N/A', resolvedHaReplicas = 0,
    resourceGroup, tags
""",

    # Cosmos DB (RU/s) accounts + DocumentDB (vCore) mongoClusters - two
    # separate ARM resource types sharing the "Cosmos DB" pricing family.
    # properties["sharding"]["shardCount"] uses bracket notation, not dot
    # notation - real fix for a live ParserFailure (dot notation on this
    # specific property name collided with something in Resource Graph's
    # own grammar; bracket notation is KQL's documented escape for this).
    "cosmos_documentdb": """
Resources
| where type in (
    'microsoft.documentdb/databaseaccounts',
    'microsoft.documentdb/mongoclusters'
)
| extend
    cosmosCapabilities = tostring(properties.capabilities),
    cosmosMultiWrite = tobool(properties.enableMultipleWriteLocations),
    documentDbTier = tostring(properties.compute.tier),
    documentDbShardCount = toint(properties["sharding"]["shardCount"])
| extend
    resolvedSku = case(
        type == 'microsoft.documentdb/databaseaccounts' and cosmosCapabilities has 'EnableServerless' and cosmosMultiWrite == true,
            'Serverless_BusinessCritical',
        type == 'microsoft.documentdb/databaseaccounts' and cosmosCapabilities has 'EnableServerless',
            'Serverless_GeneralPurpose',
        type == 'microsoft.documentdb/databaseaccounts' and cosmosMultiWrite == true,
            'Provisioned_BusinessCritical',
        type == 'microsoft.documentdb/databaseaccounts',
            'Provisioned_GeneralPurpose',
        type == 'microsoft.documentdb/mongoclusters' and isnotempty(documentDbTier) and isnotnull(documentDbShardCount),
            strcat(documentDbTier, "_", tostring(documentDbShardCount)),
        'N/A'
    )
| project
    id, name, type, location, subscriptionId,
    resolvedPowerState = 'Running', resolvedSku, osType = 'N/A',
    resolvedRedundancy = 'N/A', resolvedHaReplicas = 0,
    resourceGroup, tags
""",

    # Classic Redis (nested properties.sku.*) and Redis Enterprise
    # (top-level sku.name) - genuinely different ARM shapes.
    "cache": """
Resources
| where type in ('microsoft.cache/redis', 'microsoft.cache/redisenterprise')
| extend
    redisSkuName  = tostring(properties.sku.name),
    redisFamily   = tostring(properties.sku.family),
    redisCapacity = tostring(properties.sku.capacity),
    topSku        = tostring(sku.name)
| extend
    resolvedSku = case(
        isnotempty(redisFamily) and isnotempty(redisCapacity) and isnotempty(redisSkuName),
            strcat(redisFamily, redisCapacity, "_", redisSkuName),
        isnotempty(topSku), topSku,
        'N/A'
    )
| project
    id, name, type, location, subscriptionId,
    resolvedPowerState = 'Running', resolvedSku, osType = 'N/A',
    resolvedRedundancy = 'N/A', resolvedHaReplicas = 0,
    resourceGroup, tags
""",

    # Storage Accounts (top-level sku.name) + Managed Disks (sku.name +
    # diskSizeGB - Azure derives the real P30/E20/S60-style tier label from
    # size, not from sku.name alone).
    "storage_disk": """
Resources
| where type in ('microsoft.storage/storageaccounts', 'microsoft.compute/disks')
| extend
    diskSkuName = tostring(sku.name),
    diskSizeGB  = toint(properties.diskSizeGB),
    topSku      = tostring(sku.name)
| extend
    resolvedSku = case(
        type == 'microsoft.compute/disks' and isnotempty(diskSkuName) and isnotnull(diskSizeGB),
            strcat(diskSkuName, "_", tostring(diskSizeGB)),
        isnotempty(topSku), topSku,
        'N/A'
    )
| project
    id, name, type, location, subscriptionId,
    resolvedPowerState = 'Running', resolvedSku, osType = 'N/A',
    resolvedRedundancy = 'N/A', resolvedHaReplicas = 0,
    resourceGroup, tags
""",

    # Databricks/Synapse Dedicated SQL Pools/Fabric Capacities (all generic
    # top-level sku.name) + Data Explorer clusters (own sku.tier/
    # sku.capacity + a real Running/Stopped state at properties.state,
    # unlike the other 3 PaaS types here).
    "analytics": """
Resources
| where type in (
    'microsoft.databricks/workspaces',
    'microsoft.synapse/workspaces/sqlpools',
    'microsoft.kusto/clusters',
    'microsoft.fabric/capacities'
)
| extend
    adxSkuTier = tostring(sku.tier),
    adxSkuCapacity = tostring(sku.capacity),
    adxState = tostring(properties.state),
    topSku = tostring(sku.name)
| extend
    resolvedSku = case(
        type == 'microsoft.kusto/clusters' and isnotempty(adxSkuTier) and isnotempty(topSku) and isnotempty(adxSkuCapacity),
            strcat(adxSkuTier, "_", topSku, "_", adxSkuCapacity),
        isnotempty(topSku), topSku,
        'N/A'
    ),
    resolvedPowerState = case(
        type == 'microsoft.kusto/clusters', adxState,
        'Running'
    )
| project
    id, name, type, location, subscriptionId,
    resolvedPowerState, resolvedSku, osType = 'N/A',
    resolvedRedundancy = 'N/A', resolvedHaReplicas = 0,
    resourceGroup, tags
""",

    # Dedicated Host + App Service Plans (generic top-level sku.name) +
    # Container Instances (no SKU at all - bills per requested vCPU/memory
    # on the first container in the group).
    "compute_misc": """
Resources
| where type in (
    'microsoft.compute/hostgroups/hosts',
    'microsoft.containerinstance/containergroups',
    'microsoft.web/serverfarms'
)
| extend
    aciCpu = todouble(properties.containers[0].properties.resources.requests.cpu),
    aciMemoryGB = todouble(properties.containers[0].properties.resources.requests.memoryInGB),
    topSku = tostring(sku.name)
| extend
    resolvedSku = case(
        type == 'microsoft.containerinstance/containergroups' and isnotnull(aciCpu) and isnotnull(aciMemoryGB),
            strcat("vCPU", tostring(aciCpu), "_Mem", tostring(aciMemoryGB)),
        isnotempty(topSku), topSku,
        'N/A'
    )
| project
    id, name, type, location, subscriptionId,
    resolvedPowerState = 'Running', resolvedSku, osType = 'N/A',
    resolvedRedundancy = 'N/A', resolvedHaReplicas = 0,
    resourceGroup, tags
""",

    # Azure-SSIS Integration Runtime - the only persistent, per-node-billed
    # Data Factory sub-resource. Excludes the default serverless
    # AutoResolveIntegrationRuntime every factory gets automatically.
    # resolvedPowerState left blank on purpose - overwritten by
    # _fetch_ssis_ir_states()'s separate getStatus() RPC call in Python.
    "datafactory_ssis": """
Resources
| where type == 'microsoft.datafactory/factories/integrationruntimes'
| where isnotempty(tostring(properties.typeProperties.ssisProperties)) and isnotempty(tostring(properties.typeProperties.computeProperties.nodeSize))
| extend
    ssisNodeSize    = tostring(properties.typeProperties.computeProperties.nodeSize),
    ssisNodeCount   = tostring(properties.typeProperties.computeProperties.numberOfNodes),
    ssisEdition     = tostring(properties.typeProperties.ssisProperties.edition),
    ssisLicenseType = tostring(properties.typeProperties.ssisProperties.licenseType)
| extend
    resolvedSku = case(
        isnotempty(ssisNodeSize) and isnotempty(ssisNodeCount) and isnotempty(ssisEdition) and isnotempty(ssisLicenseType),
            strcat(ssisNodeSize, "_", ssisNodeCount, "_", ssisEdition, "_", ssisLicenseType),
        'N/A'
    )
| project
    id, name, type, location, subscriptionId,
    resolvedPowerState = '', resolvedSku, osType = 'N/A',
    resolvedRedundancy = 'N/A', resolvedHaReplicas = 0,
    resourceGroup, tags
""",

}


def _strip_kql_comments(query: str) -> str:
    """Removes // line comments from a KQL query string before it's sent to
    Azure Resource Graph - real bug fixed 2026-08-30, caught live on a
    production deployment (a genuine, reproducible ParserFailure with real
    line/character/token detail, requested from the user via Resource Graph
    Explorer specifically to pin this down rather than guess blind).

    The error pointed directly at a normal `=` in a plain extend assignment
    (pgMysqlSkuName = tostring(sku.name)) sitting right after a comment
    block dense with double-quoted words ("Burstable"/"GeneralPurpose"/...),
    with a second error further down reporting <EOF> - the classic shape of
    a lexer losing track of string-literal boundaries partway through and
    never recovering. Counted every apostrophe across this query's own
    273 comment-only lines (this file's documentation style leans heavily
    on possessives - "Microsoft's own ARM template reference", "this app's
    own convention", etc. - accumulated one new resource type's comment
    block at a time across many editing rounds) and found an ODD total
    (99) - consistent with an unmatched quote character somewhere breaking
    Resource Graph's own parser, even though standard KQL comment handling
    should make this a non-issue. Rather than hunt down and rephrase
    apostrophes scattered across hundreds of comment lines (fragile - the
    next edit could just reintroduce the same accidental parity break),
    comments are stripped from the query text actually SENT to Azure here,
    while staying in the source file above for developers reading this
    code - they serve no purpose to Azure's API either way.

    Quote-aware (tracks single/double-quoted string state) rather than a
    naive per-line strip, so a real KQL string literal that happened to
    contain "//" would not be mistaken for a comment - not a concern for
    this app's current query (its only string literals are resource type
    paths like 'microsoft.compute/virtualmachines', which never contain
    "//"), but a correct general-purpose implementation rather than one
    that would silently corrupt a future query containing a URL or similar."""
    out = []
    in_string = None
    i = 0
    n = len(query)
    while i < n:
        c = query[i]
        if in_string:
            out.append(c)
            if c == in_string:
                in_string = None
            i += 1
            continue
        if c in ("'", '"'):
            in_string = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and query[i + 1] == "/":
            while i < n and query[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    # Collapse the blank lines left behind by removed comment-only lines -
    # not required for correctness, just keeps the query compact if it's
    # ever logged/inspected for debugging.
    return "\n".join(line for line in "".join(out).split("\n") if line.strip())


def _fetch_ssis_ir_states(credential, subscription_id: str, ir_rows: list) -> dict:
    """Azure-SSIS Integration Runtime's Started/Stopped state isn't a stored
    ARM property Resource Graph can see at all (confirmed via Microsoft's
    own REST API reference, 2026-08) - it only comes back from a separate
    per-resource getStatus() RPC call. Requires the "FinOps SSIS IR Status
    Reader" custom role (see OPTIONAL_SUBSCRIPTION_ROLES above) - no
    built-in role covers this at Reader scope. Fails open, per row: any
    error (missing role, transient failure, resource_group parsing miss)
    just leaves that one IR out of the returned dict, so its state falls
    back to _map_power_state()'s "Running" default in the caller rather
    than breaking the whole sync - this is a narrow, optional enhancement,
    not a required capability, matching the file's other HAS_* guards.
    Returns {resource_id: raw_state_string} (e.g. "Started"/"Stopped")."""
    states = {}
    if not HAS_DATAFACTORY or not ir_rows:
        return states
    try:
        client = DataFactoryManagementClient(credential, subscription_id)
    except Exception:
        return states
    for r in ir_rows:
        resource_id = r.get("id", "")
        resource_group = r.get("resourceGroup", "")
        if not resource_id or not resource_group:
            continue
        # ARM ID: .../resourceGroups/{rg}/providers/Microsoft.DataFactory/
        # factories/{factory}/integrationRuntimes/{ir} - case-insensitive
        # segment search since Resource Graph's `id` preserves the
        # provider's real casing, not the lowercased `type` field this file
        # matches elsewhere.
        parts = resource_id.split("/")
        lower_parts = [p.lower() for p in parts]
        try:
            factory_name = parts[lower_parts.index("factories") + 1]
            ir_name = parts[lower_parts.index("integrationruntimes") + 1]
        except (ValueError, IndexError):
            continue
        try:
            status = client.integration_runtimes.get_status(resource_group, factory_name, ir_name)
            states[resource_id] = status.properties.state
        except Exception:
            continue
    return states


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
    # One call per group, not one call for everything (2026-08-30 - see
    # RESOURCE_GRAPH_QUERIES' own comment for the full reasoning). Each
    # group is wrapped in its own try/except so a failure in ONE group
    # (a real KQL issue, a transient API error, anything) can't block the
    # other 8 - the whole point of splitting this up. Well within Resource
    # Graph's documented throttling allowance (~15 queries/5s per user -
    # see Microsoft's own troubleshooting docs) even with zero deliberate
    # delay between calls, since each call's own network round-trip
    # already spaces them out.
    rows = []
    group_errors = {}
    for group_name, group_query in RESOURCE_GRAPH_QUERIES.items():
        try:
            request = QueryRequest(
                subscriptions=[creds.subscription_id],
                query=_strip_kql_comments(group_query),
            )
            result = client.resources(request)
            rows.extend(result.data if result.data else [])
        except Exception as e:
            group_errors[group_name] = str(e)[:300]
    # Not currently surfaced to the caller (fetch_live_inventory's own
    # signature only returns the DataFrame) - logged here as a real,
    # disclosed limitation rather than silently swallowed. A future pass
    # could thread this through to the sync pipeline's own message the
    # same way ri_sp_error/enrichment_error already are, if per-group
    # visibility turns out to matter in practice.
    if group_errors:
        import logging
        logging.getLogger(__name__).warning(f"Resource Graph group(s) failed: {group_errors}")

    ssis_rows = [r for r in rows if r.get("type", "").lower() == "microsoft.datafactory/factories/integrationruntimes"]
    ssis_states = _fetch_ssis_ir_states(credential, creds.subscription_id, ssis_rows)

    records = []
    for r in rows:
        rtype = r.get("type", "").lower()
        raw_power_state = r.get("resolvedPowerState", "")
        if rtype == "microsoft.datafactory/factories/integrationruntimes":
            raw_power_state = ssis_states.get(r.get("id", ""), raw_power_state)
        records.append({
            "Resource ID":             r.get("id", ""),
            "Resource Name":           r.get("name", ""),
            "Resource Type":           _map_resource_type(rtype),
            "Resource State":          _map_power_state(raw_power_state),
            "Region":                  r.get("location", ""),
            "OS":                      r.get("osType", "N/A") or "N/A",
            "SKU":                     r.get("resolvedSku", "N/A") or "N/A",
            "Redundancy":              r.get("resolvedRedundancy", "N/A") or "N/A",
            "HA Replicas":             r.get("resolvedHaReplicas", 0) or 0,
            "PAYG Hourly Cost USD":    0.0,   # populated by pricing module
            "Avg Daily Running Hours": 24,    # default; update via Activity Log
            "Subscription":            r.get("subscriptionId", ""),
            # Real Resource Graph 'resourceGroup' (bare name) - added
            # 2026-08-23 so a Reservation/Savings Plan purchased with
            # "Single resource group" scope can be matched against exactly
            # the resources it covers (see pricing/commitment_mapping.py).
            # Was already projected by the query but never read into a row
            # before now.
            "Resource Group":         r.get("resourceGroup", "") or "",
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
        "microsoft.datafactory/factories/integrationruntimes": "Azure-SSIS Integration Runtime",
        "microsoft.compute/disks":                       "Azure Disk Storage",
        "microsoft.documentdb/mongoclusters":            "Azure DocumentDB",
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
            # Real AppliedScopeProperties field (confirmed against the
            # installed SDK model, 2026-08-23) - only set when this
            # reservation was purchased with "Single resource group" scope
            # (a real, distinct scoping option beyond subscription-level
            # Single scope - see pricing/commitment_mapping.py).
            "applied_scope_resource_group_id": scope_props.resource_group_id if scope_props else None,
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


def fetch_vm_flexibility_groups(creds: AzureCredentials, location: str) -> dict:
    """Real VM Reserved Instance instance-size-flexibility group + ratio
    per SKU, for one Azure region, via the Reservations Catalog API
    (GET /subscriptions/{subscriptionId}/providers/Microsoft.Capacity/
    catalogs?api-version=2022-11-01&reservedResourceType=VirtualMachines
    &location={location} - real endpoint confirmed against Microsoft's
    own REST API reference, not guessed). Returns
    {sku_name: (flexibility_group, ratio)} for every SKU where both
    properties were present - a SKU missing either is silently omitted
    (not guessed), same "can't determine, don't fabricate" discipline as
    every other fetch in this file.

    ReservationsMgmtClient.get_catalog() is the SAME client class already
    used by fetch_live_reservations() above - confirmed against the
    installed SDK (azure-mgmt-reservations 3.0.0) that get_catalog is a
    real top-level client method (not a sub-resource), taking
    subscription_id positionally plus reserved_resource_type/location
    keywords, matching the REST reference's own Python sample exactly.
    subscription_id is only needed to authenticate the call (same "needs
    *some* subscription_id even though the query itself isn't
    subscription-specific" pattern already documented for
    AuthorizationManagementClient/BillingBenefitsMgmtClient above) -
    creds.subscription_id is reused, not a new concept.

    No new IAM permission needed: Reservations Reader (already required
    for fetch_live_reservations, see REQUIRED_TENANT_ROLES) grants
    "Microsoft.Capacity/*/read" - a wildcard confirmed (via a third-party
    RBAC role catalog cross-referencing the real built-in role GUID
    582fc458-8989-419f-a480-75249bc5db7e, not Microsoft's own live
    portal - this app has no way to query real RBAC role *definitions*
    live, only role *assignments*) to cover
    "Microsoft.Capacity/catalogs/read" specifically.

    Not independently live-tested (no real Azure credentials available in
    this environment) - Microsoft's own docs disagree on the exact
    skuProperties names to expect (their ISF migration guide names
    "ReservationsAutofitGroup"/"ReservationsAutofitRatio"; their own REST
    API reference's worked example response, same api-version, doesn't
    show either property on its sample SKUs) - defensively checks for
    both names and simply omits any SKU where they're absent, rather than
    guessing. Verify against one real tenant's actual response once this
    ships, same disclosure already used for this app's other
    can't-verify-without-a-real-tenant fetches.
    """
    if not HAS_AZURE_IDENTITY or not HAS_RESERVATIONS:
        raise ImportError(
            "Install required packages: pip install azure-identity azure-mgmt-reservations"
        )

    credential = ClientSecretCredential(
        tenant_id=creds.tenant_id, client_id=creds.client_id, client_secret=creds.client_secret,
    )
    client = ReservationsMgmtClient(credential)

    groups: dict = {}
    for item in client.get_catalog(
        creds.subscription_id, reserved_resource_type="VirtualMachines", location=location,
    ):
        group_name, ratio = None, None
        for prop in (item.sku_properties or []):
            if prop.name == "ReservationsAutofitGroup":
                group_name = prop.value
            elif prop.name == "ReservationsAutofitRatio":
                try:
                    ratio = float(prop.value)
                except (TypeError, ValueError):
                    ratio = None
        if item.name and group_name and ratio is not None:
            groups[item.name] = (group_name, ratio)
    return groups


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
            # Real, flattened SavingsPlanModel fields (confirmed against the
            # installed azure-mgmt-billingbenefits SDK, 2026-08-23) - were
            # previously not captured at all. Same "Single subscription" /
            # "Single resource group" scoping Reservations support - see
            # pricing/commitment_mapping.py.
            "applied_scope_subscription_id":   s.applied_scope_properties.subscription_id if s.applied_scope_properties else None,
            "applied_scope_resource_group_id": s.applied_scope_properties.resource_group_id if s.applied_scope_properties else None,
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
