# PROJECT CONTEXT — Multi-Cloud FinOps Optimization System
> **Last Updated:** 2026-08-03 | **Session:** abac3507-bd34-4463-a831-c42042c6e521
> Read this file at the start of every new conversation to resume work without repeating discovery.

---

## 👤 Project Identity
| Field | Value |
|---|---|
| **Student** | Aakif Shaikh |
| **Program** | M.Sc. in Cloud Architecture and Security |
| **Batch** | CAS04 |
| **SRN** | R24DI005 |
| **Institution** | REVA Academy for Corporate Excellence (RACE) |
| **Capstone Subject** | Multi-Cloud FinOps Optimization System |

---

## 📁 Workspace Root
```
d:\Aakif\Capstone Project\files\
```

---

## 📄 Key Documents Read
| File | What It Contains |
|---|---|
| `README.md` | Full technical spec: schemas, 2-pass waterfall math, Python POC code, hourly simulation logic |
| `Aakif_Shaikh_CAS04_Multi-Cloud FinOps Optimization System_2026.docx` | Academic proposal: 5-phase scope, FOCUS framework, OIDC auth, Azure SQL Star Schema, Azure Functions, Waterfall algorithm |
| `Aakif_Shaikh_CAS04_Multi-Cloud FinOps Optimization System_2026.pptx` | 15-slide presentation: background, objectives, methodology, proposed solution, system requirements |

---

## 🎯 Confirmed Scope Decisions (from user)
| Decision | Answer |
|---|---|
| Cloud providers | **Azure-first now**, AWS-ready architecture added later |
| Data backend | **Local SQLite** (SQLAlchemy) — Star Schema mirror for easy Azure SQL migration |
| ETL orchestration | **Inside Streamlit** (no Azure Functions for now) |
| Features to build | **All best-fit** — see feature list below |

---

## 🏗️ What Is Being Built

### Core System: 2-Pass Priority Waterfall Reconciliation Engine
The main academic contribution. Simulates a 30-day × 24-hour billing window:

**Pass 1 — Rigid Reserved Instance (RI) Allocation**
- Match: `SKU == scope_sku` AND `Region == scope_region` AND `OS == OS`
- Unutilized RI hours (e.g. Dev VMs off at night) → logged as **Rigid Commitment Leakage**

**Pass 2 — Flexible Savings Plan (SP) Allocation**
- Match: Any remaining Compute/DB PAYG overage (no SKU/region constraint)
- Logs: SP absorbed cost, Final PAYG Overage

**Safety Buffer Engine**
- Recommended SP Commitment = Mean Hourly PAYG Overage × Safety Buffer (default 80%)
- Buffer slider: 50%–100%, adjustable in sidebar

**Business-Hours Shutdown Anomaly**
- VMs with `Avg Daily Running Hours == 10` run only 08:00–18:00
- RI hours outside this window = wasted = Rigid Leakage
- Logic: `is_running = (hour >= 8 and hour < 18)` for 10-hr VMs

**Orphaned Resource Detection**
- VMs with `State == "Stopped (deallocated)"` whose SKU is covered by active RI → flagged ⚠️

---

## 📊 Streamlit Dashboard Structure (5 Tabs)
| Tab | Content |
|---|---|
| **Tab 1: ☁️ Inventory Scan** | Resources, power states, OS, region, PAYG cost USD, daily running hours |
| **Tab 2: ⚡ Waterfall Reconciliation** | 30-day mid-day snapshot table, stacked bar chart (RI vs SP vs Uncovered), hourly absorption profile |
| **Tab 3: 💰 Savings Plan Analysis** | KPI cards (baseline vs existing vs recommended), leakage warnings, intermittent VM alerts |
| **Tab 4: 🏷️ RI Coverage** | Per-SKU gap/excess table, orphaned RI drain table |
| **Tab 5: 🤖 What-If Simulator** | Slider to model extra SP commitment, projected savings chart, recommendation action cards |

