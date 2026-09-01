# azure_deploy/deploy_all_resources.ps1
# Multi-Cloud FinOps Optimization System  Azure Production Deployment Script
#
# TARGETED FOR: Windows PowerShell 5.1, PowerShell 7, Azure Cloud Shell
# FEATURES:
#    Full Architecture: Azure SQL Server + Function App + Streamlit App Service
#    PowerShell 5.1 Compatible: Uses dual-argument Join-Path calls and ASCII output.
#    ARM Resilient: Pauses briefly after app creation to ensure ARM Control Plane propagation.
#    Smart Quota Fallback: Automatically tests candidate regions if student subscription has 0 quota in the primary region.
#    App Service Plan: F1 (Free tier, $0/month) - switched from B1 2026-08 to fit a limited student
#    subscription budget. 60 CPU-min/day cap, no "Always On" (idles out after ~20 min, cold-starts on
#    next request) - fine for intermittent demo/test use, not sustained traffic. See the inline note
#    at the plan-creation step for the one open risk (Oryx remote build behavior not yet verified on F1).
#    Passwordless DB auth: both apps connect to Azure SQL via System-Assigned Managed Identity
#    (mssql-python driver) - no password/connection-string secret is ever stored anywhere. Step 7
#    grants both identities DB access automatically (falls back to printing a one-time manual
#    T-SQL grant for the Portal Query editor if the local venv isn't available).

# SqlAdminPassword has no default on purpose - it used to be a hardcoded
# real password here (found and fixed 2026-08, alongside the same
# credential duplicated in db/schema.py and azure_deploy/seed_azure_sql.py).
# Typed as [SecureString] (not [string]) specifically so PowerShell prompts
# for it itself, masked, whenever it's omitted - a Mandatory SecureString
# parameter gets this behavior built in, no separate Read-Host dance needed:
#   .\deploy_all_resources.ps1
# (PowerShell will then prompt "SqlAdminPassword: " with masked input). Still
# scriptable non-interactively if needed, e.g. from CI:
#   .\deploy_all_resources.ps1 -SqlAdminPassword (ConvertTo-SecureString $env:SQL_ADMIN_PW -AsPlainText -Force)
#
# NOTE 2026-08: SqlAdminUser/SqlAdminPassword are ONLY used to create the SQL
# Server's own break-glass admin login (Azure SQL requires some admin
# credential at server-creation time) - the app itself no longer uses them
# at all. Both the Web App and Function App now connect via their own
# System-Assigned Managed Identity (passwordless, no secret anywhere) - see
# db/schema.py's get_engine() and the "Managed Identity setup" step below.
# NAMING CONVENTION (2026-08-30 redesign - real feedback: the old scheme
# suffixed almost every resource with the first 8 hex characters of the
# SUBSCRIPTION ID (e.g. "finops-app-e0b96fd6") - unreadable, and not
# something you ever see or choose. That suffix existed for a real reason
# (Storage Accounts, Web Apps, Function Apps, and SQL Servers all need
# GLOBALLY unique names across ALL of Azure, since they get public DNS
# names like *.azurewebsites.net - a plain "finops-app" would very likely
# already be taken by someone else worldwide), but the source of the
# uniqueness doesn't need to be an opaque hash - $OwnerHandle below is an
# explicit, human-chosen replacement for it.
#
# Every resource now follows Microsoft's own Cloud Adoption Framework
# convention (<resource-type-abbreviation>-<app-name>-<owner-handle>,
# lowercase alphanumeric-only with no hyphens for Storage Accounts, which
# don't allow them):
#   rg-finops-<OwnerHandle>      Resource Group      (not globally unique - no suffix strictly required, kept for consistency with everything else)
#   stfinops<OwnerHandle>        Storage Account      (globally unique - REQUIRES this)
#   sql-finops-<OwnerHandle>     SQL Server           (globally unique - REQUIRES this)
#   finops-db                    SQL Database         (scoped under the server, not globally unique - unchanged, was already clean)
#   func-finops-<OwnerHandle>    Function App         (globally unique - REQUIRES this)
#   app-finops-<OwnerHandle>     Web App              (globally unique - REQUIRES this)
#   plan-finops-<OwnerHandle>    App Service Plan     (not globally unique - no suffix strictly required, kept for consistency)
param (
    [string]$ResourceGroupName = "",
    [string]$Location          = "westus3",
    [string]$SqlAdminUser      = "finopsadmin",
    [Parameter(Mandatory = $true)]
    [SecureString]$SqlAdminPassword,
    [string]$AppNamePrefix     = "finops",
    # Your own short, memorable handle - the readable replacement for the
    # old subscription-ID hash. Lowercase alphanumeric only (Storage
    # Account naming's strictest constraint applies to the whole scheme,
    # so every resource stays consistent) - sanitized below regardless of
    # what's passed in, so stray punctuation/casing can't silently produce
    # an invalid Storage Account name.
    [string]$OwnerHandle       = "ascloudx"
)

# Converted once, right here, to the plain string az CLI actually needs -
# the SecureString above exists only to get PowerShell's built-in masked
# prompt; nothing downstream should reference $SqlAdminPassword directly.
$SqlAdminPasswordPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SqlAdminPassword)
)

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "  [ERROR] FAILED: $msg" -ForegroundColor Red
    Write-Host "  Fix the issue above and re-run the script." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# Checked up front, before touching any Azure resource - a missing tool
