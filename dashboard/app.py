"""Interactive exploration of the health capacity warehouse.

Run with::

    streamlit run dashboard/app.py

This is the exploratory counterpart to the static page the pipeline publishes
(``docs/index.html``). Both read the same DuckDB warehouse and the same
reporting marts - the static page is the fixed narrative, this is the place to
pull a thread.

The connection is opened read-only and cached, so the app never blocks a
pipeline run that is rebuilding the warehouse alongside it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipelines.config import paths

st.set_page_config(
    page_title="Hospital Capacity Warehouse",
    page_icon="🏥",
    layout="wide",
)

SERIES_BLUE = "#2a78d6"
SERIES_ORANGE = "#eb6834"
NEUTRAL = "#c3c2b7"


@st.cache_resource
def get_connection() -> duckdb.DuckDBPyConnection:
    database = paths().database
    if not database.exists():
        st.error(
            f"No warehouse found at `{database}`.\n\nBuild it first:  `python -m pipelines.cli all`"
        )
        st.stop()
    return duckdb.connect(str(database), read_only=True)


@st.cache_data(ttl=300)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    return get_connection().execute(sql, list(params)).df()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

st.title("Hospital and intensive care capacity")
st.caption(
    "A star-schema warehouse built from Our World in Data hospital activity, "
    "World Bank population and ISO 3166 reference data."
)

indicators = query(
    """
    SELECT indicator_code, indicator_name, measure_semantics, default_time_aggregation
    FROM dim_indicator WHERE indicator_key <> -1 ORDER BY indicator_name
    """
)
locations = query(
    """
    SELECT DISTINCT location_name, source_location_code, region
    FROM mart_peak_pressure ORDER BY location_name
    """
)

with st.sidebar:
    st.header("Filters")
    indicator_name = st.selectbox("Measure", indicators["indicator_name"], index=0)
    indicator = indicators.loc[indicators["indicator_name"] == indicator_name].iloc[0]

    default = [
        c for c in ("Australia", "France", "Czechia") if c in set(locations["location_name"])
    ]
    chosen = st.multiselect(
        "Locations",
        locations["location_name"],
        default=default or list(locations["location_name"][:3]),
    )
    normalise = st.radio(
        "Scale",
        ["Per 100,000 people", "Absolute count"],
        help="Population-adjusted is the comparable scale; absolute counts "
        "mostly reflect how big a country is.",
    )
    st.divider()
    st.caption(
        f"**{indicator['indicator_name']}** is a "
        f"{indicator['measure_semantics'].lower()} measure, so it is aggregated "
        f"over time with `{indicator['default_time_aggregation']}()`."
    )

if not chosen:
    st.info("Pick at least one location in the sidebar.")
    st.stop()

codes = tuple(locations.loc[locations["location_name"].isin(chosen), "source_location_code"])
placeholders = ", ".join("?" for _ in codes)
value_column = "monthly_per_100k" if normalise.startswith("Per") else "monthly_value"
unit = "per 100k" if normalise.startswith("Per") else "beds / admissions"

# ---------------------------------------------------------------------------
# Headline figures
# ---------------------------------------------------------------------------

peaks = query(
    f"""
    SELECT location_name, peak_value, peak_value_date, peak_per_100k,
           peak_per_100k_date, days_observed, first_observed_date, last_observed_date
    FROM mart_peak_pressure
    WHERE indicator_code = ? AND source_location_code IN ({placeholders})
    ORDER BY peak_per_100k DESC
    """,
    (indicator["indicator_code"], *codes),
)

if peaks.empty:
    st.warning("No observations for that combination of measure and locations.")
    st.stop()

columns = st.columns(min(len(peaks), 4))
# columns are capped at 4, so the two sequences are intentionally unequal
for column, (_, row) in zip(columns, peaks.iterrows(), strict=False):
    column.metric(
        row["location_name"],
        f"{row['peak_per_100k']:.2f}" if pd.notna(row["peak_per_100k"]) else "-",
        help=f"Peak per 100k on {row['peak_per_100k_date']:%d %b %Y}",
    )
    column.caption(f"{row['days_observed']:,} days reported")

# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------

st.subheader("Monthly trend")
monthly = query(
    f"""
    SELECT month_start_date, location_name, {value_column} AS value, days_observed
    FROM mart_monthly_activity
    WHERE indicator_code = ? AND source_location_code IN ({placeholders})
    ORDER BY month_start_date
    """,
    (indicator["indicator_code"], *codes),
)

chart = (
    alt.Chart(monthly)
    .mark_line(strokeWidth=2, point=False)
    .encode(
        x=alt.X("month_start_date:T", title=None),
        y=alt.Y("value:Q", title=f"{indicator['indicator_name']} ({unit})"),
        color=alt.Color("location_name:N", title="Location"),
        tooltip=[
            alt.Tooltip("month_start_date:T", title="Month", format="%b %Y"),
            alt.Tooltip("location_name:N", title="Location"),
            alt.Tooltip("value:Q", title=unit, format=",.2f"),
            alt.Tooltip("days_observed:Q", title="Days reported"),
        ],
    )
    .properties(height=340)
    .interactive()
)
st.altair_chart(chart, use_container_width=True)

# ---------------------------------------------------------------------------
# Cross-country ranking
# ---------------------------------------------------------------------------

st.subheader("Peak pressure, all reporting locations")
ranking = query(
    """
    SELECT location_name, region, peak_per_100k, peak_per_100k_date, days_observed
    FROM mart_peak_pressure
    WHERE indicator_code = ? AND peak_per_100k IS NOT NULL
    ORDER BY peak_per_100k DESC
    """,
    (indicator["indicator_code"],),
)
ranking["selected"] = ranking["location_name"].isin(chosen)
st.altair_chart(
    alt.Chart(ranking)
    .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
    .encode(
        x=alt.X("peak_per_100k:Q", title="Peak per 100,000 people"),
        y=alt.Y("location_name:N", sort="-x", title=None),
        color=alt.condition(alt.datum.selected, alt.value(SERIES_BLUE), alt.value(NEUTRAL)),
        tooltip=["location_name", "region", "peak_per_100k", "peak_per_100k_date", "days_observed"],
    )
    .properties(height=max(320, 18 * len(ranking))),
    use_container_width=True,
)

# ---------------------------------------------------------------------------
# Assurance
# ---------------------------------------------------------------------------

with st.expander("Assurance · rate reconciliation and data quality"):
    st.markdown(
        "The publisher supplies a population-adjusted rate; the warehouse "
        "recomputes it from World Bank population and keeps the difference as "
        "a measure. Locations flagged below use a denominator that does not "
        "match the one behind the reported counts."
    )
    st.dataframe(
        query(
            """
            SELECT location_name, indicator_name, denominator_agreement,
                   publisher_implied_population, reference_population,
                   round(median_variance_pct, 2) AS median_variance_pct, rows_compared
            FROM mart_rate_reconciliation
            WHERE denominator_agreement <> 'Aligned'
            ORDER BY abs(median_variance_pct) DESC
            """
        ),
        use_container_width=True,
        hide_index=True,
    )
    results = query(
        """
        SELECT test_name, model, test_type, severity, status, failing_rows
        FROM meta_quality_results
        WHERE run_id = (SELECT run_id FROM meta_quality_results
                        ORDER BY executed_at DESC, rowid DESC LIMIT 1)
        ORDER BY CASE status WHEN 'pass' THEN 1 ELSE 0 END, test_name
        """
    )
    passed = int((results["status"] == "pass").sum())
    st.metric("Quality tests passed", f"{passed}/{len(results)}")
    st.dataframe(results, use_container_width=True, hide_index=True)

with st.expander("Coverage · how complete is each series?"):
    st.dataframe(
        query(
            """
            SELECT location_name, indicator_name, first_observed_date, last_observed_date,
                   days_reported, reporting_completeness_pct, coverage_grade
            FROM mart_location_coverage
            WHERE indicator_code = ?
            ORDER BY reporting_completeness_pct DESC
            """,
            (indicator["indicator_code"],),
        ),
        use_container_width=True,
        hide_index=True,
    )
