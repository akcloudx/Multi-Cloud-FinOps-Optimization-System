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


def get_cost_distribution_chart(df: pd.DataFrame, provider: str = "Azure",
                                 is_dark: bool = True) -> go.Figure:
    """Donut chart showing monthly spend distribution by resource type."""
    if df.empty:
        fig = go.Figure()
        fig.update_layout(title="No data available", **_layout(is_dark))
        return fig

    colors = _DARK_CATEGORICAL if is_dark else _LIGHT_CATEGORICAL
    primary = colors[0] if provider == "Azure" else AWS_PRIMARY

    df_grouped = df.groupby("Resource Type")["PAYG Hourly Cost USD"].sum().reset_index()
    df_grouped["Monthly Spend USD"] = df_grouped["PAYG Hourly Cost USD"] * 730

    fig = px.pie(
        df_grouped,
        values="Monthly Spend USD",
        names="Resource Type",
        hole=0.48,
        title=f"Monthly spend distribution by domain ({provider})",
        color_discrete_sequence=colors,
    )

    line_color = "#0F172A" if is_dark else "#FFFFFF"
    fig.update_traces(
        # No in-slice text at all - AWS now has ~20 resource types after
        # this session's additions (SageMaker/DocumentDB/Neptune/DMS
        # Serverless, Neptune Analytics, ...), and that's too many for a
        # donut ring at any text size. Tried "percent+label" then
        # "percent"-only first (both still overlapped, reported by the
        # user after resizing the window): Plotly's per-slice text fitting
        # (and even uniformtext_mode="hide") only checks whether text fits
        # WITHIN its own slice, not whether it collides with the adjacent
        # slice's text - with ~20 thin slices packed around a thin ring,
        # neighboring labels visually overlap regardless of font size, so
        # there's no text-sizing fix that scales to this many categories.
        # Full label + percent + value is still available on hover, and
        # every category is already listed in the legend below - dropping
        # in-slice text is the standard, category-count-proof fix for a
        # many-category pie/donut (matches Plotly's own guidance to prefer
        # hover/legend over in-slice text once you're much past ~8-10
        # slices).
        textposition="none",
        hovertemplate="<b>%{label}</b><br>%{percent}<br>$%{value:,.2f}/mo<extra></extra>",
        marker=dict(line=dict(color=line_color, width=1.5)),
    )

    fig.update_layout(
        **_layout(is_dark, margin=dict(l=10, r=10, t=45, b=60)),
        legend=dict(orientation="h", yanchor="top", y=-0.08,
                    xanchor="center", x=0.5, font=dict(size=11)),
    )
    return fig


def get_waterfall_savings_chart(vm_payg_hr: float, db_payg_hr: float,
                                 sp_commit_hr: float, ri_commit_hr: float,
                                 provider: str = "Azure",
                                 is_dark: bool = True) -> go.Figure:
    """Bar chart comparing baseline PAYG spend vs active commitments."""
    colors = _DARK_CATEGORICAL if is_dark else _LIGHT_CATEGORICAL
    primary = colors[0] if provider == "Azure" else AWS_PRIMARY

    total_baseline_hr  = vm_payg_hr + db_payg_hr
    total_commit_hr    = sp_commit_hr + ri_commit_hr
    uncovered_payg_hr  = max(0.0, total_baseline_hr - total_commit_hr)

    categories = ["Total PAYG Baseline", "RI Covered", "SP Pool Committed", "Net Uncovered PAYG"]
    values     = [total_baseline_hr * 730, ri_commit_hr * 730,
                  sp_commit_hr * 730, uncovered_payg_hr * 730]
    bar_colors = [primary, ACCENT_GREEN, colors[5], ACCENT_RED]

    line_color = "#0F172A" if is_dark else "#FFFFFF"
    fig = go.Figure(go.Bar(
        x=categories,
        y=values,
        text=[f"${v:,.2f}" for v in values],
        textposition="auto",
        marker_color=bar_colors,
        marker=dict(line=dict(color=line_color, width=1.5)),
    ))

    fig.update_layout(
        title=f"Monthly financial baseline vs commitment coverage ({provider})",
        yaxis_title="Est. monthly cost (USD $)",
        **_layout(is_dark, margin=dict(l=20, r=20, t=50, b=20)),
    )
    return fig


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