**Sidebar:** Safety Buffer Slider (50–100%, default 80%), simulation days (7/14/30), region filter

---

## 📁 File Structure (Target)
```
d:\Aakif\Capstone Project\files\
├── app.py                            ← Main Streamlit entry point [REWRITE]
├── requirements.txt                  ← [UPDATE: add plotly, sqlalchemy]
├── .streamlit/
│   └── config.toml                   ← Theme config [NEW]
├── db/
│   ├── __init__.py                   ← [NEW]
│   ├── schema.py                     ← SQLite DDL + SQLAlchemy [NEW]
│   └── seed.py                       ← Mock data seeder [NEW]
├── data/
│   └── inventory_loader.py           ← Reads from SQLite [MODIFY]
├── pricing/
│   └── retail_pricing.py             ← Expanded PAYG rates [MODIFY]
├── commitments/
│   └── existing_commitments.py       ← RI + SP from SQLite [MODIFY]
└── analysis/
    └── engine.py                     ← Full 2-pass waterfall [REWRITE]
```

---

## 💾 Database Schema (SQLite — Star Schema)

### Table: `cloud_inventory`
```sql
resource_id, resource_name, resource_type, resource_state,
region, os, sku, payg_hourly_usd, avg_daily_running_hours,
subscription, provider (= 'Azure')
```

### Table: `commitments`
```sql
commitment_id, commitment_type ('Reserved Instance'|'Savings Plan'),
scope_sku, scope_region, os, hourly_usd_commitment,
reserved_qty (RI only), term, expiry_date
```

### Table: `reconciliation_log`
```sql
date, hour, resource_id, sku, payg_rate, covered_by_ri,
covered_by_sp, final_payg_overage
```

### Table: `recommendations`
```sql
recommendation_type, resource_id, message, action, severity
```

---

## 🧮 Key Math (from README)

$$\text{Final PAYG Overage}_{r,h} = \max(0, \text{PAYG Rate}_{r,h} - \text{RI Allocation}_{r,h} - \text{SP Allocation}_{r,h})$$

$$\text{Recommended SP Commitment} = \text{Mean Hourly PAYG Overage} \times \text{Safety Buffer}$$

$$\text{Leakage} = \text{Commitment Capacity} - \text{Utilized Commitment}$$

$$\text{Efficiency \%} = \frac{\text{Utilized}}{\text{Total Potential}} \times 100$$

---

## 💱 Currency
**All prices in USD ($)** — matches Azure Pricing Calculator default. Use `usd(x, decimals)` helper from `pricing/retail_pricing.py`.

---

## 🗺️ Mock Data (Azure, australiaeast region focus)

### Inventory
| Resource | Type | State | Region | OS | SKU | PAYG $/hr | Hrs/day |
|---|---|---|---|---|---|---|---|
| VM-Prod-01 | Compute | Running | australiaeast | Windows | Standard_D4ds_v5 | 0.284 | 24 |
| VM-Prod-02 | Compute | Running | australiaeast | Windows | Standard_D4ds_v5 | 0.284 | 24 |
| VM-Dev-03 | Compute | Running | australiaeast | Windows | Standard_B2ms | 0.106 | 10 |
| VM-Dev-04 | Compute | Running | australiasoutheast | Windows | Standard_B2s | 0.053 | 10 |
| DB-Prod-01 | Database | Running | australiaeast | Linux | Standard_E4s_v5 | 0.300 | 24 |
| VM-Legacy-05 | Compute | Stopped (deallocated) | australiaeast | Windows | Standard_D4ds_v4 | 0.284 | 0 |
| VM-Prod-06 | Compute | Running | australiaeast | Windows | Standard_D4ds_v4 | 0.284 | 24 |
| VM-Prod-07 | Compute | Running | australiaeast | Windows | Standard_D4ds_v4 | 0.284 | 24 |
| SQL-Prod-01 | Database | Running | australiaeast | N/A | GP_Gen5_4 | 0.526 | 24 |

