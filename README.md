```python
markdown_content = """# Multi-Cloud FinOps Optimization System
## Technical Architecture & Product Documentation

This document provides a comprehensive technical reference for the **Multi-Cloud FinOps Optimization System**. It contains detailed specifications for schemas, logic layers, mathematical functions, and complete source code to support a production deployment within an enterprise infrastructure.

---

## 1. Executive Summary & Core Objective

The Multi-Cloud FinOps Optimization System is a local proof-of-concept (POC) and production architecture designed to ingest multi-cloud infrastructure usage payloads and reconcile them against existing cloud commitments (Reserved Instances and Savings Plans). 

The primary business objective is to eliminate proprietary nesting discrepancies, identify real-time financial **Leakage** (wasted commitment spend), measure **Overage** (uncovered on-demand compute spend), and resolve the **Business-Hours Shutdown Anomaly** where resources are shut down after business hours but continue to drain rigid commitments.

### Key Capabilities
* **Granular Time-Grain Profiling:** Evaluates infrastructure data at an hourly precision grain rather than applying broad monthly averages.
* **Rigid vs. Flexible Tiering:** Enforces a multi-pass priority waterfall queue separating strict resource bounds from flexible dollar pools.
* **Deterministic Recommendation Layer:** Computes data-driven capacity expansion metrics fortified with a built-in safety buffer factor.

---

## 2. Functional Architecture & Core Logic Layers


```

```text
FILE_CREATED_SUCCESSFULLY


```

[Cloud Resources & DB Scan] ──> [Normalized Business Schema Mapping]
│
▼
┌──────────────────────────────┐
│  Priority Waterfall Queue   │
└──────────────┬───────────────┘
│
┌────────────────────────┴────────────────────────┐
▼                                                 ▼
[Pass 1: Strict RI Allocation]                    [Pass 2: Flexible SP Allocation]
(Perfect Region + SKU Match)                       (Global Portfolio Overage Absorption)
│                                                 │
└────────────────────────┬────────────────────────┘
│
▼
┌──────────────────────────────┐
│ Executive Dashboard & Buffer │
│    Recommendation Engine     │
└──────────────────────────────┘

