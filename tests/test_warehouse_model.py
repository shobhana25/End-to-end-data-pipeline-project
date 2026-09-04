"""Tests of the dimensional model itself, against a warehouse built from the
committed fixtures.

These are the tests that would catch a modelling regression: a fanned-out join,
a lost row, an aggregation that ignores stock/flow semantics.
"""

from __future__ import annotations

import pytest


def test_every_star_object_exists(query):
    built = {row[0] for row in query("SELECT table_name FROM duckdb_tables()")}
    assert {
        "dim_date",
        "dim_location",
        "dim_indicator",
        "fct_hospital_activity",
        "fct_population_annual",
        "mart_monthly_activity",
        "mart_peak_pressure",
        "mart_location_coverage",
        "mart_rate_reconciliation",
    } <= built


def test_fact_row_count_is_exactly_half_the_staged_rows(scalar):
    """The two unit variants of each measure are pivoted onto one fact row, so
    the fact must be exactly half the staged long-format feed. A different
    number means the pivot fanned out or dropped rows."""
    staged = scalar("SELECT count(*) FROM stg_hospital_activity")
    facts = scalar("SELECT count(*) FROM fct_hospital_activity")
    assert facts * 2 == staged


def test_every_dimension_carries_an_unknown_member(scalar):
    """Unknown members are what let an unmatched fact be counted rather than
    silently dropped by an inner join."""
    for table, key in (
        ("dim_date", "date_key"),
        ("dim_location", "location_key"),
        ("dim_indicator", "indicator_key"),
    ):
        assert scalar(f"SELECT count(*) FROM {table} WHERE {key} = -1") == 1


def test_no_fact_row_falls_through_to_an_unknown_member(scalar):
    assert (
        scalar(
            "SELECT count(*) FROM fct_hospital_activity "
            "WHERE date_key = -1 OR location_key = -1 OR indicator_key = -1"
        )
        == 0
    )


def test_joining_the_full_star_neither_loses_nor_duplicates_rows(scalar):
    """The canonical fan-out check: the star joined through all three
    dimensions must return exactly the fact's own row count."""
    facts = scalar("SELECT count(*) FROM fct_hospital_activity")
    joined = scalar(
        """
        SELECT count(*)
        FROM fct_hospital_activity AS f
        JOIN dim_date      AS d ON d.date_key      = f.date_key
        JOIN dim_location  AS l ON l.location_key  = f.location_key
        JOIN dim_indicator AS i ON i.indicator_key = f.indicator_key
        """
    )
    assert joined == facts


def test_non_iso_publisher_codes_are_still_classified(query):
    """Our World in Data reports the UK nations under OWID_* pseudo-codes. The
    override config is what keeps them in the geography hierarchy."""
    rows = query(
        """
        SELECT location_name, location_type, region, has_population_data
        FROM dim_location WHERE source_location_code = 'OWID_ENG'
        """
    )
    assert rows, "OWID_ENG missing from dim_location"
    name, location_type, region, has_population = rows[0]
    assert (name, location_type, region) == ("England", "Sub-national", "Europe")
    assert has_population is False


def test_world_bank_aggregates_never_reach_the_population_fact(scalar):
    """'World', 'OECD members' and the income groups share a schema with real
    countries. Letting one through would double-count a population."""
    assert (
        scalar(
            """
        SELECT count(*) FROM fct_population_annual AS p
        JOIN dim_location AS l ON l.location_key = p.location_key
        WHERE l.source_location_code IN ('WLD', 'OED', 'HIC')
        """
        )
        == 0
    )
    assert scalar("SELECT count(*) FROM raw_population WHERE is_aggregate") > 0


def test_population_is_carried_onto_the_fact_for_iso_countries(scalar):
    assert (
        scalar(
            """
        SELECT count(*) FROM fct_hospital_activity AS f
        JOIN dim_location AS l ON l.location_key = f.location_key
        WHERE l.location_type = 'Country' AND f.population IS NULL
        """
        )
        == 0
    )


def test_locations_without_population_get_null_rates_not_guesses(scalar):
    assert (
        scalar(
            """
        SELECT count(*) FROM fct_hospital_activity AS f
        JOIN dim_location AS l ON l.location_key = f.location_key
        WHERE l.has_population_data = FALSE AND f.derived_per_100k IS NOT NULL
        """
        )
        == 0
    )


def test_derived_rate_matches_a_hand_calculation(query):
    """Recompute one row by hand and check the model agrees."""
    rows = query(
        """
        SELECT reported_value, population, derived_per_100k, derived_per_million
        FROM fct_hospital_activity
        WHERE population IS NOT NULL AND reported_value > 0
        ORDER BY event_date, location_key, indicator_key LIMIT 1
        """
    )
    value, population, per_100k, per_million = rows[0]
    assert per_100k == pytest.approx(value / (population / 1e5))
    assert per_million == pytest.approx(value / (population / 1e6))
    assert per_million == pytest.approx(per_100k * 10)


def test_stock_measures_are_averaged_and_flow_measures_summed(query):
    """dim_indicator.is_additive_over_time is not decoration - the monthly mart
    must actually honour it."""
    rows = query(
        """
        SELECT i.is_additive_over_time, m.monthly_value, m.avg_value, m.total_value
        FROM mart_monthly_activity AS m
        JOIN dim_indicator AS i ON i.indicator_key = m.indicator_key
        """
    )
    assert rows
    for additive, monthly, average, total in rows:
        if additive:
            assert monthly == pytest.approx(total)
        else:
            assert monthly == pytest.approx(average)
            assert total is None


def test_occupancy_is_never_offered_as_a_monthly_total(scalar):
    assert (
        scalar(
            """
        SELECT count(*) FROM mart_monthly_activity
        WHERE is_additive_over_time = FALSE AND total_value IS NOT NULL
        """
        )
        == 0
    )


def test_peak_mart_agrees_with_the_fact_it_summarises(query):
    for location, indicator, peak in query(
        "SELECT location_key, indicator_key, peak_value FROM mart_peak_pressure LIMIT 5"
    ):
        actual = query(
            "SELECT max(reported_value) FROM fct_hospital_activity "
            "WHERE location_key = ? AND indicator_key = ?",
            [location, indicator],
        )[0][0]
        assert peak == pytest.approx(actual)


def test_date_dimension_is_gapless_and_spans_the_facts(scalar):
    assert (
        scalar(
            """
        SELECT count(*) FROM (
            SELECT full_date, lead(full_date) OVER (ORDER BY full_date) AS next_date
            FROM dim_date WHERE date_key <> -1
        ) WHERE next_date IS NOT NULL AND date_diff('day', full_date, next_date) <> 1
        """
        )
        == 0
    )
    assert (
        scalar(
            """
        SELECT count(*) FROM fct_hospital_activity AS f
        LEFT JOIN dim_date AS d ON d.date_key = f.date_key
        WHERE d.date_key IS NULL
        """
        )
        == 0
    )


def test_the_build_is_idempotent(build_runs):
    """Rebuilding must reproduce the same warehouse, not append to it. Every
    model is CREATE OR REPLACE precisely so a rerun is safe."""
    first, second = build_runs
    assert [(m.layer, m.name, m.rows) for m in first] == [(m.layer, m.name, m.rows) for m in second]


def test_cyprus_denominator_mismatch_is_detected(query):
    """The fixture keeps Cyprus precisely because it is the case the
    reconciliation control exists to catch."""
    rows = query(
        """
        SELECT DISTINCT denominator_agreement FROM mart_rate_reconciliation
        WHERE source_location_code = 'CYP'
        """
    )
    assert rows and all(row[0] == "Definitional mismatch" for row in rows)
