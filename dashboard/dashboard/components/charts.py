"""
components/charts.py — Chart builders (Altair, bundled with Streamlit).

House style: thin marks, one hue for a single series, 4px rounded data-end
anchored to a zero baseline, hairline recessive grid, a tooltip on every mark,
and no legend for a single series (the title names it).
"""

from __future__ import annotations

import altair as alt
import pandas as pd

SERIES_1 = "#2a78d6"      # single-series hue (categorical slot 1, blue)
GRID = "#e6e5e1"          # one step off the chart surface
TEXT_SECONDARY = "#52514e"


def daily_counts_bar(df: pd.DataFrame, value_title: str) -> alt.Chart:
    """Columns per day. `df` has columns `date` (datetime) and `count` (int)."""
    max_count = int(df["count"].max()) if len(df) else 0

    base = alt.Chart(df).encode(
        x=alt.X(
            "yearmonthdate(date):O",
            title=None,
            axis=alt.Axis(format="%d %b", labelAngle=0, labelOverlap=True, ticks=False,
                          domainColor=GRID, labelColor=TEXT_SECONDARY),
        ),
        y=alt.Y(
            "count:Q",
            title=None,
            scale=alt.Scale(domainMin=0, nice=True, domainMax=max(max_count, 4)),
            axis=alt.Axis(format="d", tickMinStep=1, gridColor=GRID, gridWidth=1,
                          domain=False, ticks=False, labelColor=TEXT_SECONDARY),
        ),
        tooltip=[
            alt.Tooltip("date:T", title="Date", format="%a, %d %b %Y"),
            alt.Tooltip("count:Q", title=value_title, format=","),
        ],
    )
    bars = base.mark_bar(
        color=SERIES_1,
        size=14,  # thin column, never fills its slot (<= 24px)
        cornerRadiusTopLeft=4,
        cornerRadiusTopRight=4,
    )
    return (
        bars.properties(height=260)
        .configure_view(strokeWidth=0)
        .configure_axis(labelFontSize=12)
    )