```

### Layer 1: Ingestion & Normalization Scan
Ingests cloud cost and usage payloads (e.g., Azure Cost Management JSON column/row vectors) and normalizes them into human-readable business tracking domains. It tracks the core execution parameters: operational state, host region, instance size profile, default pricing benchmarks in USD, and specific baseline usage distributions.

### Layer 2: Strict Reserved Instance (RI) Matching
Executes Pass 1 of the reconciliation engine. Because Reserved Instances are structurally rigid, they demand a perfect constraint match. A resource's properties must align seamlessly across two specific boundary fields:
1. **SKU Matching:** `Workload SKU == Commitment Scope SKU`
2. **Regional Placement:** `Workload Region == Commitment Scope Region`

If an RI is allocated to an intermittent workload that shuts down after hours, the system logs the idle capacity window directly as **Rigid Commitment Leakage**.

### Layer 3: Flexible Savings Plan (SP) Matching
Executes Pass 2 of the reconciliation engine. Compute Savings Plans are dollar-backed ($/hour commitment pools) and can float dynamically across regions, database engines, operating systems, and size families. The system routes all remaining compute overages from Pass 1 into this layer to maximize portfolio utilization.

### Layer 4: Actionable Recommendation & Safety Buffer Engine
Evaluates the downstream efficiency metrics to issue precise cloud directives. To protect the organization against over-purchasing commitments based on high business-hour peaks, it implements a **Stability Factor / Safety Buffer**. The engine calculates a safe procurement volume anchored strictly to the steady-state valley line, minimizing downstream waste when development boxes go offline at night.

---

## 3. Detailed Schema Definitions

### 3.1 Input Environment Scan Schema
This schema standardizes the discovered compute and database assets before processing calculations.

| Field Name | Data Type | Sample Value | Description |
| :--- | :--- | :--- | :--- |
| `Resource ID` | String | `VM-Prod-01` | Unique identifier for the instance asset. |
| `ResourceType` | String | `Compute` | Architectural class (e.g., Compute, Database). |
| `Resource State` | String | `Running` | Operational condition (`Running`, `Stopped (deallocated)`). |
| `Region` | String | `australiaeast` | Targeted cloud deployment zone name. |
| `OS` | String | `Windows` | Host operating system. |
| `Current SKU` | String | `Standard_D4ds_v5` | The hardware family size designation. |
| `PAYG Hourly Cost USD` | Float | `0.28` | Retail standard price rate pulled from the pricing calculator. |
| `Avg Daily Running Hours`| Integer | `24` | Metric capturing the business-hours profile (0 to 24). |

### 3.2 Commitments Inventory Schema
Defines the structure of contract positions held by the organization.

| Field Name | Data Type | Sample Value | Description |
| :--- | :--- | :--- | :--- |
| `commitment_id` | String | `RI-D4DS-V5-POOL` | Unique contract reference index. |
| `commitment_type` | String | `Reserved Instance` | Structural family class (`Reserved Instance`, `Savings Plan`). |
| `scope_sku` | String | `Standard_D4ds_v5` | target hardware size constraint (`Any Compute/DB` for SP). |
| `scope_region` | String | `australiaeast` | Geographic regional target rule boundary (`Global` for SP). |
| `hourly_usd_commitment`| Float | `12.00` | Contractual cash burn rate dedicated per hour. |
| `expiry_date` | String (Date)| `2026-11-01` | Calendar schedule cutoff date. |

### 3.3 Reconciled Granular Output Schema (Hourly Log Snapshot)
The final reporting format detailing asset tracking records for mid-day snapshots.

| Field Name | Data Type | Sample Value | Description |
| :--- | :--- | :--- | :--- |
| `Date` | Date | `2026-07-24` | Day index identifier. |
| `Hour` | String | `12:00` | Granular hourly evaluation checkpoint block. |
| `Resource ID` | String | `VM-Prod-01` | Target asset identity tracking token. |
| `SKU` | String | `Standard_D4ds_v5` | Instance family configuration profile size. |
| `PAYG Rate per Hour` | Float | `0.28` | Current theoretical baseline retail price fee. |
| `Covered by RI Cost` | Float | `0.20` | Financial cash cost absorbed during Pass 1 processing. |
| `Covered by SP Cost` | Float | `0.08` | Financial cash cost absorbed during Pass 2 processing. |
| `Final PAYG Overage Cost`| Float | `0.00` | Net unassigned out-of-pocket billing charge amount. |

---

## 4. Mathematical Models & Equations

### 4.1 Hourly Out-of-Pocket Overage
For any individual resource ($r$) during a specific discrete hour ($h$), the remaining out-of-pocket invoice rate is computed as:

$$\text{Final PAYG Overage Cost}_{r,h} = \max\left(0, \text{PAYG Rate}_{r,h} - \text{RI Allocation}_{r,h} - \text{SP Allocation}_{r,h}\right)$$

### 4.2 Total Potential Hourly Capacity Pool
The absolute capacity pool of a given commitment contract package ($c$) for a single evaluation slice is defined as:

$$\text{Potential Capacity}_{c} = \text{Hourly USD Commitment}_{c} \times 24 \text{ hours} \times \text{Billing Lifecycle Days}$$

### 4.3 Actionable Safety Buffer Framework
When the system identifies ongoing unassigned retail overages, the recommendation engine calculates a safe buying buffer to prevent over-purchasing:

$$\text{Recommended New Commitment Volume} = \text{Mean Hourly PAYG Overage} \times \text{Safety Buffer Multiplier}$$

Where the **Safety Buffer Multiplier** is hardcoded to `0.80`. This creates a conservative 20% safety boundary margin protection zone to avoid matching volatile business-hour daytime usage peaks.

---

## 5. Complete Production-Grade Python Implementation

This code is optimized for local verification runs using zero cloud credentials, utilizing structured datasets to replicate multi-cloud enterprise footprints.

```python
\"\"\"
Multi-Cloud FinOps Optimization System - Core Reconciliation Engine
File: finops_poc.py
\"\"\"

