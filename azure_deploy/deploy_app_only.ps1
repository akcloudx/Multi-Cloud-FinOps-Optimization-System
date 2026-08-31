# azure_deploy/deploy_app_only.ps1
# Fast Code-Only Deployment Script for Multi-Cloud FinOps Optimization System
# Pushes local application code changes to Azure App Service in ~15 seconds without touching infrastructure.

# Naming convention matches deploy_all_resources.ps1's own 2026-08-30
# redesign - see that script's header comment for the full reasoning
# (readable owner handle instead of an opaque subscription-ID hash).
param (
    [string]$ResourceGroupName = "",
    [string]$AppNamePrefix     = "finops",
    [string]$OwnerHandle       = "ascloudx"
)

$subId = (az account show --query id -o tsv 2>&1).Trim()
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Run 'az login' first." -ForegroundColor Red
    exit 1
}

$suffix = ($OwnerHandle -replace '[^a-zA-Z0-9]', '').ToLower()
if (-not $suffix) {
    Write-Host "[ERROR] OwnerHandle resolved empty after removing non-alphanumeric characters." -ForegroundColor Red
    exit 1
}
if (-not $ResourceGroupName) { $ResourceGroupName = "rg-$AppNamePrefix-$suffix" }
$WebAppName = "app-$AppNamePrefix-$suffix"

Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host "  FAST LIVE CODE DEPLOYMENT (Streamlit App Service)" -ForegroundColor Cyan
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host "   App Name      : $WebAppName" -ForegroundColor DarkCyan
Write-Host "   Resource Group: $ResourceGroupName" -ForegroundColor DarkCyan
Write-Host "========================================================================" -ForegroundColor Cyan

# 1. Package Python files into finops_deploy.zip using POSIX zip tool
Write-Host "  [1/2] Creating Linux POSIX zip package..." -ForegroundColor Yellow
$PSScriptRootDir = $PSScriptRoot
$zipScript = Join-Path $PSScriptRootDir "create_zip.py"
python $zipScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Failed to create zip package." -ForegroundColor Red
    exit 1
}

$tempDir = if ($env:TEMP) { $env:TEMP } elseif ($env:TMPDIR) { $env:TMPDIR } else { "/tmp" }
$zipPath = Join-Path $tempDir "finops_deploy.zip"

# 2. Deploy directly to App Service via Azure CLI
Write-Host "  [2/2] Pushing code directly to Azure App Service '$WebAppName'..." -ForegroundColor Yellow
az webapp deployment source config-zip --resource-group $ResourceGroupName --name $WebAppName --src $zipPath -o none

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "========================================================================" -ForegroundColor Green
    Write-Host "  CODE DEPLOYMENT SUCCESSFUL!" -ForegroundColor Green
    Write-Host "========================================================================" -ForegroundColor Green
    Write-Host "   Live Dashboard: https://$WebAppName.azurewebsites.net" -ForegroundColor Cyan
    Write-Host "========================================================================" -ForegroundColor Green
} else {
    Write-Host "[ERROR] Code deployment failed." -ForegroundColor Red
}

Remove-Item $zipPath -ErrorAction SilentlyContinue