### Commitments
| ID | Type | SKU | Region | OS | $/hr | Qty | Term |
|---|---|---|---|---|---|---|---|
| RI-D4DS-V5-AE | Reserved Instance | Standard_D4ds_v5 | australiaeast | Windows | 0.20 | 2 | 1-year |
| RI-D4DS-V4-AE | Reserved Instance | Standard_D4ds_v4 | australiaeast | Windows | 0.19 | 3 | 1-year |
| SP-GLOBAL-001 | Savings Plan | Any Compute/DB | Global | Any | 1.10/hr | N/A | 1-year |

---

## ✅ Build Progress
| Phase | Status | Notes |
|---|---|---|
| Requirements & Scope | ✅ Done | Azure-first, SQLite, Streamlit-only ETL |
| Implementation Plan | ✅ Done | `implementation_plan.md` created |
| DB Layer (`db/`) | ✅ Done | `db/schema.py`, `db/seed.py`, `db/__init__.py` |
| Data Layer | ✅ Done | `data/inventory_loader.py` reads from SQLite |
| Pricing Module | ✅ Done | `pricing/retail_pricing.py` expanded USD rates |
| Commitments Module | ✅ Done | `commitments/existing_commitments.py` reads from SQLite |
| Engine Rewrite | ✅ Done | `analysis/engine.py` — full 2-pass waterfall + all analyses |
| Streamlit Dashboard | ✅ Done | `app.py` — 5 tabs with Plotly charts |
| Requirements + Theme | ✅ Done | `requirements.txt` + `.streamlit/config.toml` |
| Verification | ✅ Done | App launched and verified |

## 📝 Files Created / Modified in This Session
| File | Action | Description |
|---|---|---|
| `PROJECT_CONTEXT.md` | NEW | This file — resume context for future sessions |
| `db/__init__.py` | NEW | DB package init |
| `db/schema.py` | NEW | SQLite Star Schema (SQLAlchemy ORM) |
| `db/seed.py` | NEW | Mock data seeder (10 resources, 3 commitments) |
| `data/inventory_loader.py` | MODIFIED | Reads from SQLite, FOCUS-aligned schema |
| `pricing/retail_pricing.py` | MODIFIED | Expanded PAYG rates, USD formatter |
| `commitments/existing_commitments.py` | MODIFIED | Reads RI + SP from SQLite |
| `analysis/engine.py` | REWRITTEN | Full 2-pass waterfall, SP/RI analysis, What-If, recommendations |
| `app.py` | REWRITTEN | 5-tab Streamlit dashboard with Plotly charts |
| `requirements.txt` | MODIFIED | Added plotly, sqlalchemy, numpy |
| `.streamlit/config.toml` | NEW | Dark FinOps theme |

---

## 🔮 Future Phases (deferred)
- **AWS Support:** Add `data/aws_inventory_loader.py`, `pricing/aws_pricing.py` with Cost Explorer API. FOCUS schema already compatible.
- **Azure Functions:** Move ETL seeding to `function_app.py` with CRON trigger.
- **OIDC Auth:** Add Workload Identity Federation for live Azure/AWS API calls.
- **Azure SQL Migration:** Change `db/schema.py` connection string from SQLite to `mssql+pyodbc://...`

---

## 📚 References (from PPTX)
1. M. S. de Brito et al., "Automated Cloud Cost Optimization," IEEE Access, 2023.
2. S. Dustdar et al., "Governance Frameworks for Multi-Cloud," IEEE Cloud Computing, 2022.
3. Y. Ju et al., "Event-Driven Serverless Architectures," IEEE Trans. Services Computing, 2023.
4. J. Opara-Martins et al., "Vendor Lock-In Strategies," IEEE Systems Journal, 2020.