# discovered mid-script (e.g. at the Function App publish step) leaves
# whatever got created so far in a half-finished state, and PowerShell's own
# "command not found" error for a genuinely-missing executable is a raw,
# unfriendly `CommandNotFoundException`, not a clean message. This matters
# for running this script on a DIFFERENT machine than the one it was
# developed on (a mentor's/examiner's laptop, a fresh clone) - `az` and
# `python` were already implicitly required, `func` (Azure Functions Core
# Tools) too, but none of the three were ever verified present before this.
Write-Host "  Checking prerequisites ..." -ForegroundColor Yellow
$missingTools = @()
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { $missingTools += "az (Azure CLI - https://learn.microsoft.com/cli/azure/install-azure-cli)" }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { $missingTools += "python (Python 3 - https://www.python.org/downloads/)" }
if (-not (Get-Command func -ErrorAction SilentlyContinue)) { $missingTools += "func (Azure Functions Core Tools - https://learn.microsoft.com/azure/azure-functions/functions-run-local)" }
if ($missingTools.Count -gt 0) {
    Write-Host ""
    Write-Host "  [ERROR] Missing required tool(s):" -ForegroundColor Red
    foreach ($tool in $missingTools) { Write-Host "    - $tool" -ForegroundColor Red }
    Write-Host ""
    Write-Host "  Install the above, then re-run this script. Nothing has been created yet." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
Write-Host "        [OK] az, python, and func are all available." -ForegroundColor Green

$subId = (az account show --query id -o tsv 2>&1).Trim()
if ($LASTEXITCODE -ne 0) { Fail "Cannot reach Azure CLI. Run 'az login' first." }

# Sanitized once, here, regardless of what was passed in - lowercased and
# stripped to alphanumeric-only, since that's Storage Account naming's
# strictest constraint and every resource name below shares this same
# token for consistency (a Storage Account name can't have hyphens at
# all, so a raw "-OwnerHandle 'A.Cloud-X'" would otherwise silently
# produce an invalid Storage Account name while looking fine everywhere
# else, until step 3 fails).
$suffix = ($OwnerHandle -replace '[^a-zA-Z0-9]', '').ToLower()
if (-not $suffix) { Fail "OwnerHandle resolved empty after removing non-alphanumeric characters - pick a short alphanumeric handle, e.g. 'ascloudx'." }

if (-not $ResourceGroupName) { $ResourceGroupName = "rg-$AppNamePrefix-$suffix" }

$StorageAccountName = "st$($AppNamePrefix)$suffix"
$SqlServerName      = "sql-$AppNamePrefix-$suffix"
$SqlDbName          = "finops-db"
$FunctionAppName    = "func-$AppNamePrefix-$suffix"
$WebAppName         = "app-$AppNamePrefix-$suffix"
$AppPlanName        = "plan-$AppNamePrefix-$suffix"
# Separate from $AppPlanName (that one hosts the Web App) - the Function
# App gets its own Consumption/Dynamic plan, auto-created and auto-named
# by `az functionapp create --consumption-plan-location` if left
# unnamed (e.g. "WestUS3LinuxDynamicPlan" - real gap the user caught live
# after a fresh deploy). See this script's own step 5 comment for why
# giving it a real name needs a different, lower-level command than every
# other resource here.
$FunctionPlanName   = "plan-func-$AppNamePrefix-$suffix"

# Storage Account names have the strictest real limit of any resource
# named here - 3-24 characters, enforced by Azure itself (a name over 24
# chars is rejected outright at creation, not truncated) - checked
# up front with the defaults' own math shown in the message, rather than
# letting a longer -AppNamePrefix/-OwnerHandle combination fail confusingly
# deep into step 3.
if ($StorageAccountName.Length -gt 24) {
    Fail "Storage Account name '$StorageAccountName' is $($StorageAccountName.Length) characters - Azure's limit is 24. Shorten -AppNamePrefix and/or -OwnerHandle (the default 'st' + 'finops' + 'ascloudx' is 16)."
}

Write-Host ""
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host "  FINOPS AZURE PRODUCTION DEPLOYMENT" -ForegroundColor Cyan
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host "   Subscription  : $subId" -ForegroundColor DarkCyan
Write-Host "   Resource Group: $ResourceGroupName" -ForegroundColor DarkCyan
Write-Host "   Target Region : $Location" -ForegroundColor DarkCyan
Write-Host "   App Name      : $WebAppName" -ForegroundColor DarkCyan
Write-Host "   SQL Server    : $SqlServerName" -ForegroundColor DarkCyan
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Resource Group
Write-Host "  [1/7] Resource Group ..." -ForegroundColor Yellow
az group create --name $ResourceGroupName --location $Location -o none
if ($LASTEXITCODE -ne 0) { Fail "Creating resource group" }
Write-Host "        [OK] Done." -ForegroundColor Green

# 2. Resource Providers
Write-Host "  [2/7] Registering resource providers ..." -ForegroundColor Yellow
az provider register --namespace Microsoft.Sql --wait -o none
az provider register --namespace Microsoft.Web --wait -o none
Write-Host "        [OK] Done." -ForegroundColor Green

# 3. Storage Account
Write-Host "  [3/7] Storage Account: $StorageAccountName ..." -ForegroundColor Yellow
$stExists = az storage account show --name $StorageAccountName --resource-group $ResourceGroupName --query name -o tsv 2>$null
if (-not $stExists) {
    az storage account create --name $StorageAccountName --location $Location --resource-group $ResourceGroupName --sku Standard_LRS -o none
    if ($LASTEXITCODE -ne 0) { Fail "Creating storage account '$StorageAccountName'" }
    Write-Host "        [OK] Created." -ForegroundColor Green
} else {
    Write-Host "        [OK] Already exists - skipped." -ForegroundColor DarkGreen
}

# 4. Azure SQL Server and Serverless Database
Write-Host "  [4/7] Azure SQL Server and Serverless Database ..." -ForegroundColor Yellow
$sqlExists = az sql server show --name $SqlServerName --resource-group $ResourceGroupName --query name -o tsv 2>$null
if (-not $sqlExists) {
    Write-Host "        Creating SQL Server '$SqlServerName'..." -ForegroundColor Yellow
    $sqlOut = az sql server create --name $SqlServerName --resource-group $ResourceGroupName --location $Location --admin-user $SqlAdminUser --admin-password $SqlAdminPasswordPlain
    if ($LASTEXITCODE -ne 0) {
        Write-Host "        [WARNING] SQL Server creation failed in '$Location'. Error: $sqlOut" -ForegroundColor Red
        Fail "SQL Server creation failed. Check subscription SQL quotas or location."
    }

    az sql server firewall-rule create --resource-group $ResourceGroupName --server $SqlServerName --name AllowAzureServices --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 -o none
}

$dbExists = az sql db show --name $SqlDbName --server $SqlServerName --resource-group $ResourceGroupName --query name -o tsv 2>$null
if (-not $dbExists) {
    # Try the free-tier offer first (2026-08, added to fit a limited student
    # subscription budget): 100,000 vCore-seconds + 32 GB data + 32 GB backup
    # storage free per month, for the lifetime of the subscription - see
    # https://learn.microsoft.com/en-us/azure/azure-sql/database/free-offer .
    # --free-limit-exhaustion-behavior AutoPause means it pauses (not bills
    # overage) if the monthly free allowance is ever exceeded - the safer
    # choice given the goal here is avoiding ANY accidental spend, not
    # maximizing uptime. NOT guaranteed to work on every subscription type -
    # Microsoft's own docs explicitly say "the Microsoft Azure for Students
    # Starter offer is incompatible with this Azure SQL Database free offer"
    # (the separate, credit-card-free student program - different from
    # "Azure for Students" or a general Azure Free account, both of which
    # ARE compatible). If this subscription is Students Starter, the
    # --use-free-limit attempt below will fail and the fallback plain
    # Serverless create (still auto-pausing, just not literally free) runs
    # instead - deliberately NOT retrying the same free-limit flags a second
    # time, so an incompatible subscription degrades gracefully rather than
    # hard-failing the whole deployment.
    Write-Host "        Creating Serverless Database '$SqlDbName' (trying free-tier offer)..." -ForegroundColor Yellow
    az sql db create --resource-group $ResourceGroupName --server $SqlServerName --name $SqlDbName --edition GeneralPurpose --family Gen5 --compute-model Serverless --capacity 1 --auto-pause-delay 60 --use-free-limit --free-limit-exhaustion-behavior AutoPause -o none 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "        [INFO] Free-tier offer unavailable on this subscription - falling back to standard Serverless billing (still auto-pauses)." -ForegroundColor DarkYellow
        az sql db create --resource-group $ResourceGroupName --server $SqlServerName --name $SqlDbName --edition GeneralPurpose --family Gen5 --compute-model Serverless --capacity 1 --auto-pause-delay 60 -o none 2>$null
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "        Re-creating SQL Server and Database..." -ForegroundColor Yellow
        az sql server create --name $SqlServerName --resource-group $ResourceGroupName --location $Location --admin-user $SqlAdminUser --admin-password $SqlAdminPasswordPlain -o none
        az sql server firewall-rule create --resource-group $ResourceGroupName --server $SqlServerName --name AllowAzureServices --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 -o none
        az sql db create --resource-group $ResourceGroupName --server $SqlServerName --name $SqlDbName --edition GeneralPurpose --family Gen5 --compute-model Serverless --capacity 1 --auto-pause-delay 60 -o none
        if ($LASTEXITCODE -ne 0) { Fail "Creating SQL database '$SqlDbName'" }
    }
    Write-Host "        [OK] SQL Server and Database Created." -ForegroundColor Green
} else {
    Write-Host "        [OK] SQL Server and Database already exist - skipped." -ForegroundColor DarkGreen
}