from datetime import date, timedelta
import pandas as pd
import numpy as np

def scan_cloud_environment():
    \"\"\"
    Simulates a live environment scan across infrastructure assets.
    Tracks running hours to capture business-hours shutdown anomalies.
    All pricing metrics are benchmarked strictly in USD.
    \"\"\"
    columns = [
        "Resource ID", "ResourceType", "Resource State", "Region", 
        "OS", "Current SKU", "PAYG Hourly Cost USD", "Avg Daily Running Hours"
    ]
    
    rows = [
        ["VM-Prod-01", "Compute", "Running", "australiaeast", "Windows", "Standard_D4ds_v5", 0.28, 24],
        ["VM-Prod-02", "Compute", "Running", "australiaeast", "Windows", "Standard_D4ds_v5", 0.28, 24],
        ["VM-Dev-03", "Compute", "Running", "australiaeast", "Windows", "Standard_B2ms", 0.11, 10],   # Intermittent Business-Hours
        ["VM-Dev-04", "Compute", "Running", "australiasoutheast", "Windows", "Standard_B2s", 0.05, 10],  # Intermittent Business-Hours
        ["DB-Prod-01", "Database", "Running", "australiaeast", "Linux", "Standard_E4s_v5", 0.30, 24],
        ["VM-Legacy-05", "Compute", "Stopped (deallocated)", "australiaeast", "Windows", "Standard_D4ds_v4", 0.28, 0]
    ]
    return pd.DataFrame(rows, columns=columns)


def fetch_existing_commitments():
    \"\"\"
    Fetches active contract coverage baseline configurations.
    \"\"\"
    return pd.DataFrame([
        {
            "commitment_id": "RI-D4DS-V5-POOL",
            "commitment_type": "Reserved Instance",
            "scope_sku": "Standard_D4ds_v5", 
            "scope_region": "australiaeast",
            "hourly_usd_commitment": 0.20,
            "expiry_date": "2026-11-01"
        },
        {
            "commitment_id": "SP-GLOBAL-COMPUTE",
            "commitment_type": "Savings Plan",
            "scope_sku": "Any Compute/DB", 
            "scope_region": "Global",
            "hourly_usd_commitment": 0.15,
            "expiry_date": "2027-01-15"
        }
    ])


