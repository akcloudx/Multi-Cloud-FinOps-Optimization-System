# Multi-Cloud FinOps Optimization System
## Complete System Architecture & Mentor Defense Guide

This document serves as the authoritative architectural blueprint and technical defense reference for the **Multi-Cloud FinOps Optimization System** (Azure & AWS).

---

## 🏛️ System Architecture & Ingestion Data Flow

Per the Capstone Design Architecture, the system decouples API extraction from UI presentation through an automated **24-Hour Ingestion Pipeline**:

```
┌─────────────────────────┐       Data Extraction       ┌─────────────────────────────────┐
│  AWS & Azure Cloud APIs │ ──────────────────────────► │ Azure Function (Python Script)  │
│  (OIDC / SP Credentials)│                             │ (24-Hour Cron TimerTrigger)     │
└─────────────────────────┘                             └────────────────┬────────────────┘
                                                                         │ Ingest & Normalize
                                                                         ▼
┌─────────────────────────┐         Data Flow           ┌─────────────────────────────────┐
│    Streamlit Portal     │ ◄────────────────────────── │ Relational Data Repository (DB) │
│  (UI Dashboard app.py)  │                             │ (Star Schema: Azure SQL / DB)   │
└─────────────────────────┘                             └─────────────────────────────────┘
```

### Architectural Data Flow Steps:
1. **API Ingestion (Extraction Layer):** Azure Resource Graph and AWS Cost APIs are queried using Service Principal / IAM credentials.
2. **Automated Cron Pipeline (`azure_function/function_app.py`):** An Azure Function running a Python Timer Trigger (`0 0 0 * * *`) executes every 24 hours at 00:00 UTC to extract, transform, and normalize raw cloud payload into the FOCUS schema.
3. **Star Schema Repository (`db/schema.py`):** Normalized data is ingested into the relational database (`CloudInventory`, `Commitment`, `ReconciliationLog`, `SyncLog` tables). Supports both SQLite for local portability and **Azure SQL (Free Tier)** for production.
4. **Streamlit UI Portal (`app.py`):** The dashboard queries the normalized Star Schema database (not live API loops), ensuring instant page loads, zero API rate limiting, and 24-hour historical consistency.

---

## 📁 File-by-File Breakdown & Source Code Reference

### 1. `app.py` — Streamlit Web Application Interface
- **Role:** Presentation and decision support portal.
- **Key Responsibilities:**
  - Provides top navigation, cloud provider toggle (`Azure` vs `AWS`), and environment mode selection (`Demo / Benchmark Mode` vs `Live Cloud API`).
  - Implements strict live API state validation: displays empty state alerts if live mode is selected without valid credentials rather than silently returning mock data.
  - Dynamically renders provider-specific titles, icons, metric cards, inventory tables, and policy coverage expanders.
  - Displays **24-Hour Cron Sync Status** and provides a manual trigger button to execute `data/sync_pipeline.py`.

### 2. `db/schema.py` — Multi-Cloud Database Schema & Engine Router
- **Role:** Database layer definition using SQLAlchemy ORM (aligned with FOCUS specifications).
- **Key Models:**
  - `CloudInventory`: Asset registry storing resource metadata, state, region, OS, SKU, PAYG rates, and orphan flags.
  - `Commitment`: Active RI and Savings Plan contract positions ($/hr commitment, reserved quantities, expiry).
  - `ReconciliationLog`: Output log for hourly waterfall evaluation.
  - `Recommendation`: Actionable FinOps directives with severity levels and financial impacts.
  - `SyncLog`: Ingestion audit log capturing timestamp, status, and records synced by the 24h cron pipeline.
- **Multi-DB Engine Factory:** `get_engine(provider)` routes queries to `azure_finops.db` or `aws_finops.db` (or Azure SQL via ODBC string).

### 3. `azure_function/function_app.py` — Azure Function 24h Cron Worker
- **Role:** Azure Function v2 Python Timer Trigger (`schedule="0 0 0 * * *"`).
- **Functionality:** Runs automatically every 24 hours at 00:00 UTC to extract cloud API data and populate the Azure SQL Star Schema database.

### 4. `data/sync_pipeline.py` — Ingestion Engine Pipeline
- **Role:** Core pipeline logic invoked by both the Azure Function and Streamlit manual trigger button.

### 5. `db/seed.py` — Azure Mock Data & Policy Matrix
- **Role:** Generates realistic Azure infrastructure inventory and commitments in Star Schema format.
- **Scope:** 14 resources across Azure VMs, SQL Database, SQL Managed Instance, PostgreSQL, MySQL, Cosmos DB, Blob Storage, Files, Redis, Synapse, Databricks, and Disk Storage.

