# Multi-Cloud FinOps Optimization System

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Deploy](https://img.shields.io/badge/deploy-Azure-0078D4.svg)](#deployment)

A live, multi-tenant, credential-driven cost and inventory dashboard for organizations running production workloads on **both Microsoft Azure and Amazon Web Services**. It ingests real inventory and pricing from each provider's own APIs, reconciles it against actual owned Reserved Instances / Savings Plans, and surfaces rightsizing, coverage, and FinOps maturity insights — all from one place, without needing either provider's native console.

> Built as a Capstone project for the M.Sc. in Cloud Architecture & Security (RACE, REVA University). See [AI Usage Disclosure](#ai-usage-disclosure) for how AI assistance was used in its development.

---

## Table of Contents

- [Why This Exists](#why-this-exists)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Repository Structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Getting the Code](#getting-the-code)
- [Deployment](#deployment)
  - [Option A — Azure Cloud Shell (recommended, zero local install)](#option-a--azure-cloud-shell-recommended-zero-local-install)
  - [Option B — Your own machine (Windows / macOS / Linux)](#option-b--your-own-machine-windows--macos--linux)
  - [Option C — Fast code-only redeploy](#option-c--fast-code-only-redeploy)
  - [Option D — GitHub Actions CI/CD](#option-d--github-actions-cicd)
  - [Deployment parameters](#deployment-parameters)
  - [What gets created, and what it costs](#what-gets-created-and-what-it-costs)
- [Connecting a Real Tenant](#connecting-a-real-tenant)
- [Local Development (no Azure required)](#local-development-no-azure-required)
- [Security Model](#security-model)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)
- [AI Usage Disclosure](#ai-usage-disclosure)

---

## Why This Exists

Each cloud provider's native tooling (AWS Cost Explorer, Azure Cost Management + Billing) is capable within its own estate, but neither is designed to reason about cost posture *across* both providers at once — a Reserved Instance on AWS and a Reservation on Azure are structurally different products with different scope and pricing mechanics, and neither console attempts to unify them. Commercial multi-cloud FinOps platforms exist, but they're closed-source, licensed enterprise products.

This project builds that missing piece as a live, credential-driven application: real ingestion from both providers, real retail pricing, real commitment-coverage reconciliation — with a security-conscious, least-privilege onboarding model, since the credentials a cost-visibility tool asks for are themselves a real attack surface.

## Key Features

- **Live multi-cloud ingestion** — Azure inventory via Azure Resource Graph (9 modular KQL query groups: VMs, SQL, Flexible Servers, Cosmos/DocumentDB, Cache, Storage/Disks, Analytics, miscellaneous Compute, Data Factory SSIS); AWS inventory across 14+ resource types in every enabled region, scanned in parallel.
- **Real retail pricing enrichment** — every synced resource is priced against the provider's own live Retail Prices / Price List API, not a static table.
- **Reserved Instance & Savings Plan coverage** — subscription/resource-group-scoped and Zonal/Regional AWS RI scope matching, with a real coverage waterfall (on-demand baseline → RI coverage → Savings Plan coverage → residual overage).
- **VM / EC2 rightsizing** — classifies compute resources as Underutilized / Overutilized / Optimal from *live* Azure Monitor and AWS CloudWatch CPU + memory metrics, not just demo data.
- **Actionable recommendations engine** — combined RI + Savings Plan sizing and cancellation guidance with quantified monthly dollar impact.
- **FinOps Maturity Assessment** — Crawl/Walk/Run scoring against real FinOps Foundation KPIs (Cost Optimization Index, Anomaly Detection Rate, commitment coverage thresholds), computed from actual tenant data, not fixed.
- **Multi-tenant, Demo + Production modes** — a shared, publicly-known demo login for zero-setup exploration, fully isolated from real connected-tenant data.
- **Passwordless database access** — both the web app and the sync worker authenticate to Azure SQL via System-Assigned Managed Identity; no password or connection string is ever stored anywhere.
- **Automated hourly sync** — an Azure Function on a timer trigger keeps inventory, pricing, and commitment data current without any user action.

## Architecture

```
                              ┌─────────────────────────┐
                              │      User (Browser)      │
                              └────────────┬─────────────┘
                                           │ HTTPS
                                           ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │             Streamlit Web App  (Azure App Service, Linux)         │
   │   Home · Inventory · Rightsizing · Savings Plan Analysis ·        │
   │   RI Coverage · Recommendations · Maturity Assessment             │
   └───────────────┬───────────────────────────────┬───────────────────┘
                   │ on-demand sync                  │ read (no live API calls)
                   ▼                                  ▼
   ┌──────────────────────────────┐      ┌─────────────────────────────┐
   │   data/sync_pipeline.py       │      │      db/schema.py            │
   │   (shared orchestrator)       │◄────►│   (SQLAlchemy → Azure SQL)   │
   └───────┬───────────────┬───────┘      └─────────────────────────────┘
           │               │                              ▲
           ▼               ▼                              │ hourly sync
 ┌─────────────────┐ ┌─────────────────┐    ┌──────────────────────────────┐
 │ azure_conn/      │ │ aws/             │    │  Azure Function App          │
 │ connector.py     │ │ connector.py     │    │  (Consumption plan,          │
 │ Resource Graph,  │ │ boto3, 14+ types,│    │  hourly timerTrigger)        │
 │ Monitor metrics  │ │ every region,    │    │  runs the same sync_pipeline │
 │                  │ │ CloudWatch       │    │  independent of the web app  │
 └────────┬─────────┘ └────────┬─────────┘    └──────────────────────────────┘
          │                    │
          ▼                    ▼
 ┌──────────────────┐ ┌──────────────────┐
 │ Azure Retail      │ │ AWS Price List /  │
 │ Prices API        │ │ Cost Explorer API │
 └──────────────────┘ └──────────────────┘
```

Both the Web App and the Function App connect to Azure SQL via their own **System-Assigned Managed Identity** — there is no password, connection string, or secret anywhere in configuration. A shared encryption key (`TENANT_SECRET_KEY`, generated once at deploy time and reused on every redeploy) protects each connected tenant's stored client secret at rest.

## Tech Stack

| Layer | Technology |
|---|---|
| UI | [Streamlit](https://streamlit.io/) |
| Data | pandas, NumPy, Plotly |
| ORM / DB access | SQLAlchemy (`mssql-python` passwordless dialect) |
| Database | Azure SQL Database (Serverless, free-tier eligible) |
| Azure SDKs | `azure-identity`, `azure-mgmt-resourcegraph`, `azure-mgmt-compute`, `azure-mgmt-monitor`, `azure-mgmt-resource`, `azure-mgmt-reservations`, `azure-mgmt-billingbenefits` |
| AWS SDK | `boto3` / `botocore` |
| Compute (web) | Azure App Service, Linux, Python 3.12 |
| Compute (cron) | Azure Function App, Python 3.12, Consumption plan |
| Auth | System-Assigned Managed Identity (DB), bcrypt-hashed shared login (app) |
| Secrets at rest | `cryptography` (Fernet) |

Full pinned versions are in [`requirements.txt`](requirements.txt).

## Repository Structure

```
app.py                    Streamlit entry point / UI
ui/                       Page-level UI components
data/sync_pipeline.py     Shared ingestion orchestrator (web app + Function App)
azure_conn/connector.py   Azure Resource Graph, Monitor metrics, IAM checks
aws/connector.py          AWS multi-region inventory, CloudWatch metrics, IAM checks
pricing/                  Retail pricing + commitment pricing enrichment
analysis/                 Rightsizing, RI/SP eligibility, recommendations, maturity scoring
commitments/              Commitment (RI/SP) matching logic
db/                       SQLAlchemy models, engine factory, crypto, seed/demo data
azure_function/           Azure Function App (hourly cron worker)
azure_deploy/             Deployment scripts (see Deployment below)
.github/workflows/        GitHub Actions CI/CD for code-only redeploys
```

## Prerequisites

| Tool | Needed for | Notes |
|---|---|---|
| An Azure subscription | Deployment | A free/student subscription works — the default SKUs are all free-tier eligible |
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

If you're cloning in Azure Cloud Shell, clone under `$HOME` (its persistent storage mount) rather than `/tmp`, so it survives session recycles. On Windows, clone into a shallow path (e.g. `C:\dev\...`) rather than a deeply nested folder — this repo bundles a Linux-compiled dependency fallback ([see below](#security-model)) with some long internal paths.

## Deployment

Four ways to deploy, depending on where you're running from. All of them provision the same architecture and are fully idempotent — safe to re-run if something fails partway through.

### Option A — Azure Cloud Shell (recommended, zero local install)

Nothing to install. Open [Cloud Shell](https://shell.azure.com) (or the `>_` icon in the Azure Portal), select **PowerShell**, then:

```bash
gh auth login          # only needed once, and only if the repo is private
gh repo clone akcloudx/Multi-Cloud-FinOps-Optimization-System
cd Multi-Cloud-FinOps-Optimization-System/azure_deploy
./deploy_all_resources_cloudshell.ps1
```

You'll be prompted once for a SQL admin password (8+ characters, at least 3 of uppercase/lowercase/digit/symbol — this is only ever used for the SQL Server's break-glass admin login, never by the app itself). Everything else — resource creation, code deployment, and granting the app's Managed Identity access to the database — runs automatically, including self-provisioning the one Python package a fully-automated database grant needs.

### Option B — Your own machine (Windows / macOS / Linux)

Requires the [Prerequisites](#prerequisites) installed locally. PowerShell 7+ works identically on all three platforms (`pwsh`); Windows PowerShell 5.1 is also supported.

```powershell
az login
cd azure_deploy
.\deploy_all_resources.ps1
```

If you're on macOS/Linux, run it with `pwsh ./deploy_all_resources.ps1` instead. The script checks for `az`/`python`/`func` up front (with install links if anything's missing) and warns if your PowerShell execution policy would block future runs.

### Option C — Fast code-only redeploy

Once the infrastructure exists, push a code change in ~15 seconds without touching any Azure resource:

```powershell
cd azure_deploy
.\deploy_app_only.ps1
```

### Option D — GitHub Actions CI/CD

[`​.github/workflows/deploy.yml`](.github/workflows/deploy.yml) redeploys the web app and Function App code automatically on every push to `main` (infrastructure must already exist via Option A or B first). One-time setup:

1. In the Azure Portal, open the Web App and Function App resources and download each one's **Publish Profile** (Overview → Get publish profile).
2. In this repo: **Settings → Secrets and variables → Actions**, add:
   - `AZURE_WEBAPP_FINOPS_PUBLISH_PROFILE`
   - `AZURE_FUNCTION_FINOPS_PUBLISH_PROFILE`
3. Push to `main`, or trigger manually from the **Actions** tab (`workflow_dispatch`).

### Deployment parameters

All four scripts accept the same core parameters (Options A/B; C only needs the naming ones):

| Parameter | Default | Purpose |
|---|---|---|
| `-OwnerHandle` | `ascloudx` | Short, lowercase-alphanumeric handle that makes every resource name globally unique and readable (e.g. `sql-finops-yourhandle`) — **change this** if deploying your own copy |
| `-SqlAdminPassword` | *(prompted)* | SQL Server break-glass admin password — never used by the running app |
| `-Location` | `westus3` | Target Azure region (auto-falls back to nearby regions if quota is hit) |
| `-AppNamePrefix` | `finops` | First segment of every resource name |
| `-SqlAdminUser` | `finopsadmin` | SQL Server admin username |
| `-ResourceGroupName` | `rg-finops-<OwnerHandle>` | Override to deploy into an existing resource group |

### What gets created, and what it costs

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

## Local Development (no Azure required)

```bash
python -m venv venv
./venv/bin/pip install -r requirements.txt      # Windows: venv\Scripts\pip
streamlit run app.py
```

Sign in with **Demo Mode** — no cloud credentials needed. It reads from a local SQLite database seeded with realistic multi-service Azure and AWS inventory.

## Security Model

- **No standing credentials in code.** Both compute resources authenticate to the database via Managed Identity; connected-tenant client secrets are encrypted at rest with a key generated once at deploy time, never hardcoded.
- **Least-privilege by design.** The app only ever requests read-level access (Azure `Reader` role; a documented, minimal AWS IAM policy) — verified against each provider's own policy simulator during testing.
- **Vendored Linux dependency fallback.** `azure_sdk_vendor/` bundles pre-built Linux binaries for a subset of dependencies as a fallback if a live `pip install` fails during deployment's remote build step. It's explicitly guarded to activate on Linux only and never shadow a real install (see `app.py`).
- **History-audited.** This repository's git history has been scanned with both [gitleaks](https://github.com/gitleaks/gitleaks) and [betterleaks](https://github.com/betterleaks/betterleaks) and contains no live secrets.

Found a security issue? Please open a private security advisory rather than a public issue.

## Troubleshooting

- **`func` version mismatch** — this project deploys with `--functions-version 4`; an older Core Tools install will fail late in the deploy. Reinstall with `npm install -g azure-functions-core-tools@4 --unsafe-perm true`.
- **PowerShell won't run the script at all** (`...cannot be loaded because running scripts is disabled...`) — Windows' default execution policy blocks unsigned scripts. Fix once: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.
- **`PasswordTooShort` from Azure SQL** — the admin password needs 8+ characters covering at least 3 of: uppercase, lowercase, digit, symbol.
- **A step hangs with no output** — some `az monitor` commands install a CLI extension on first use and can prompt for confirmation invisibly in some terminals. The deploy scripts pre-configure `az config set extension.use_dynamic_install=yes_without_prompt` to avoid this; if you hit it anyway, type `y` and Enter.
- **"Filename too long" while cloning on Windows** — clone into a shallow path (e.g. `C:\dev\...`) rather than a deeply nested one; see [Getting the Code](#getting-the-code).

## Contributing

Issues and pull requests are welcome. For anything non-trivial, please open an issue first to discuss the change.

## License

MIT — see [LICENSE](LICENSE).

## AI Usage Disclosure

Portions of this project's code, documentation, and this README were developed with AI assistance (Claude, Anthropic), used for grammar/language, structuring, code generation, and data analysis under the author's direction and review. See the project report's own AI Usage Disclosure Statement for the full, formal breakdown.
