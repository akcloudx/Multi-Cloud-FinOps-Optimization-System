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
#    below prints a one-time manual T-SQL grant you run in the Portal Query editor to finish setup.

# SqlAdminPassword has no default on purpose - it used to be a hardcoded
# real password here (found and fixed 2026-08, alongside the same
# credential duplicated in db/schema.py and azure_deploy/seed_azure_sql.py).
# Pass it explicitly, e.g.:
#   .\deploy_all_resources.ps1 -SqlAdminPassword (Read-Host -AsSecureString "SQL admin password" | ConvertFrom-SecureString -AsPlainText)
#
# NOTE 2026-08: SqlAdminUser/SqlAdminPassword are ONLY used to create the SQL
# Server's own break-glass admin login (Azure SQL requires some admin
# credential at server-creation time) - the app itself no longer uses them
# at all. Both the Web App and Function App now connect via their own
# System-Assigned Managed Identity (passwordless, no secret anywhere) - see
# db/schema.py's get_engine() and the "Managed Identity setup" step below.
param (
    [string]$ResourceGroupName = "rg-finops-optimizer",
    [string]$Location          = "westus3",
    [string]$SqlAdminUser      = "finopsadmin",
    [Parameter(Mandatory = $true)]
    [string]$SqlAdminPassword,
    [string]$AppNamePrefix     = "finops"
)

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "  [ERROR] FAILED: $msg" -ForegroundColor Red
    Write-Host "  Fix the issue above and re-run the script." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

$subId = (az account show --query id -o tsv 2>&1).Trim()
if ($LASTEXITCODE -ne 0) { Fail "Cannot reach Azure CLI. Run 'az login' first." }

$suffix             = $subId.Replace("-","").Substring(0,8).ToLower()
$StorageAccountName = "$($AppNamePrefix)st$suffix"
$SqlServerName      = "$AppNamePrefix-sql-$suffix"
$SqlDbName          = "finops-db"
$FunctionAppName    = "$AppNamePrefix-func-$suffix"
$WebAppName         = "$AppNamePrefix-app-$suffix"
$AppPlanName        = "$AppNamePrefix-plan-$suffix"

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
    $sqlOut = az sql server create --name $SqlServerName --resource-group $ResourceGroupName --location $Location --admin-user $SqlAdminUser --admin-password $SqlAdminPassword
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
        az sql server create --name $SqlServerName --resource-group $ResourceGroupName --location $Location --admin-user $SqlAdminUser --admin-password $SqlAdminPassword -o none
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
    az functionapp create --resource-group $ResourceGroupName --consumption-plan-location $Location --runtime python --runtime-version 3.12 --functions-version 4 --name $FunctionAppName --storage-account $StorageAccountName --os-type Linux -o none
    if ($LASTEXITCODE -ne 0) { Fail "Creating Function App '$FunctionAppName'" }
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

# Fix PowerShell 5.1 Join-Path syntax: use nested 2-argument Join-Path calls
$parentPath   = Join-Path $PSScriptRoot ".."
$funcCodePath = [System.IO.Path]::GetFullPath((Join-Path $parentPath "azure_function"))

if (Test-Path $funcCodePath) {
    Write-Host "        Publishing cron function code ..." -ForegroundColor Yellow
    Push-Location $funcCodePath
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

az webapp config appsettings set --resource-group $ResourceGroupName --name $WebAppName --settings SCM_DO_BUILD_DURING_DEPLOYMENT="true" WEBSITES_PORT="8000" WEBSITES_CONTAINER_STARTTIME_LIMIT="1800" AZURE_SQL_SERVER="$SqlServerFqdn" AZURE_SQL_DATABASE="$SqlDbName" STREAMLIT_SERVER_PORT="8000" STREAMLIT_SERVER_ADDRESS="0.0.0.0" STREAMLIT_SERVER_HEADLESS="true" -o none

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
# This is the one step that genuinely can't be fully automated from here:
# it requires running T-SQL AS a Microsoft Entra admin against the database
# itself, not just an ARM/az CLI resource operation. Sets the current
# signed-in az CLI user as the SQL Server's Entra admin (idempotent - safe
# to re-run), then prints the exact statements to run once in the Portal's
# built-in Query Editor (Azure SQL Database > Query editor - authenticates
# with your Entra login directly, no extra firewall rule or local sqlcmd
# install needed).
Write-Host "  [7/7] Granting Managed Identity database access ..." -ForegroundColor Yellow
$signedInUser = az ad signed-in-user show --query "{upn:userPrincipalName, oid:id}" -o json 2>$null | ConvertFrom-Json
if ($signedInUser) {
    az sql server ad-admin create --resource-group $ResourceGroupName --server-name $SqlServerName --display-name $signedInUser.upn --object-id $signedInUser.oid -o none 2>$null
    Write-Host "        [OK] Set '$($signedInUser.upn)' as this SQL Server's Microsoft Entra admin." -ForegroundColor Green
} else {
    Write-Host "        [WARNING] Could not resolve the signed-in user (are you signed in as a service principal, not a real user?)." -ForegroundColor DarkYellow
    Write-Host "        Set a Microsoft Entra admin manually: az sql server ad-admin create --resource-group $ResourceGroupName --server-name $SqlServerName --display-name <ADMIN> --object-id <ADMIN_OBJECT_ID>" -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "        ACTION NEEDED - run this once in the Azure Portal:" -ForegroundColor Yellow
Write-Host "        Azure SQL Database ($SqlDbName) > Query editor (preview) > sign in with Microsoft Entra > run:" -ForegroundColor Yellow
Write-Host ""
Write-Host "          CREATE USER [$WebAppName] FROM EXTERNAL PROVIDER;" -ForegroundColor White
Write-Host "          ALTER ROLE db_datareader ADD MEMBER [$WebAppName];" -ForegroundColor White
Write-Host "          ALTER ROLE db_datawriter ADD MEMBER [$WebAppName];" -ForegroundColor White
Write-Host "          ALTER ROLE db_ddladmin ADD MEMBER [$WebAppName];" -ForegroundColor White
Write-Host "          CREATE USER [$FunctionAppName] FROM EXTERNAL PROVIDER;" -ForegroundColor White
Write-Host "          ALTER ROLE db_datareader ADD MEMBER [$FunctionAppName];" -ForegroundColor White
Write-Host "          ALTER ROLE db_datawriter ADD MEMBER [$FunctionAppName];" -ForegroundColor White
Write-Host "          ALTER ROLE db_ddladmin ADD MEMBER [$FunctionAppName];" -ForegroundColor White
Write-Host ""
Write-Host "        (db_ddladmin is needed because this app runs its own schema migrations -" -ForegroundColor DarkGray
Write-Host "         see db/schema.py's _ensure_column - not just plain row reads/writes.)" -ForegroundColor DarkGray
Write-Host "        Until this runs, the app will show a clear connection error rather than silently" -ForegroundColor DarkGray
Write-Host "         using stale/local data - check the Web App's Log stream if inventory looks empty." -ForegroundColor DarkGray
Write-Host ""

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
Write-Host "  Don't forget step 7 above (Query editor grant) - the app can't reach the" -ForegroundColor DarkYellow
Write-Host "  database until that runs once." -ForegroundColor DarkYellow
Write-Host ""