$SqlServerFqdn = "$SqlServerName.database.windows.net"

# 5. Azure Function App
Write-Host "  [5/7] Azure Function App: $FunctionAppName ..." -ForegroundColor Yellow
$funcExists = az functionapp show --name $FunctionAppName --resource-group $ResourceGroupName --query name -o tsv 2>$null
if (-not $funcExists) {
    # Consumption/Dynamic (Y1) hosting plan created EXPLICITLY and named,
    # 2026-08-30 - real gap the user caught live after a fresh deploy:
    # passing --consumption-plan-location (below) to `az functionapp
    # create` without an explicit --plan makes Azure auto-provision an
    # UNNAMED plan with its own generated name (e.g.
    # "WestUS3LinuxDynamicPlan") - not something this script or the user
    # ever chose. Naming it ourselves turns out to need a genuinely
    # different, lower-level command than every other resource in this
    # script: confirmed via Azure CLI's own GitHub issue tracker
    # (Azure/azure-cli#11195, #19864) that `az functionapp plan create`/
    # `az appservice plan create` do NOT support the Y1/Dynamic Consumption
    # tier at all - only paid tiers (B1, S1, EP1, ...). The documented
    # workaround is a raw ARM resource create against
    # Microsoft.Web/serverfarms with an explicit sku.name="Y1"/
    # sku.tier="Dynamic" JSON body - that's what this is. Getting this
    # SKU/tier wrong would silently switch to a BILLED plan on a
    # budget-conscious student subscription, so this is deliberately
    # exact, not approximate - verify after deploying with
    # `az functionapp plan show --name $FunctionPlanName --resource-group
    # $ResourceGroupName --query sku` and confirm it still reports
    # "Dynamic"/"Y1" if you ever touch this block.
    $funcPlanExists = az functionapp plan show --name $FunctionPlanName --resource-group $ResourceGroupName --query name -o tsv 2>$null
    if ($funcPlanExists) {
        # Real error hit 2026-09-01, one run after the @file JSON-quoting
        # fix below: the plan WAS created, but the JSON body only set
        # location/sku - with no kind/reserved, ARM defaults a serverfarm
        # to WINDOWS, and Python Functions only run on Linux ("Runtime
        # python not supported for os windows" - the exact live error).
        # Detects and self-heals a plan left over from that: `reserved`
        # is ARM's real Linux/Windows flag on Microsoft.Web/serverfarms
        # (confirmed via Microsoft's own ARM/Bicep samples for a Linux
        # Consumption Function plan) - "true" means Linux. A plan that's
        # already correctly Linux is left alone (idempotent, no-op).
        $funcPlanIsLinux = (az functionapp plan show --name $FunctionPlanName --resource-group $ResourceGroupName --query reserved -o tsv 2>$null) -eq "true"
        if (-not $funcPlanIsLinux) {
            Write-Host "        Existing plan '$FunctionPlanName' is Windows-mode (Python needs Linux) - deleting so it can be recreated correctly..." -ForegroundColor Yellow
            az functionapp plan delete --name $FunctionPlanName --resource-group $ResourceGroupName --yes -o none
            $funcPlanExists = $null
        }
    }
    if (-not $funcPlanExists) {
        Write-Host "        Creating named Consumption plan '$FunctionPlanName'..." -ForegroundColor Yellow
        # Passed via a temp @file, not inline on the command line - real
        # error hit 2026-09-01: `az` on Windows is az.cmd, a batch wrapper
        # that re-parses the command line through cmd.exe before Python
        # ever sees it, and that re-parse strips embedded double quotes
        # from an inline JSON string PowerShell passes to a native/batch
        # exe (confirmed live: the JSON arrived as
        # {location:westus3,sku:{name:Y1,tier:Dynamic}} - every `"` gone).
        # `az`'s own `--properties @<file>` form reads the JSON straight
        # off disk instead, sidestepping that re-parse entirely - the
        # standard, documented fix for this exact class of Windows-only
        # az CLI quoting bug. [System.IO.File]::WriteAllText (not
        # Set-Content -Encoding utf8) writes UTF-8 with NO byte-order-mark
        # in both Windows PowerShell 5.1 and 7+ - Set-Content's BOM-less
        # "utf8NoBOM" encoding name only exists from PS 6 on, and a BOM at
        # the front of this file could itself trip up az's JSON parser.
        # kind="linux" + properties.reserved=true is what actually makes
        # this a LINUX Consumption plan (see the self-heal comment above -
        # omitting these is exactly what produced the Windows-default
        # plan the first time).
        $planJsonPath = Join-Path $env:TEMP "finops-func-plan-$suffix.json"
        $planJson = @{
            location   = $Location
            kind       = "linux"
            sku        = @{ name = "Y1"; tier = "Dynamic" }
            properties = @{ reserved = $true }
        } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($planJsonPath, $planJson)
        az resource create --resource-group $ResourceGroupName --name $FunctionPlanName --resource-type "Microsoft.Web/serverfarms" --is-full-object --properties "@$planJsonPath" -o none
        $funcPlanCreateExit = $LASTEXITCODE
        Remove-Item -Path $planJsonPath -Force -ErrorAction SilentlyContinue
        if ($funcPlanCreateExit -ne 0) { Fail "Creating named Consumption plan '$FunctionPlanName'" }
    }

    # --disable-app-insights added 2026-08-30, real feedback: without it,
    # `az functionapp create` silently provisions an Application Insights
    # resource (and, per current Azure platform behavior, a backing Log
    # Analytics Workspace auto-named something like "DefaultWorkspace-..."
    # or "Default...") that this app has zero code reference to anywhere -
    # confirmed via a full repo grep for "Application Insights"/"Log
    # Analytics"/"APPINSIGHTS" before removing it, not assumed. Pure
    # unused monitoring infrastructure this deployment never asked for.
    #
    # Real error hit 2026-09-01: `az functionapp create --plan
    # $FunctionPlanName` (pointing at the named plan created above) fails
    # with "AlwaysOn cannot be set for this site as the plan does not
    # allow it" - a long-standing, documented Azure CLI bug (Azure/
    # azure-cli#8388, #12271): the --plan code path always tries to set
    # AlwaysOn regardless of what plan it's given, and Consumption/Dynamic
    # plans reject AlwaysOn outright. Only --consumption-plan-location
    # correctly skips it - but that auto-names the plan, defeating the
    # whole point of creating a named one above. Real, documented
    # workaround (same GitHub issues): create via
    # --consumption-plan-location (known-good path, gets an auto-named
    # throwaway Consumption plan), then move the Function App onto the
    # ALREADY-CREATED named plan via `az functionapp update --plan`
    # (confirmed a real, supported parameter via Microsoft's own CLI
    # reference) - moving a Consumption-tier Function App between two
    # Consumption plans in the same region is a supported, low-risk
    # operation (Consumption plans are a scaling/billing construct, not a
    # dedicated host). The now-empty throwaway plan is deleted afterward
    # so it doesn't linger as clutter.
    az functionapp create --resource-group $ResourceGroupName --consumption-plan-location $Location --runtime python --runtime-version 3.12 --functions-version 4 --name $FunctionAppName --storage-account $StorageAccountName --os-type Linux --disable-app-insights true -o none
    if ($LASTEXITCODE -ne 0) { Fail "Creating Function App '$FunctionAppName'" }

    $tempPlanId = az functionapp show --resource-group $ResourceGroupName --name $FunctionAppName --query "appServicePlanId" -o tsv
    $tempPlanName = ($tempPlanId -split '/')[-1]
    if ($tempPlanName -and $tempPlanName -ne $FunctionPlanName) {
        Write-Host "        Moving Function App onto the named plan '$FunctionPlanName'..." -ForegroundColor Yellow
        az functionapp update --resource-group $ResourceGroupName --name $FunctionAppName --plan $FunctionPlanName -o none
        if ($LASTEXITCODE -ne 0) { Fail "Moving Function App '$FunctionAppName' onto '$FunctionPlanName'" }
        Write-Host "        Deleting the auto-named throwaway plan '$tempPlanName'..." -ForegroundColor Yellow
        az functionapp plan delete --resource-group $ResourceGroupName --name $tempPlanName --yes -o none
    }

    Start-Sleep -Seconds 5
    Write-Host "        [OK] Provisioned." -ForegroundColor Green
} else {
    Write-Host "        [OK] Already exists - skipped." -ForegroundColor DarkGreen
}

