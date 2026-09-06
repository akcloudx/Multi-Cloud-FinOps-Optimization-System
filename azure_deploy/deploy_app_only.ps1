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

# Same execution-policy / "downloaded from the internet" checks as
# deploy_all_resources.ps1 - see that script's header comment for the full
# reasoning. This script is documented as a standalone entry point (README's
# "Option C"), so it can just as easily be someone's first run on a fresh
# machine as deploy_all_resources.ps1's own run - it needs the same
# up-front guidance, not just the machine that already ran the main script.
$effectivePolicy = Get-ExecutionPolicy -Scope CurrentUser
if ($effectivePolicy -in @('Restricted', 'AllSigned', 'Default', 'Undefined')) {
    Write-Host ""
    Write-Host "  [WARNING] PowerShell's CurrentUser execution policy is '$effectivePolicy'." -ForegroundColor DarkYellow
    Write-Host "  This run may only be working because of a one-off bypass flag - a plain" -ForegroundColor DarkYellow
    Write-Host "  '.\deploy_app_only.ps1' could otherwise fail with:" -ForegroundColor DarkYellow
    Write-Host "    ...cannot be loaded because running scripts is disabled on this system." -ForegroundColor DarkYellow
    Write-Host "  Fix once, permanently, for your own account (no admin rights needed):" -ForegroundColor DarkYellow
    Write-Host "    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned" -ForegroundColor White
    Write-Host ""
}
try {
    if (Get-Item -Path $PSCommandPath -Stream Zone.Identifier -ErrorAction Stop) {
        Write-Host "  [WARNING] This script file is still marked 'downloaded from the internet'," -ForegroundColor DarkYellow
        Write-Host "  which can block it even under a permissive execution policy. Fix with:" -ForegroundColor DarkYellow
        Write-Host "    Unblock-File -Path `"$PSCommandPath`"" -ForegroundColor White
        Write-Host ""
    }
} catch {
    # No Zone.Identifier stream - file isn't flagged as downloaded (e.g. a
    # plain git clone). Nothing to warn about; this is the expected case.
}

# Checked up front, before packaging anything - a missing tool discovered
# mid-script leaves a half-written zip behind and PowerShell's own
# "command not found" error for a genuinely-missing executable is a raw,
# unfriendly CommandNotFoundException, not a clean message. Unlike
# deploy_all_resources.ps1, this script never touches the Function App, so
# `func` isn't required here - only `az` (the actual deploy step) and
# `python` (create_zip.py below).
Write-Host "  Checking prerequisites ..." -ForegroundColor Yellow
$missingTools = @()
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { $missingTools += "az (Azure CLI - https://learn.microsoft.com/cli/azure/install-azure-cli)" }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { $missingTools += "python (Python 3 - https://www.python.org/downloads/)" }
if ($missingTools.Count -gt 0) {
    Write-Host ""
    Write-Host "  [ERROR] Missing required tool(s):" -ForegroundColor Red
    foreach ($tool in $missingTools) { Write-Host "    - $tool" -ForegroundColor Red }
    Write-Host ""
    Write-Host "  Install/fix the above, then re-run this script. Nothing has been created yet." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
Write-Host "        [OK] az and python are both available." -ForegroundColor Green

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
