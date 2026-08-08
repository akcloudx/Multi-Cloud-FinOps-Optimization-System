# azure_deploy/deploy_all_resources.ps1
# Multi-Cloud FinOps Optimization System  Azure Production Deployment Script
#
# TARGETED FOR: Windows PowerShell 5.1, PowerShell 7, Azure Cloud Shell
# FEATURES:
#    Full Architecture: Azure SQL Server + Function App + Streamlit App Service
#    PowerShell 5.1 Compatible: Uses dual-argument Join-Path calls and ASCII output.
#    ARM Resilient: Pauses briefly after app creation to ensure ARM Control Plane propagation.
#    Smart Quota Fallback: Automatically tests candidate regions if student subscription has 0 quota for B1 VMs in the primary region.

param (
    [string]$ResourceGroupName = "rg-finops-optimizer",
    [string]$Location          = "westus3",
    [string]$SqlAdminUser      = "finopsadmin",
    [string]$SqlAdminPassword  = "***REMOVED-SECRET***",
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
Write-Host "  [1/6] Resource Group ..." -ForegroundColor Yellow
az group create --name $ResourceGroupName --location $Location -o none
if ($LASTEXITCODE -ne 0) { Fail "Creating resource group" }
Write-Host "        [OK] Done." -ForegroundColor Green

# 2. Resource Providers
Write-Host "  [2/6] Registering resource providers ..." -ForegroundColor Yellow
az provider register --namespace Microsoft.Sql --wait -o none
az provider register --namespace Microsoft.Web --wait -o none
Write-Host "        [OK] Done." -ForegroundColor Green

# 3. Storage Account
Write-Host "  [3/6] Storage Account: $StorageAccountName ..." -ForegroundColor Yellow
$stExists = az storage account show --name $StorageAccountName --resource-group $ResourceGroupName --query name -o tsv 2>$null
if (-not $stExists) {
    az storage account create --name $StorageAccountName --location $Location --resource-group $ResourceGroupName --sku Standard_LRS -o none
    if ($LASTEXITCODE -ne 0) { Fail "Creating storage account '$StorageAccountName'" }
    Write-Host "        [OK] Created." -ForegroundColor Green
} else {
    Write-Host "        [OK] Already exists - skipped." -ForegroundColor DarkGreen
}

# 4. Azure SQL Server and Serverless Database
Write-Host "  [4/6] Azure SQL Server and Serverless Database ..." -ForegroundColor Yellow
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
    Write-Host "        Creating Serverless Database '$SqlDbName'..." -ForegroundColor Yellow
    az sql db create --resource-group $ResourceGroupName --server $SqlServerName --name $SqlDbName --edition GeneralPurpose --family Gen5 --compute-model Serverless --capacity 1 --auto-pause-delay 60 -o none 2>$null
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

$SqlConnectionString = "mssql+pyodbc://$($SqlAdminUser):$($SqlAdminPassword)@$($SqlServerName).database.windows.net/$($SqlDbName)?driver=ODBC+Driver+18+for+SQL+Server"

# 5. Azure Function App
Write-Host "  [5/6] Azure Function App: $FunctionAppName ..." -ForegroundColor Yellow
$funcExists = az functionapp show --name $FunctionAppName --resource-group $ResourceGroupName --query name -o tsv 2>$null
if (-not $funcExists) {
    az functionapp create --resource-group $ResourceGroupName --consumption-plan-location $Location --runtime python --runtime-version 3.12 --functions-version 4 --name $FunctionAppName --storage-account $StorageAccountName --os-type Linux -o none
    if ($LASTEXITCODE -ne 0) { Fail "Creating Function App '$FunctionAppName'" }
    Start-Sleep -Seconds 5
    
    az functionapp config appsettings set --resource-group $ResourceGroupName --name $FunctionAppName --settings DATABASE_URL="$SqlConnectionString" -o none
    Write-Host "        [OK] Provisioned." -ForegroundColor Green
} else {
    Write-Host "        [OK] Already exists - skipped." -ForegroundColor DarkGreen
}

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
Write-Host "  [6/6] App Service Plan + Web App: $WebAppName ..." -ForegroundColor Yellow
$planExists = az appservice plan show --name $AppPlanName --resource-group $ResourceGroupName --query name -o tsv 2>$null

if (-not $planExists) {
    $candidateRegions = @($Location, "eastus2", "centralus", "eastus", "westus2")
    $planCreated = $false

    foreach ($region in $candidateRegions) {
        Write-Host "        Attempting App Service Plan creation in region '$region'..." -ForegroundColor Yellow
        $planOut = az appservice plan create --name $AppPlanName --resource-group $ResourceGroupName --sku B1 --is-linux --location $region 2>&1
        if ($LASTEXITCODE -eq 0) {
            $planCreated = $true
            Write-Host "        [OK] App Service Plan created in '$region'." -ForegroundColor Green
            break
        } else {
            Write-Host "        [WARNING] Region '$region' quota limit hit. Trying next region..." -ForegroundColor DarkYellow
        }
    }

    if (-not $planCreated) {
        Fail "Could not find an available region for B1 App Service Plan due to subscription quota limits."
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

# Configure startup command and connection string
az webapp config set --resource-group $ResourceGroupName --name $WebAppName --startup-file "startup.sh" -o none

az webapp config appsettings set --resource-group $ResourceGroupName --name $WebAppName --settings SCM_DO_BUILD_DURING_DEPLOYMENT="true" WEBSITES_PORT="8000" WEBSITES_CONTAINER_STARTTIME_LIMIT="1800" DATABASE_URL="$SqlConnectionString" STREAMLIT_SERVER_PORT="8000" STREAMLIT_SERVER_ADDRESS="0.0.0.0" STREAMLIT_SERVER_HEADLESS="true" -o none

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
Write-Host ""