# Managed Identity (passwordless) - no DATABASE_URL / secret app setting at
# all. AZURE_SQL_SERVER/AZURE_SQL_DATABASE are plain identifiers, not
# credentials - db/schema.py's get_engine() uses them + this identity to
# authenticate to Azure SQL via Microsoft Entra, no password anywhere. The
# identity still needs to be GRANTED database access - see step 7 below,
# which runs after both apps' identities exist.
Write-Host "        Enabling Managed Identity ..." -ForegroundColor Yellow
az functionapp identity assign --resource-group $ResourceGroupName --name $FunctionAppName -o none
az functionapp config appsettings set --resource-group $ResourceGroupName --name $FunctionAppName --settings AZURE_SQL_SERVER="$SqlServerFqdn" AZURE_SQL_DATABASE="$SqlDbName" -o none
# Explicitly remove any leftover DATABASE_URL from a prior password-based
# deploy - db/schema.py's get_engine() checks DATABASE_URL FIRST, so a
# stale setting here would silently keep using the old password path
# instead of the new Managed Identity one. Safe no-op if it was never set.
az functionapp config appsettings delete --resource-group $ResourceGroupName --name $FunctionAppName --setting-names DATABASE_URL -o none 2>$null

# Shared encryption key for CloudTenant.client_secret at rest (db/crypto.py).
# Without this app setting, crypto.py falls back to a key auto-generated and
# cached in a local file - fine for local dev, but this Free-tier plan's
# Oryx build produces a brand-new container on every code deploy, so that
# local file (never committed, never persisted) is gone on the next push.
# Every redeploy silently rotated the key and made every already-stored
# client secret undecryptable garbage (real incident, 2026-08 - showed up as
# "Invalid client secret" on a secret that was actually fine). Resolved ONCE
# here and reused on every re-run of this script (read back from whichever
# app already has it set, so re-running never rotates a working key out from
# under already-saved tenants) - and must be IDENTICAL on both apps, since
# the Function App's cron sync decrypts what the Web App encrypted.
Write-Host "        Configuring shared encryption key (TENANT_SECRET_KEY) ..." -ForegroundColor Yellow
$sharedSecretKey = az functionapp config appsettings list --resource-group $ResourceGroupName --name $FunctionAppName --query "[?name=='TENANT_SECRET_KEY'].value" -o tsv 2>$null
if (-not $sharedSecretKey) {
    # Generated with pure .NET crypto, NOT `python -c "from cryptography..."`
    # - the earlier version shelled out to whatever `python` resolves to on
    # PATH, which silently has no guarantee of being this project's venv
    # (real incident, 2026-08-19: system python was 3.14 with no
    # `cryptography` installed, the command threw ModuleNotFoundError on
    # stderr, and the script's missing exit-code check let it print "[OK]"
    # and set TENANT_SECRET_KEY to an EMPTY string on both apps anyway -
    # silently reintroducing the exact "every redeploy breaks stored client
    # secrets" bug this block exists to prevent). A Fernet key is just 32
    # random bytes, base64-urlsafe-encoded - identical to what
    # cryptography.fernet.Fernet.generate_key() produces, no Python needed.
    # RandomNumberGenerator.Create()+GetBytes() (not the newer static
    # ::Fill(), which is .NET 6+ only and doesn't exist on Windows
    # PowerShell 5.1's .NET Framework runtime - confirmed by testing both
    # against the actual target PS version this script declares support for).
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $keyBytes = New-Object byte[] 32
    $rng.GetBytes($keyBytes)
    $rng.Dispose()
    $sharedSecretKey = [Convert]::ToBase64String($keyBytes).Replace('+', '-').Replace('/', '_')
    Write-Host "        [OK] Generated a new TENANT_SECRET_KEY." -ForegroundColor Green
} else {
    Write-Host "        [OK] Reusing existing TENANT_SECRET_KEY - already-saved client secrets stay valid." -ForegroundColor DarkGreen
}
if (-not $sharedSecretKey) { Fail "TENANT_SECRET_KEY resolved empty - refusing to deploy with a blank encryption key." }
az functionapp config appsettings set --resource-group $ResourceGroupName --name $FunctionAppName --settings TENANT_SECRET_KEY="$sharedSecretKey" -o none
# Unlike every other az call in this script, this one had no exit-code
# check - confirmed as a real gap 2026-08-23: a transient DNS failure on
# this exact call let the script print "[OK] Generated a new
# TENANT_SECRET_KEY." (true - the LOCAL .NET generation above did succeed)
# and continue straight past a Function App that never actually got the
# key set. Since the Web App's matching TENANT_SECRET_KEY gets set later
# using the same $sharedSecretKey variable in this same run, that mismatch
# would silently break the cron sync's decryption of tenant secrets the
# Web App encrypted - exactly the kind of "looked fine, wasn't" failure
# the Fail() pattern exists to catch everywhere else in this script.
if ($LASTEXITCODE -ne 0) { Fail "Setting TENANT_SECRET_KEY on Function App '$FunctionAppName'" }

