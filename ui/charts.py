"""
ui/charts.py — Plotly Financial Visualizations for Multi-Cloud FinOps Dashboard
Adapts automatically to active Streamlit theme (light / dark).
"""

import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

# Provider colours
AZURE_PRIMARY = "#60A5FA"   # soft blue — readable on both themes
AWS_PRIMARY   = "#FBBF24"   # amber
ACCENT_GREEN  = "#34D399"
ACCENT_RED    = "#F87171"
ACCENT_ORANGE = "#FB923C"

# Palette per theme
_DARK_TEXT  = "#F1F5F9"
_LIGHT_TEXT = "#111827"

_DARK_CATEGORICAL  = ["#60A5FA","#34D399","#A78BFA","#F87171","#FBBF24","#38BDF8","#94A3B8","#FB923C"]
_LIGHT_CATEGORICAL = ["#2563EB","#059669","#DC2626","#D97706","#7C3AED","#0891B2","#6B7280","#EA580C"]


def _layout(is_dark: bool, **extra) -> dict:
    """Shared Plotly layout defaults that adapt to the active theme."""
    template   = "plotly_dark" if is_dark else "plotly_white"
    font_color = _DARK_TEXT if is_dark else _LIGHT_TEXT
    return dict(
        template=template,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=font_color, family="Inter, sans-serif", size=13),
        **extra,
    )


def get_recommendation_opportunity_chart(recs: list,
                                          is_dark: bool = True) -> go.Figure:
    """Horizontal bar chart showing potential monthly savings by issue
    CATEGORY (e.g. "Orphaned Capacity", "RI Purchase Gap") - one bar per
    category, not one per underlying recommendation card, so a tenant with a
    dozen orphaned resources still reads as one clean bar instead of a wall
    of per-resource labels."""
    if not recs or (len(recs) == 1 and recs[0].get("type") == "OPTIMAL"):
        fig = go.Figure()
        fig.update_layout(title="No optimization opportunities - portfolio is well-matched", **_layout(is_dark))
        return fig

    colors = _DARK_CATEGORICAL if is_dark else _LIGHT_CATEGORICAL
    high_c = ACCENT_RED
    med_c  = ACCENT_ORANGE
    low_c  = ACCENT_GREEN
    ok_c   = colors[0]

    rec_data = [
        {
            "Category": r.get("category") or r.get("title", "Recommendation"),
            "Severity": r.get("severity", "LOW"),
            "Monthly Impact USD": r.get("financial_impact_hr", 0.0) * 730,
        }
        for r in recs
        if r.get("type") != "OPTIMAL"
    ]
    if not rec_data:
        fig = go.Figure()
        fig.update_layout(title="No optimization opportunities - portfolio is well-matched", **_layout(is_dark))
        return fig

    df_rec = pd.DataFrame(rec_data).sort_values("Monthly Impact USD", ascending=True)

    fig = px.bar(
        df_rec,
        x="Monthly Impact USD",
        y="Category",
        orientation="h",
        color="Severity",
        title="Estimated monthly savings opportunity by issue category",
        color_discrete_map={"HIGH": high_c, "MEDIUM": med_c, "LOW": low_c, "OK": ok_c},
    )

    fig.update_layout(
        xaxis_title="Potential monthly savings ($ USD)",
        yaxis_title=None,
        **_layout(is_dark, margin=dict(l=20, r=20, t=50, b=20)),
    )
    return fig
