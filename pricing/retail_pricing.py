"""
pricing/retail_pricing.py
PAYG (Pay-As-You-Go) pricing lookup — all rates in USD.

REAL AZURE EQUIVALENT (when you move off mock data):
  Azure Retail Prices API — public REST endpoint, no auth required:
    GET https://prices.azure.com/api/retail/prices?$filter=
        armRegionName eq 'australiaeast' and
        armSkuName eq 'Standard_D4ds_v5' and
        priceType eq 'Consumption' and
        currencyCode eq 'USD'

  Filter on the correct meter for the OS:
    Windows SKUs carry a licensing surcharge over the base Linux compute meter
    for the same VM size. Always match on both armSkuName and meterName.

All prices benchmarked in USD — matches Azure Pricing Calculator default.
"""

import pandas as pd
import streamlit as st

CURRENCY = "USD"
SYMBOL = "$"
HOURS_PER_MONTH = 730  # Azure standard averaging convention: 365 × 24 / 12


# ── Mock PAYG Hourly Rates (USD) ───────────────────────────────────────────────
# Key: (SKU, OS) → hourly PAYG rate in USD
# Replace with live Azure Retail Prices API calls for production.

SKU_HOURLY_PAYG: dict[tuple[str, str], float] = {
    # D-series v5 — Production VMs (Windows carries ~48% licensing surcharge vs Linux)
    ("Standard_D4ds_v5", "Windows"): 0.284,
    ("Standard_D4ds_v5", "Linux"):   0.192,

    # D-series v4 — Legacy Production VMs
    ("Standard_D4ds_v4", "Windows"): 0.284,
    ("Standard_D4ds_v4", "Linux"):   0.192,

    # D-series v5 standard (non-ds)
    ("Standard_D4s_v5",  "Windows"): 0.302,
    ("Standard_D4s_v5",  "Linux"):   0.192,

    # B-series — Dev / burstable (business-hours only)
    ("Standard_B2ms",    "Windows"): 0.106,
    ("Standard_B2ms",    "Linux"):   0.052,
    ("Standard_B2s",     "Windows"): 0.053,
    ("Standard_B2s",     "Linux"):   0.025,

    # E-series v5 — Memory-optimised (Database workloads)
    ("Standard_E4s_v5",  "Windows"): 0.400,
    ("Standard_E4s_v5",  "Linux"):   0.300,

    # Azure SQL Database — General Purpose Gen5, 4 vCores
    ("GP_Gen5_4",        "N/A"):     0.526,
}


# ── Live Exchange Rate (USD → INR) ─────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)   # Cache 1 hour — re-fetches automatically
def get_inr_rate() -> float:
    """Fetch live USD → INR exchange rate from frankfurter.app (free, no auth).
    Falls back to a hardcoded rate if the API is unavailable.
    """
    _FALLBACK_RATE = 84.0
    try:
        import urllib.request, json
        with urllib.request.urlopen(
            "https://api.frankfurter.app/latest?from=USD&to=INR", timeout=4
        ) as resp:
            data = json.loads(resp.read())
            return float(data["rates"]["INR"])
    except Exception:
        return _FALLBACK_RATE


def fmt_currency(x: float, decimals: int = 2,
                 currency: str = "USD", inr_rate: float = 84.0) -> str:
    """
    Format a float as a currency string.
      currency="USD"  →  '$1,234.56'
      currency="INR"  →  '₹1,03,456.78'
    """
    if pd.isna(x):
        return "N/A"
    if currency == "INR":
        return f"\u20b9{x * inr_rate:,.{decimals}f}"
    return f"${x:,.{decimals}f}"


def usd(x: float, decimals: int = 4) -> str:
    """
    Format a float as a USD currency string (kept for backward compatibility).
    Example: usd(0.284, 4) → '$0.2840'
    """
    if pd.isna(x):
        return "N/A"
    return f"${x:,.{decimals}f}"


def get_payg_rate(sku: str, os_: str) -> float | None:
    """Look up the PAYG hourly rate for a given SKU and OS combination."""
    return SKU_HOURLY_PAYG.get((sku, os_), None)


def price_inventory(df: pd.DataFrame) -> pd.DataFrame:
    """
    Enriches the inventory DataFrame with PAYG pricing columns.
    Adds:
      - 'PAYG Hourly Cost USD' — already in inventory but confirmed here
      - 'Sku Cost USD 730hrs'  — monthly estimate at full PAYG rate
      - 'Does SKU need to be resized' — placeholder for Azure Monitor CPU% logic
    """
    df = df.copy()

    # Confirm/override from pricing table (single source of truth)
    df["PAYG Hourly Cost USD"] = df.apply(
        lambda r: SKU_HOURLY_PAYG.get((r["SKU"], r["OS"]), r["PAYG Hourly Cost USD"]),
        axis=1
    )
    df["Sku Cost USD 730hrs"] = df["PAYG Hourly Cost USD"] * HOURS_PER_MONTH

    # Placeholder right-sizing flag — swap in Azure Monitor avg CPU% over 30 days:
    # Flag "Yes" if avg CPU < 20% over the past 30 days as a downsize candidate.
    df["Does SKU need to be resized"] = "No"

    return df