# Fix PowerShell 5.1 Join-Path syntax: use nested 2-argument Join-Path calls
$parentPath   = Join-Path $PSScriptRoot ".."
$funcCodePath = [System.IO.Path]::GetFullPath((Join-Path $parentPath "azure_function"))

if (Test-Path $funcCodePath) {
    # function_app.py imports db/, data/, azure_conn/, pricing/, aws/ from the
    # repo root (its sync_pipeline call chain) - publishing azure_function/
    # alone (the old behavior here) never shipped them, so the Python worker
    # failed to import the function at cold start and Azure indexed ZERO
    # functions from it, silently, even on a "successful" publish (confirmed
    # 2026-08-16: az functionapp function list returned empty against a
    # deployment that reported no error). Stage a temp copy with those
    # sibling packages alongside azure_function/'s own files before
    # publishing - same fix applied to .github/workflows/deploy.yml's
    # deploy-function job.
    Write-Host "        Staging function app with its dependencies ..." -ForegroundColor Yellow
    $funcStagePath = Join-Path ([System.IO.Path]::GetTempPath()) "finops_func_stage"
    if (Test-Path $funcStagePath) { Remove-Item $funcStagePath -Recurse -Force }
    New-Item -ItemType Directory -Path $funcStagePath -Force | Out-Null
    Copy-Item (Join-Path $funcCodePath "host.json") $funcStagePath
    Copy-Item (Join-Path $funcCodePath "function_app.py") $funcStagePath
    Copy-Item (Join-Path $funcCodePath "requirements.txt") $funcStagePath
    foreach ($dep in @("db", "data", "azure_conn", "pricing", "aws")) {
        Copy-Item (Join-Path $parentPath $dep) (Join-Path $funcStagePath $dep) -Recurse
    }

    Write-Host "        Publishing cron function code ..." -ForegroundColor Yellow
    Push-Location $funcStagePath
    func azure functionapp publish $FunctionAppName --python
    Pop-Location
    if ($LASTEXITCODE -ne 0) {
        Write-Host "        [WARNING] Function code publish failed (non-fatal)." -ForegroundColor DarkYellow
    } else {
        Write-Host "        [OK] Function code deployed." -ForegroundColor Green
    }
} else {
    Write-Host "        [WARNING] azure_function/ not found - skipping code publish." -ForegroundColor DarkYellow
}