def process_finops_waterfall(inventory_df: pd.DataFrame, commitments_df: pd.DataFrame) -> tuple:
    \"\"\"
    Reconciles hourly infrastructure asset load against active discount positions
    using a multi-pass priority queue based on structural rigidity.
    \"\"\"
    detailed_billing_records = []
    today = date.today()
    simulated_days = [today - timedelta(days=i) for i in range(30)]
    
    total_sp_potential = 0.0
    total_sp_utilized = 0.0
    total_ri_potential = 0.0
    total_ri_utilized = 0.0

    for current_day in simulated_days:
        for hour in range(24):
            # Isolate pools for this discrete hour
            active_ri = commitments_df[commitments_df["commitment_type"] == "Reserved Instance"].copy()
            active_sp = commitments_df[commitments_df["commitment_type"] == "Savings Plan"].copy()
            
            ri_pools = {idx: row["hourly_usd_commitment"] for idx, row in active_ri.iterrows()}
            sp_pools = {idx: row["hourly_usd_commitment"] for idx, row in active_sp.iterrows()}
            
            # Accumulate potential capacities
            for idx, row in active_ri.iterrows(): total_ri_potential += row["hourly_usd_commitment"]
            for idx, row in active_sp.iterrows(): total_sp_potential += row["hourly_usd_commitment"]

            # Formulate the hourly infrastructure load demands
            workload_demand = []
            for _, res in inventory_df.iterrows():
                is_running = False
                if res["Resource State"] == "Running":
                    if res["Avg Daily Running Hours"] == 24:
                        is_running = True
                    elif res["Avg Daily Running Hours"] == 10 and (8 <= hour < 18):
                        is_running = True
                
                hourly_cost = res["PAYG Hourly Cost USD"] if is_running else 0.0
                
                workload_demand.append({
                    "Resource ID": res["Resource ID"],
                    "SKU": res["Current SKU"],
                    "Region": res["Region"],
                    "Remaining PAYG Cost": hourly_cost,
                    "Covered By RI": 0.0,
                    "Covered By SP": 0.0
                })

            # PASS 1: Rigid SKU/Region Matching (Reserved Instances)
            for idx, ri_commitment in active_ri.iterrows():
                for res in workload_demand:
                    if (res["SKU"] == ri_commitment["scope_sku"] and 
                        res["Region"] == ri_commitment["scope_region"] and 
                        res["Remaining PAYG Cost"] > 0 and ri_pools[idx] > 0):
                        
                        allocated_ri = min(res["Remaining PAYG Cost"], ri_pools[idx])
                        ri_pools[idx] -= allocated_ri
                        total_ri_utilized += allocated_ri
                        res["Remaining PAYG Cost"] -= allocated_ri
                        res["Covered By RI"] += allocated_ri

            # PASS 2: Flexible Financial Pool Matching (Savings Plans)
            for idx, sp_commitment in active_sp.iterrows():
                for res in workload_demand:
                    if res["Remaining PAYG Cost"] > 0 and sp_pools[idx] > 0:
                        allocated_sp = min(res["Remaining PAYG Cost"], sp_pools[idx])
                        sp_pools[idx] -= allocated_sp
                        total_sp_utilized += allocated_sp
                        res["Remaining PAYG Cost"] -= allocated_sp
                        res["Covered By SP"] += allocated_sp

            # Record sample logs for the mid-day snapshot visualization
            for res in workload_demand:
                if hour == 12:  # Isolate 12:00 PM for the executive matrix reporting view
                    detailed_billing_records.append({
                        "Date": current_day,
                        "Hour": f"{hour}:00",
                        "Resource ID": res["Resource ID"],
                        "SKU": res["SKU"],
                        "PAYG Rate per Hour": round(res["Remaining PAYG Cost"] + res["Covered By RI"] + res["Covered By SP"], 2),
                        "Covered by RI Cost": round(res["Covered By RI"], 2),
                        "Covered by SP Cost": round(res["Covered By SP"], 2),
                        "Final PAYG Overage Cost": round(res["Remaining PAYG Cost"], 2)
                    })

    # Compile efficiency tables
    summary_data = [
        {"Commitment Type": "Reserved Instances", "Total Potential USD": total_ri_potential, "Utilized USD": total_ri_utilized},
        {"Commitment Type": "Savings Plans", "Total Potential USD": total_sp_potential, "Utilized USD": total_sp_utilized}
    ]
    summary_df = pd.DataFrame(summary_data)
    summary_df["Leakage (Wasted Dollars)"] = (summary_df["Total Potential USD"] - summary_df["Utilized USD"]).round(2)
    summary_df["Efficiency"] = (summary_df["Utilized USD"] / summary_df["Total Potential USD"] * 100).round(1).astype(str) + "%"
    
    return pd.DataFrame(detailed_billing_records), summary_df


def generate_finops_recommendations(summary_df: pd.DataFrame, detailed_bill: pd.DataFrame):
    \"\"\"
    Analyzes portfolio run-rates to generate automated sizing and procurement actions.
    Applies an 80% safety boundary factor to mitigate off-hour capacity waste.
    \"\"\"
    print("\\n" + "=" * 115)
    print("🤖 ACTIONABLE FINOPS RECOMMENDATION ENGINE REPORT")
    print("=" * 115)
    
    ri_leakage = summary_df.loc[summary_df["Commitment Type"] == "Reserved Instances", "Leakage (Wasted Dollars)"].values[0]
    avg_hourly_overage = detailed_bill["Final PAYG Overage Cost"].mean()

    if ri_leakage > 200.0:
        print("• [ACTION REQUIRED]: CANCEL or MODIFY underutilized Reserved Instances (RIs).\\n"
              "  Reasoning: Substantial financial leakage detected. Intermittent workloads (e.g., Dev environments)\\n"
              "  are shut down after business hours, causing rigid RI hours to sit completely unutilized at night.\\n"
              "  Strategy Shift: Move intermittent workloads from RI coverage to a flexible Savings Plan (SP) profile.\\n")

    if avg_hourly_overage > 0.10:
        recommended_sp_buffer_increase = round(avg_hourly_overage * 0.80, 2)
        print(f"• [RECOMMENDATION]: PURCHASE additional Savings Plan capacity of ${recommended_sp_buffer_increase}/hr.\\n"
              f"  Reasoning: Ongoing unassigned PAYG overages detected. Applying a conservative 20% safety buffer\\n"
              f"  protects your financial baseline against valley drop-offs when business-hour VMs turn off.")
    else:
        print("• [OPTIMAL STATE]: Savings Plan baseline is well-matched against current active baseline run-rates.")
    print("=" * 115)


