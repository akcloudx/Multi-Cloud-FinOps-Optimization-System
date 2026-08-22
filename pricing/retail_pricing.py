"""
pricing/retail_pricing.py
Currency formatting + live USD→INR exchange rate - the two genuinely
still-live pieces of what was originally a broader "PAYG pricing lookup"
module.

Real Azure/AWS PAYG rates now come from pricing/azure_retail_api.py and
pricing/aws_price_list.py (live Retail Prices / Price List Query API calls),
not this file - this module's original mock SKU_HOURLY_PAYG rate table and
its price_inventory()/get_payg_rate() consumers were removed 2026-08-23
after confirming (via repo-wide grep) neither had any live caller left -
app.py imported price_inventory but never called it, and get_payg_rate had
zero callers anywhere. Left in place as dead code alongside the real
pricing engine, it read as ambiguous - as if it might still be a pricing
source of truth - which it never was for anything shipped after the real
API integrations landed.
"""

import pandas as pd
import streamlit as st

CURRENCY = "USD"
SYMBOL = "$"


# ── Live Exchange Rate (USD → INR) ─────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)   # Cache 1 hour — re-fetches automatically
def get_inr_rate() -> float:
    """Fetch live USD → INR exchange rate from frankfurter.app (free, no auth).
    Falls back to a hardcoded rate if the API is unavailable.

    Real bug found live 2026-08-21: this was silently falling back to
    _FALLBACK_RATE on every single call (not just when the API was
    genuinely down) because frankfurter.app returns 403 Forbidden for
    requests with no User-Agent header - urllib.request.urlopen() sends
    none by default. Confirmed directly: the exact same URL succeeds
    instantly with a User-Agent header set, fails every time without one.
    Every INR-displayed cost app-wide (Azure and AWS both, not specific to
    either) had been silently using the stale 84.0 fallback instead of the
    real live rate (~95.7 at the time this was caught) for however long
    this function has existed - caught only because the AWS pricing round
    happened to produce a number precise enough for the user to notice the
    conversion didn't match a real exchange rate lookup.
    """
    _FALLBACK_RATE = 84.0
    try:
        import urllib.request, json
        req = urllib.request.Request(
            "https://api.frankfurter.app/latest?from=USD&to=INR",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
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