# 6. App Service Plan + Web App (Smart Regional Quota Fallback)
Write-Host "  [6/7] App Service Plan + Web App: $WebAppName ..." -ForegroundColor Yellow
$planExists = az appservice plan show --name $AppPlanName --resource-group $ResourceGroupName --query name -o tsv 2>$null

if (-not $planExists) {
    $candidateRegions = @($Location, "eastus2", "centralus", "eastus", "westus2")
    $planCreated = $false

    foreach ($region in $candidateRegions) {
        Write-Host "        Attempting App Service Plan creation in region '$region'..." -ForegroundColor Yellow
        # F1 (Free tier) - $0/month, chosen deliberately over B1 to fit a
        # student subscription's limited remaining credit (2026-08). 60
        # CPU-min/day cap and no "Always On" (app idles out and cold-starts
        # on the next request after ~20 min) - both fine for intermittent
        # demo/test usage, not sustained production traffic. NOT verified
        # live yet whether Oryx's remote build behaves identically on F1 -
        # the comment below this block documents build/startup behavior that
        # was confirmed specifically on B1; F1's much tighter build-time
        # resource limits (shared CPU, 1 GB storage) could plausibly cause
        # the remote build to behave differently or time out. Test via a
        # manual workflow_dispatch run before relying on this for a demo.
        $planOut = az appservice plan create --name $AppPlanName --resource-group $ResourceGroupName --sku F1 --is-linux --location $region 2>&1
        if ($LASTEXITCODE -eq 0) {
            $planCreated = $true
            Write-Host "        [OK] App Service Plan created in '$region'." -ForegroundColor Green
            break
        } else {
            Write-Host "        [WARNING] Region '$region' quota limit hit. Trying next region..." -ForegroundColor DarkYellow
        }
    }

    if (-not $planCreated) {
        Fail "Could not find an available region for F1 App Service Plan due to subscription quota limits."
    }
} else {
    Write-Host "        [OK] App Service Plan already exists - skipped." -ForegroundColor DarkGreen
}