### 6. `db/aws_seed.py` — AWS Mock Data & Policy Matrix
- **Role:** Generates realistic AWS infrastructure inventory and commitments in Star Schema format.
- **Scope:** EC2 instances (`m5.large`, `c5.xlarge`, `t3.medium`), RDS PostgreSQL/MySQL, Amazon Aurora, DynamoDB, ElastiCache, Redshift, and OpenSearch.

---

## 🚀 Complete Azure Live Production Deployment Guide

The complete architecture consists of **3 Azure Resources**:
1. **Azure SQL Database (Basic / Free Tier)** $\rightarrow$ Relational Star Schema Data Repository.
2. **Azure Function App (Consumption Plan)** $\rightarrow$ 24-Hour Automated Cron Ingestion Worker.
3. **Azure App Service (Linux Web App B1)** $\rightarrow$ Streamlit Dashboard UI.

---

### Option A: Automated Provisioning Script (Recommended)

Run the automated PowerShell deployment script located at [`azure_deploy/deploy_all_resources.ps1`](file:///d:/Aakif/Capstone%20Project/files/azure_deploy/deploy_all_resources.ps1):

```powershell
az login
.\azure_deploy\deploy_all_resources.ps1
```

This single command provisions the Azure SQL Server, Database, Function App (24h Cron), and Web App automatically!

---

### Option B: Step-by-Step Azure CLI Deployment Guide

#### Step 1: Login & Create Resource Group
```bash
az login
az group create --name "rg-finops-optimizer" --location "eastus"
```

#### Step 2: Provision Azure SQL Database (Star Schema Repository)
```bash
# Create Azure SQL Server
az sql server create \
  --name "finops-sql-server-2026" \
  --resource-group "rg-finops-optimizer" \
  --location "eastus" \
  --admin-user "finopsadmin" \
  --admin-password "***REMOVED-SECRET***"

# Allow Azure Services firewall access
az sql server firewall-rule create \
  --resource-group "rg-finops-optimizer" \
  --server "finops-sql-server-2026" \
  --name "AllowAzureServices" \
  --start-ip-address "0.0.0.0" \
  --end-ip-address "0.0.0.0"

# Create Azure SQL Database (Basic Tier)
az sql db create \
  --resource-group "rg-finops-optimizer" \
  --server "finops-sql-server-2026" \
  --name "finops-sqldb" \
  --service-objective Basic
```

#### Step 3: Provision & Deploy Azure Function App (24h Cron Worker)
```bash
# Create Storage Account
az storage account create \
  --name "stfinops2026" \
  --location "eastus" \
  --resource-group "rg-finops-optimizer" \
  --sku Standard_LRS

# Create Azure Function App
az functionapp create \
  --resource-group "rg-finops-optimizer" \
  --consumption-plan-location "eastus" \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --name "finops-cron-func" \
  --storage-account "stfinops2026" \
  --os-type Linux

# Publish Function Code
cd azure_function
func azure functionapp publish finops-cron-func --python
cd ..
```

#### Step 4: Provision & Deploy Azure App Service (Streamlit Dashboard)
```bash
# Create App Service Plan
az appservice plan create \
  --name "finops-plan" \
  --resource-group "rg-finops-optimizer" \
  --sku B1 \
  --is-linux

# Create Web App
az webapp create \
  --resource-group "rg-finops-optimizer" \
  --plan "finops-plan" \
  --name "finops-optimizer-app" \
  --runtime "PYTHON:3.11"

# Configure Startup Command & Azure SQL Connection String
az webapp config set \
  --resource-group "rg-finops-optimizer" \
  --name "finops-optimizer-app" \
  --startup-file "python -m streamlit run app.py --server.port 8000 --server.address 0.0.0.0"

az webapp config appsettings set \
  --resource-group "rg-finops-optimizer" \
  --name "finops-optimizer-app" \
  --settings DATABASE_URL="mssql+pyodbc://finopsadmin:***REMOVED-SECRET***@finops-sql-server-2026.database.windows.net/finops-sqldb?driver=ODBC+Driver+18+for+SQL+Server"

# Deploy Web App Code
az webapp up \
  --resource-group "rg-finops-optimizer" \
  --name "finops-optimizer-app" \
  --runtime "PYTHON:3.11"
```

Once completed, your complete enterprise environment will be live:
- **Streamlit Web Portal:** `https://finops-optimizer-app.azurewebsites.net`
- **Azure SQL Database:** `finops-sql-server-2026.database.windows.net/finops-sqldb`
- **Azure Function 24h Cron:** `finops-cron-func` (Runs automatically every night at 00:00 UTC)
