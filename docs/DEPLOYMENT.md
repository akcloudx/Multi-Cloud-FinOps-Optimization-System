# Deployment Guide

Full detail behind the [README](../README.md#deploy)'s quick deploy table — prerequisites, all four deployment paths in full, configuration parameters, what gets created and what it costs, and troubleshooting.

## Prerequisites

| Tool | Needed for | Notes |
|---|---|---|
| An Azure subscription | Deployment | A free/student subscription works — every default SKU is free-tier eligible |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Deployment | Not needed if using Cloud Shell |
| [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local) | Deployment | Publishes the cron worker; version matters, see [Troubleshooting](#troubleshooting) |
| Python 3.12 | Deployment + local dev | |
| PowerShell 7+ (or Windows PowerShell 5.1) | Deployment | Cloud Shell and macOS/Linux always have `pwsh` 7 |
| [GitHub CLI](https://cli.github.com/) (`gh`) | Cloning, if the repo is private | `git clone` alone doesn't support GitHub's token-based auth by default |

## Getting the Code

```bash
git clone https://github.com/akcloudx/Multi-Cloud-FinOps-Optimization-System.git
cd Multi-Cloud-FinOps-Optimization-System
```

If cloning in Azure Cloud Shell, clone under `$HOME` (its persistent storage mount) rather than `/tmp`, so it survives session recycles. On Windows, clone into a shallow path (e.g. `C:\dev\...`) rather than a deeply nested one — this repo bundles a Linux-compiled dependency fallback with some long internal paths (see [Security](../README.md#security)).

## Option A — Azure Cloud Shell (recommended, zero local install)

Nothing to install. Open [Cloud Shell](https://shell.azure.com) (or the `>_` icon in the Azure Portal), select **PowerShell**, then:

```bash
gh auth login          # only needed once, and only if the repo is private
gh repo clone akcloudx/Multi-Cloud-FinOps-Optimization-System
cd Multi-Cloud-FinOps-Optimization-System/azure_deploy
./deploy_all_resources_cloudshell.ps1
```

You'll be prompted once for a SQL admin password (8+ characters, at least 3 of uppercase/lowercase/digit/symbol — used only for the SQL Server's break-glass admin login, never by the app itself). Everything else — resource creation, code deployment, and granting the app's Managed Identity access to the database — runs automatically, including self-provisioning the one Python package a fully-automated database grant needs.

## Option B — Your own machine (Windows / macOS / Linux)

Requires the [Prerequisites](#prerequisites) installed locally. PowerShell 7+ works identically on all three platforms (`pwsh`); Windows PowerShell 5.1 is also supported.

```powershell
az login
cd azure_deploy
.\deploy_all_resources.ps1
```

On macOS/Linux, run it as `pwsh ./deploy_all_resources.ps1` instead. The script checks for `az`/`python`/`func` up front (with install links if anything's missing) and warns if your PowerShell execution policy would block future runs.

## Option C — Fast code-only redeploy

Once the infrastructure exists, push a code change in ~15 seconds without touching any Azure resource:

```powershell
cd azure_deploy
.\deploy_app_only.ps1
```

## Option D — GitHub Actions CI/CD

[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) redeploys the web app and Function App code automatically on every push to `main` (infrastructure must already exist via Option A or B first). One-time setup:

1. In the Azure Portal, open the Web App and Function App resources and download each one's **Publish Profile** (Overview → Get publish profile).
2. In this repo: **Settings → Secrets and variables → Actions**, add:
   - `AZURE_WEBAPP_FINOPS_PUBLISH_PROFILE`
   - `AZURE_FUNCTION_FINOPS_PUBLISH_PROFILE`
3. Push to `main`, or trigger manually from the **Actions** tab (`workflow_dispatch`).

## Deployment Parameters

All four scripts accept the same core parameters (Options A/B; C only needs the naming ones):

| Parameter | Default | Purpose |
|---|---|---|
| `-OwnerHandle` | `ascloudx` | Short, lowercase-alphanumeric handle that makes every resource name globally unique and readable (e.g. `sql-finops-yourhandle`) — **change this** if deploying your own copy |
| `-SqlAdminPassword` | *(prompted)* | SQL Server break-glass admin password — never used by the running app |
| `-Location` | `westus3` | Target Azure region (auto-falls back to nearby regions if quota is hit) |
| `-AppNamePrefix` | `finops` | First segment of every resource name |
| `-SqlAdminUser` | `finopsadmin` | SQL Server admin username |
| `-ResourceGroupName` | `rg-finops-<OwnerHandle>` | Override to deploy into an existing resource group |

## What Gets Created, and What It Costs

| Resource | SKU | Cost |
|---|---|---|
| Resource Group, Storage Account | Standard_LRS | ~$0.02/mo |
| Azure SQL Database | Serverless, free-tier eligible | $0 (100k vCore-seconds + 32GB/mo free) |
| Function App | Consumption (Y1) | $0 for this workload's volume |
| App Service Plan | **F1 (Free)** | $0/mo — 60 CPU-min/day cap, cold-starts after ~20 min idle |
| Log Analytics + Application Insights | Pay-as-you-go | Negligible at this log volume |

Every default is chosen to fit a free/student subscription. The F1 plan is fine for demo/evaluation use; for sustained traffic, change the App Service Plan SKU.

## Connecting a Real Tenant

After deployment, sign in (Demo mode works with zero setup) and use **Manage Tenants** to connect a real Azure subscription or AWS account. The in-app **Getting Started** guide walks through creating a least-privilege Service Principal (Azure, `Reader` role) or IAM policy (AWS) — the app never requests write access to your cloud resources.

## Troubleshooting

- **`func` version mismatch** — this project deploys with `--functions-version 4`; an older Core Tools install will fail late in the deploy. Reinstall with `npm install -g azure-functions-core-tools@4 --unsafe-perm true`.
- **PowerShell won't run the script at all** (`...cannot be loaded because running scripts is disabled...`) — Windows' default execution policy blocks unsigned scripts. Fix once: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.
- **`PasswordTooShort` from Azure SQL** — the admin password needs 8+ characters covering at least 3 of: uppercase, lowercase, digit, symbol.
- **A step hangs with no output** — some `az monitor` commands install a CLI extension on first use and can prompt for confirmation invisibly in some terminals. The deploy scripts pre-configure `az config set extension.use_dynamic_install=yes_without_prompt` to avoid this; if you hit it anyway, type `y` and Enter.
- **"Filename too long" while cloning on Windows** — clone into a shallow path (e.g. `C:\dev\...`) rather than a deeply nested one.