$appExists = az webapp show --name $WebAppName --resource-group $ResourceGroupName --query name -o tsv 2>$null
if (-not $appExists) {
    az webapp create --resource-group $ResourceGroupName --plan $AppPlanName --name $WebAppName --runtime "PYTHON:3.12" -o none
    if ($LASTEXITCODE -ne 0) { Fail "Creating Web App '$WebAppName'" }
    Start-Sleep -Seconds 5
}

# Configure startup command and app settings (Managed Identity - see the
# Function App section above for why there's no DATABASE_URL/password here)
az webapp config set --resource-group $ResourceGroupName --name $WebAppName --startup-file "startup.sh" -o none

Write-Host "        Enabling Managed Identity ..." -ForegroundColor Yellow
az webapp identity assign --resource-group $ResourceGroupName --name $WebAppName -o none

az webapp config appsettings set --resource-group $ResourceGroupName --name $WebAppName --settings SCM_DO_BUILD_DURING_DEPLOYMENT="true" WEBSITES_PORT="8000" WEBSITES_CONTAINER_STARTTIME_LIMIT="1800" AZURE_SQL_SERVER="$SqlServerFqdn" AZURE_SQL_DATABASE="$SqlDbName" STREAMLIT_SERVER_PORT="8000" STREAMLIT_SERVER_ADDRESS="0.0.0.0" STREAMLIT_SERVER_HEADLESS="true" TENANT_SECRET_KEY="$sharedSecretKey" -o none
# Same reasoning as the Function App above: remove any leftover DATABASE_URL
# from a prior password-based deploy so it can't silently shadow the new
# Managed Identity path. Safe no-op if it was never set.
az webapp config appsettings delete --resource-group $ResourceGroupName --name $WebAppName --setting-names DATABASE_URL -o none 2>$null

Write-Host "        [OK] App Service settings configured. Pausing 10s for container stabilization..." -ForegroundColor Green
Start-Sleep -Seconds 10

Write-Host "        [OK] App Service ready." -ForegroundColor Green

# Code deployment via Kudu ZIP API.
# NOTE: this SKU/plan's remote build always runs Oryx with --compress-destination-dir,
# regardless of which az deploy command is used - the platform decompresses the real app
# content to a runtime-local path at container start, it is not left as plain files under
# /home/site/wwwroot. This is why startup.sh does NOT hardcode "cd /home/site/wwwroot" -
# doing so silently breaks the app the moment wwwroot has no leftover files from an older,
# differently-built deployment to fall back on. Don't reintroduce that cd.
Write-Host "        Deploying application code ..." -ForegroundColor Yellow
$appRoot = [System.IO.Path]::GetFullPath($parentPath)
$appPy   = Join-Path $appRoot "app.py"

if (Test-Path $appPy) {
    $tempDir = if ($env:TEMP) { $env:TEMP } elseif ($env:TMPDIR) { $env:TMPDIR } else { "/tmp" }
    $zipPath = Join-Path $tempDir "finops_deploy.zip"

    Write-Host "        Creating POSIX zip package for Linux App Service ..." -ForegroundColor Yellow
    $zipScript = Join-Path $PSScriptRoot "create_zip.py"
    python $zipScript

    Write-Host "        Fetching publishing credentials ..." -ForegroundColor Yellow
    $user = [string](az webapp deployment list-publishing-credentials --resource-group $ResourceGroupName --name $WebAppName --query publishingUserName -o tsv 2>$null)
    $pass = [string](az webapp deployment list-publishing-credentials --resource-group $ResourceGroupName --name $WebAppName --query publishingPassword -o tsv 2>$null)

    if ($user) { $user = $user.Trim() }
    if ($pass) { $pass = $pass.Trim() }

    if (-not $user -or -not $pass) {
        Write-Host "        [WARNING] Could not retrieve publishing credentials." -ForegroundColor DarkYellow
    } else {
        $base64  = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${user}:${pass}"))
        Write-Host "        Uploading app package via Azure CLI Async Zip Deployment ..." -ForegroundColor Yellow
        az webapp deployment source config-zip --resource-group $ResourceGroupName --name $WebAppName --src $zipPath -o none
        if ($LASTEXITCODE -eq 0) {
            Write-Host "        [OK] Code package uploaded and build initiated!" -ForegroundColor Green
        } else {
            Write-Host "        [ERROR] Deployment failed." -ForegroundColor Red
        }
    }
    Remove-Item $zipPath -ErrorAction SilentlyContinue
}

# 7. Grant both apps' Managed Identities access to the SQL Database
# Sets the current signed-in az CLI user as the SQL Server's Entra admin
# (idempotent - safe to re-run), then attempts the actual CREATE USER/ALTER
# ROLE grant automatically via grant_managed_identity_access.py - this
# genuinely CAN be automated (confirmed 2026-08-19, previously assumed it
# couldn't be): mssql-python's token_provider parameter accepts an
# AzureCliCredential directly, reusing the exact same `az login` session this
# script already requires, so no extra credential/secret is needed. A direct
# client connection (unlike the Portal's Query editor, which runs inside
# Azure's own network) needs THIS machine's own public IP allowed though -
# AllowAzureServices (from step 4) only covers Azure services - so a firewall
# rule is added just for the duration of the grant, then removed. Falls back
# to printing the manual T-SQL if the local venv/Python dependencies aren't
# available (e.g. Azure Cloud Shell, a fresh clone with no venv set up yet)
# or the automated attempt fails for any reason - never blocks the deploy.
Write-Host "  [7/7] Granting Managed Identity database access ..." -ForegroundColor Yellow
$signedInUser = az ad signed-in-user show --query "{upn:userPrincipalName, oid:id}" -o json 2>$null | ConvertFrom-Json
if ($signedInUser) {
    az sql server ad-admin create --resource-group $ResourceGroupName --server-name $SqlServerName --display-name $signedInUser.upn --object-id $signedInUser.oid -o none 2>$null
    Write-Host "        [OK] Set '$($signedInUser.upn)' as this SQL Server's Microsoft Entra admin." -ForegroundColor Green
} else {
    Write-Host "        [WARNING] Could not resolve the signed-in user (are you signed in as a service principal, not a real user?)." -ForegroundColor DarkYellow
    Write-Host "        Set a Microsoft Entra admin manually: az sql server ad-admin create --resource-group $ResourceGroupName --server-name $SqlServerName --display-name <ADMIN> --object-id <ADMIN_OBJECT_ID>" -ForegroundColor DarkYellow
}

