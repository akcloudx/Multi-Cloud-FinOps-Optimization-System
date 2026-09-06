# Multi-Cloud FinOps Optimization System — Architecture Reference

This is the deeper technical reference behind the project [README](README.md) — internal component structure, data flow, and data model, for anyone extending the codebase or defending its design decisions. For "how do I deploy this," see the README's [Deployment](README.md#deployment) section instead; this document assumes the system is already running and explains how it's built.

Every diagram and claim below is verified directly against the current codebase, not aspirational — if a design element here doesn't match what's in the code, the code is right and this document is stale (please open an issue).

---

## 1. System Architecture

```
                              ┌───────────────────┐
                              │   User (Browser)   │
                              └──────────┬──────────┘
                                         │ HTTPS
                                         ▼
                          ┌───────────────────────────────┐        ┌──────────────────────────┐
                          │   Streamlit Web Application     │        │     Azure Function App    │
                          │   Azure App Service, Linux, F1   │        │  Consumption plan (Y1) -   │
                          └───────────────┬─────────────────┘        │  hourly timerTrigger       │
                                          │ on-demand sync            └─────────────┬──────────────┘
                     ┌────────────────────┴─────────────────┐                       │ hourly sync
                     ▼                                      │                       │
        ┌─────────────────────────────┐                     │                       │
        │   Application / Pipeline     │◄────────────────────┴───────────────────────┘
        │   Layer (Python)              │
        │   ─────────────────────────   │
        │   Azure Connector              │──┐
        │   AWS Connector                 │  │
        │   Pricing Engine                 │  │  External Cloud Provider APIs
        │   Analysis Engine                 │  │  ─────────────────────────
        └─────────────────────────────┘  ├─►│ Azure Resource Graph (KQL, 9 query groups)
                     │                       ├─►│ Azure Monitor (VM CPU/memory metrics)
                     │  SQLAlchemy ORM        ├─►│ AWS SDK (boto3) — EC2, RDS, IAM, CloudWatch, etc.
                     ▼  (all pipeline modules) └─►│ Azure Retail Prices API / AWS Price List API
        ┌─────────────────────────────┐
        │      Azure SQL Database       │◄── dashboard reads (no live API calls)
        │  General Purpose, Serverless,  │
        │  auto-pause                     │
        └─────────────────────────────┘
```

Both entry points — the web app (on-demand sync) and the Function App (hourly, automatic) — call into the **same** `data/sync_pipeline.py` orchestrator, so there is exactly one code path for "how inventory gets into the database," regardless of what triggered it. The dashboard's own reads never trigger a live cloud API call; a sync must have already populated the database, which keeps normal browsing fast and free of provider rate limits.

The **Azure Connector** and **AWS Connector** are the only two modules that ever talk to an external cloud provider directly; every downstream module (Pricing Engine, Analysis Engine) works against the normalized, provider-agnostic row shape those connectors produce — a new resource type, or even a third provider, only ever needs a new connector, not changes to pricing or analysis.

## 2. Component / Module Design

The Application/Pipeline Layer above expands into 16 real Python modules across five concerns. Presented here as three linked diagrams rather than one dense one, since each showing the real import/call relationships from the codebase, not idealized ones.

### 2a. UI Layer and its direct callees

```
UI Layer - app.py (Streamlit)
Home | Inventory | Rightsizing | Savings Plan Analysis | RI Coverage | Recommendations | Maturity Assessment
   │
   ├── triggers sync ──► data/sync_pipeline.py
   ├── reads inventory ► data/inventory_loader.py     (read-only — never triggers a live sync)
   ├── renders analysis► analysis/engine.py           (cost waterfall, SP/RI coverage, recommendations)
   ├──────────────────► analysis/rightsizing.py       (CPU/memory classification)
   └──────────────────► analysis/ri_eligibility.py / sp_eligibility.py (per-provider eligibility rules)
```

`inventory_loader.py` is deliberately a separate module from `sync_pipeline.py` so a page render can never accidentally trigger a live sync.

### 2b. Sync Pipeline — provider connectors and pricing

```
data/sync_pipeline.py
   ├──► azure_conn/connector.py    (Resource Graph, 9 KQL query groups)
   ├──► aws/connector.py           (14+ resource types, every enabled region)
   ├──► pricing/azure_retail_api.py    (enrich pricing)
   ├──► pricing/aws_price_list.py
   └──► pricing/commitment_pricing.py  (RI / SP retail rates)

   Credentials/Auth: Service Principal (Azure) · IAM Role (AWS)
   pricing/cache_admin.py: manual cache refresh utility (ops-only, not on the sync path)
```

### 2c. Persistence layer

```
sync_pipeline.py ──persist──► db/schema.py
inventory_loader.py ──read (dashed)──► db/schema.py
pricing/{azure_retail_api,aws_price_list,commitment_pricing}.py ──read (dashed)──► db/schema.py

db/schema.py (SQLAlchemy models: CloudInventory, RetailPrice, ReservationPurchase, SavingsPlan)
   alongside: Tenants/Users tables (multi-tenant isolation) · Sessions/Auth tables (login, roles)
```

No module writes to the database directly outside `db/schema.py`.

The Analysis layer is intentionally split into three modules by concern rather than kept as one file: `engine.py` owns cross-cutting analysis (cost waterfall, SP/RI coverage, the combined recommendations engine), `rightsizing.py` owns utilisation-based classification, and `ri_eligibility.py` / `sp_eligibility.py` own each provider's separate, real eligibility rules — a change to AWS Savings Plan eligibility logic cannot accidentally affect Azure Reservation eligibility logic, because they're different modules.

## 3. Data Flow (one sync cycle)