if __name__ == "__main__":
    print("=" * 115)
    print("STEP 1: CLOUD ENVIRONMENT INVENTORY SCAN (USD BENCHMARKS)")
    print("=" * 115)
    env_inventory = scan_cloud_environment()
    print(env_inventory.to_string(index=False))
    
    print("\\n" + "=" * 115)
    print("STEP 2: PROCESSING GRANULAR HOURLY WATERFALL BILLING MATRIX")
    print("=" * 115)
    commitments_pool = fetch_existing_commitments()
    detailed_bill, efficiency_summary = process_finops_waterfall(env_inventory, commitments_pool)
    
    print(">>> MID-DAY SNAPSHOT SAMPLE (RECONCILED HOUR 12:00 LOOKUP):")
    print(detailed_bill.head(6).to_string(index=False))
    
    print("\\n" + "=" * 115)
    print("STEP 3: EXECUTIVE COMMITMENT METRICS SUMMARY")
    print("=" * 115)
    print(efficiency_summary.to_string(index=False))
    
    # Trigger final evaluation reporting layer
    generate_finops_recommendations(efficiency_summary, detailed_bill)

```

---

## 6. Enterprise Production Deployment Blueprint

To transition this proof-of-concept into a live Azure Function (`function_app.py`), complete the following two operational migrations:

1. **Migrate `scan_cloud_environment()`:** Remove the hardcoded rows array and implement the `azure-mgmt-resourcegraph` library. Utilize structured KQL queries targeting the `resources` table to dynamically retrieve `properties.extended.instanceView.powerState.code` alongside core SKU size matrices across corporate subscriptions.
2. **Migrate `fetch_existing_commitments()`:** Substitute the simulated mock data frames with live downstream API requests routed directly through the Azure Consumption API and the `azure-mgmt-billing` SDK, allowing real-time retrieval of active contract profile pools.
"""

# Write documentation out to disk

with open("finops_system_documentation.md", "w") as f:
f.write(markdown_content)

print("FILE_CREATED_SUCCESSFULLY")

```
Your Markdown file is ready
[file-tag: code-generated-file-0-1785774462022338310]

***

### 📘 Documentation Overview: What's Included

The generated `finops_system_documentation.md` file contains a complete technical blueprint of the app. It includes:

1. **Executive Summary & Core Objective:** Highlighting the business justification for parsing data at an hourly grain, removing nested JSON wrappers, and handling the business-hours shutdown anomaly.
2. **Functional Architecture & Logic Layers:** A structural layout mapping out how the multi-pass priority queue operates (Pass 1 for rigid Reserved Instances and Pass 2 for flexible Savings Plans) along with the buffer logic.
3. **Detailed Schema Definitions:** Clean Markdown tables displaying schemas for the Input Environment Scan, the Commitments Inventory, and the final Reconciled Granular Hourly Log Snapshot.
4. **Mathematical Models & Equations:** Fully rendered LaTeX equations tracking how the out-of-pocket costs are extracted row-by-row and how the $80\%$ conservative safety buffer targets are formulated.
5. **Complete Production-Grade Python Source Code:** The full code implementation containing the corrected nested loops, priority scanning, running hours window matching, and recommendation generation.
6. **Enterprise Production Deployment Blueprint:** Actionable architecture steps for swapping out your mock arrays for the live `azure-mgmt-resourcegraph` and `azure-mgmt-billing` SDK queries within `function_app.py`.

```