$grantScriptPath = Join-Path $PSScriptRoot "grant_managed_identity_access.py"
$venvPython       = Join-Path $parentPath "venv\Scripts\python.exe"
$grantAutomated   = $false
if ($signedInUser -and (Test-Path $venvPython) -and (Test-Path $grantScriptPath)) {
    Write-Host "        Attempting automated grant ..." -ForegroundColor Yellow
    Start-Sleep -Seconds 10   # let the AD-admin assignment above propagate
    $myIp = $null
    try { $myIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 10) } catch {}
    if ($myIp) {
        az sql server firewall-rule create --resource-group $ResourceGroupName --server $SqlServerName --name "TempDeployAccess" --start-ip-address $myIp --end-ip-address $myIp -o none 2>$null
        Start-Sleep -Seconds 15   # firewall rule propagation
        & $venvPython $grantScriptPath $SqlServerFqdn $SqlDbName $WebAppName $FunctionAppName
        if ($LASTEXITCODE -eq 0) {
            $grantAutomated = $true
            Write-Host "        [OK] Managed Identity database access granted automatically." -ForegroundColor Green
        } else {
            Write-Host "        [WARNING] Automated grant failed - falling back to manual instructions below." -ForegroundColor DarkYellow
        }
        az sql server firewall-rule delete --resource-group $ResourceGroupName --server $SqlServerName --name "TempDeployAccess" -o none 2>$null
    } else {
        Write-Host "        [WARNING] Could not detect this machine's public IP - falling back to manual instructions below." -ForegroundColor DarkYellow
    }
} else {
    Write-Host "        [INFO] Local venv (venv\Scripts\python.exe) not found - falling back to manual instructions below." -ForegroundColor DarkYellow
}

if (-not $grantAutomated) {
    # Guarded with IF NOT EXISTS, not plain CREATE USER/ALTER ROLE - the
    # automated attempt above can fail PARTWAY through (e.g. the Web App's
    # grant succeeds, then something breaks before the Function App's does),
    # and a plain re-run of these statements would throw "principal already
    # exists" on whichever one already went through. Safe to paste this
    # whole block regardless of how much the automation completed first.
    Write-Host ""
    Write-Host "        ACTION NEEDED - run this once in the Azure Portal:" -ForegroundColor Yellow
    Write-Host "        Azure SQL Database ($SqlDbName) > Query editor (preview) > sign in with Microsoft Entra > run:" -ForegroundColor Yellow
    Write-Host ""
    foreach ($principal in @($WebAppName, $FunctionAppName)) {
        Write-Host "          IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '$principal')" -ForegroundColor White
        Write-Host "              CREATE USER [$principal] FROM EXTERNAL PROVIDER;" -ForegroundColor White
        foreach ($role in @("db_datareader", "db_datawriter", "db_ddladmin")) {
            Write-Host "          IF NOT EXISTS (SELECT 1 FROM sys.database_role_members rm JOIN sys.database_principals r ON rm.role_principal_id = r.principal_id JOIN sys.database_principals m ON rm.member_principal_id = m.principal_id WHERE r.name = '$role' AND m.name = '$principal')" -ForegroundColor White
            Write-Host "              ALTER ROLE $role ADD MEMBER [$principal];" -ForegroundColor White
        }
        Write-Host "" -ForegroundColor White
    }
    Write-Host "        (db_ddladmin is needed because this app runs its own schema migrations -" -ForegroundColor DarkGray
    Write-Host "         see db/schema.py's _ensure_column - not just plain row reads/writes.)" -ForegroundColor DarkGray
    Write-Host "        Until this runs, the app will show a clear connection error rather than silently" -ForegroundColor DarkGray
    Write-Host "         using stale/local data - check the Web App's Log stream if inventory looks empty." -ForegroundColor DarkGray
    Write-Host ""
}

Write-Host ""
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "  DEPLOYMENT COMPLETE!" -ForegroundColor Green
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "   Dashboard URL : https://$WebAppName.azurewebsites.net" -ForegroundColor Cyan
Write-Host "   Azure SQL DB  : $SqlServerName.database.windows.net/$SqlDbName" -ForegroundColor Cyan
Write-Host "   Function App  : $FunctionAppName" -ForegroundColor Cyan
Write-Host "   Resource Group: $ResourceGroupName" -ForegroundColor Cyan
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "  First load takes ~2 min while the container warms up." -ForegroundColor DarkYellow
if (-not $grantAutomated) {
    Write-Host "  Don't forget step 7 above (Query editor grant) - the app can't reach the" -ForegroundColor DarkYellow
    Write-Host "  database until that runs once." -ForegroundColor DarkYellow
}
Write-Host ""