```
Azure Tenant ──┐
               ├─resource data─► 1.0 Ingest Live Inventory ──raw inventory──► D1 CloudInventory
AWS Account ───┘                        │  ▲                                        │
                                         │  └───────────────── read ─────────────────┘
                                         ▼
                              2.0 Enrich with Retail Pricing ◄──cache──► D3 RetailPrice Cache
                                         │ priced inventory
                                         ▼
                              3.0 Match RI / SP Commitments ◄──owned RI/SP──  D2 Commitment Tables
                                         │ coverage-matched
                                         ▼
                              4.0 Analyse & Recommend  ──dashboard/recommendations──► FinOps User
                                         ▲
                                         └── utilisation history (read directly from D1, for rightsizing)
```

Pricing enrichment (2.0) and commitment matching (3.0) are separate processes rather than one combined step, because the two facts are independent in reality: a resource can have a known on-demand price with no commitment coverage, known coverage with no priceable on-demand rate, or both.

## 4. Data Model (core tables)

```
CloudTenant                     RetailPrice
  PK id                           PK id
  provider, mode, tenant_name     provider, region, sku
  tenant_id, subscription_id      effective_hourly_rate_usd
  last_synced_at                  fetched_at
  sync_interval_hours             (NOT tenant-scoped — shared cache, matched
       │ 1                         at query time by provider+region+SKU,
       │                           no FK to CloudTenant)
       │ N
       ▼
  CloudInventory              ReservationPurchase           SavingsPlan
    PK id                       PK id                         PK id
    FK tenant_id                FK tenant_id                  FK tenant_id
    resource_id, resource_type  commitment_id, scope_sku       commitment_id
    region, sku, resource_state term, expiry_date               hourly_usd_commitment
    payg_hourly_cost_usd        hourly_usd_commitment            term

User                             UserSession
  PK id                            PK token
  email, role                      FK user_id
  created_at, last_login_at        expires_at
     │ 1
     └──────N──────────────────────────┘
```

`CloudTenant` is the root of the tenant-scoped side of the schema — every `CloudInventory`, `ReservationPurchase`, and `SavingsPlan` row carries a `tenant_id` foreign key, so all data for one connected account can be deleted or re-synced independently of every other tenant. `RetailPrice` is deliberately **not** tenant-scoped: it's a shared, provider/region/SKU-keyed cache reused across every tenant of that provider, matched at query time rather than joined by a database relationship — the alternative (a price row per tenant) would mean re-fetching an identical retail rate once per tenant instead of once per SKU/region.

`User`/`UserSession` are entirely separate from the `CloudTenant` subtree — a session represents who is logged into this application, not a cloud provider identity, and carries no relationship to any tenant, since one logged-in user can connect several tenants across both providers.

## 5. Repository Map

See the README's [Repository Structure](README.md#repository-structure) for the top-level layout. Module-level detail:

| Module | Role |
|---|---|
| `app.py` | Streamlit entry point, routing, and the seven top-level pages |
| `ui/` | Page-level UI components rendered by `app.py` |
| `data/sync_pipeline.py` | Shared ingestion orchestrator — called by both the web app and the Function App |
| `data/inventory_loader.py` | Read-only inventory queries for the dashboard |
| `azure_conn/connector.py` | Azure Resource Graph queries, Azure Monitor utilisation metrics, IAM permission checks |
| `aws/connector.py` | Multi-region AWS inventory (parallelized via `ThreadPoolExecutor`), CloudWatch metrics, IAM permission checks |
| `pricing/` | Retail pricing enrichment (Azure Retail Prices API, AWS Price List API) and commitment (RI/SP) pricing |
| `analysis/engine.py` | Cost waterfall, RI/SP coverage analysis, combined recommendations engine |
| `analysis/rightsizing.py` | CPU/memory utilisation classification (Underutilized/Overutilized/Optimal) |
| `analysis/ri_eligibility.py`, `analysis/sp_eligibility.py` | Per-provider commitment eligibility rules |
| `commitments/` | RI/SP scope-matching logic (subscription/resource-group scope for Azure, Zonal/Regional for AWS) |
| `db/schema.py` | SQLAlchemy models and the multi-tenant/multi-provider engine factory |
| `db/crypto.py` | Fernet encryption for tenant client secrets at rest |
| `db/seed.py`, `db/aws_seed.py` | Demo-mode seed data |
| `azure_function/function_app.py` | Azure Function App entry point — hourly `timerTrigger` calling `sync_pipeline.py` |
| `azure_deploy/` | Deployment scripts — see the README |

## 6. Design Decisions Worth Knowing

- **Passwordless database auth.** Both compute resources use System-Assigned Managed Identity against Azure SQL. No `DATABASE_URL`, password, or connection string is stored in app settings or code. See `db/schema.py::get_engine()`.
- **Hourly sync, not daily.** The Function App runs on an hourly `timerTrigger`, independent of whether a user has the web app open — commitment coverage and rightsizing data stay current without manual action.
- **F1 (Free) App Service tier by default.** Chosen deliberately to fit a free/student subscription — 60 CPU-min/day cap, cold-starts after ~20 minutes idle. Fine for demo/evaluation traffic; change the SKU for production load.
- **Named Consumption plan for the Function App.** `az functionapp create --consumption-plan-location` alone auto-generates an unnamed plan; the deploy scripts explicitly name it via a lower-level ARM resource create, since Azure CLI's plan-creation commands don't support the Y1/Dynamic tier directly.
- **Provider-agnostic core, provider-specific edges.** Only the two connector modules know about Azure/AWS API shapes; everything past that point (pricing, analysis, persistence) works against one normalized row shape.

---

For deployment instructions, configuration, security model, and troubleshooting, see the [README](README.md).
