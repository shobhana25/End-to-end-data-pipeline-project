"""The quality engine, tested the only way that means anything: by breaking
things and checking the right assertion notices.

A suite that has never been seen to fail is not evidence of anything, so each
test here injects one specific defect into a scratch copy of the warehouse and
asserts that the matching assertion catches it - and, just as importantly, that
the unrelated assertions stay green.
"""

from __future__ import annotations

import duckdb
import pytest

from pipelines.quality import QualityConfigError, compile_test, load_tests


@pytest.fixture(scope="module")
def suite():
    return {test["name"]: test for test in load_tests()}


def _failing_rows(connection, test) -> int:
    return connection.execute(
        f"SELECT count(*) FROM ({compile_test(test)}) AS failures"
    ).fetchone()[0]


@pytest.fixture
def scratch(tmp_path, build_runs, pipeline_modules):
    """A writable copy of the built warehouse, so defects can be injected."""
    import shutil

    source = pipeline_modules["config"].paths().database
    target = tmp_path / "scratch.duckdb"
    shutil.copyfile(source, target)
    connection = duckdb.connect(str(target))
    yield connection
    connection.close()


def test_the_clean_warehouse_passes_every_applicable_assertion(warehouse):
    for test in load_tests(include_volume_dependent=False):
        assert _failing_rows(warehouse, test) == 0, f"{test['name']} failed on clean data"


@pytest.mark.parametrize(
    "defect,expected_test",
    [
        (
            "INSERT INTO dim_location SELECT * FROM dim_location LIMIT 1",
            "dim_location_key_is_unique",
        ),
        (
            "UPDATE fct_hospital_activity SET reported_value = -1 WHERE rowid = 5",
            "activity_values_are_not_negative",
        ),
        (
            "UPDATE fct_hospital_activity SET location_key = 987654 WHERE rowid = 7",
            "fct_activity_location_key_resolves",
        ),
        (
            "UPDATE fct_hospital_activity SET location_key = -1 WHERE rowid = 9",
            "fct_activity_has_no_unknown_members",
        ),
        (
            "DELETE FROM dim_date WHERE date_key = 20210301",
            "dim_date_is_gapless",
        ),
        (
            "UPDATE dim_indicator SET measure_semantics = 'Neither' WHERE indicator_key = 1",
            "dim_indicator_semantics_are_known",
        ),
        (
            "UPDATE dim_location SET location_type = 'Planet' WHERE location_key = 1",
            "dim_location_types_are_known",
        ),
        (
            # target a row with a non-zero rate: doubling zero is still zero
            "UPDATE fct_hospital_activity SET reported_per_100k = reported_per_100k * 2 "
            "WHERE rowid = (SELECT min(rowid) FROM fct_hospital_activity "
            "               WHERE reported_per_million > 0)",
            "per_100k_is_consistent_with_per_million",
        ),
        (
            "UPDATE fct_hospital_activity SET event_date = DATE '2099-01-01' WHERE rowid = 13",
            "event_dates_are_plausible",
        ),
        (
            "INSERT INTO fct_population_annual SELECT * FROM fct_population_annual LIMIT 1",
            "fct_population_grain_is_unique",
        ),
    ],
)
def test_each_assertion_catches_its_own_defect(scratch, suite, defect, expected_test):
    assert _failing_rows(scratch, suite[expected_test]) == 0, "defect present before injection"
    scratch.execute(defect)
    assert _failing_rows(scratch, suite[expected_test]) > 0, (
        f"{expected_test} did not notice: {defect}"
    )


def test_a_defect_does_not_trip_unrelated_assertions(scratch, suite):
    """False positives cost as much trust as false negatives."""
    scratch.execute("UPDATE fct_hospital_activity SET reported_value = -1 WHERE rowid = 5")
    for name in (
        "dim_location_key_is_unique",
        "dim_date_is_gapless",
        "dim_indicator_semantics_are_known",
        "fct_activity_grain_is_unique",
    ):
        assert _failing_rows(scratch, suite[name]) == 0, f"{name} false-positived"


def test_grain_assertion_catches_a_duplicated_fact_row(scratch, suite):
    """The declared grain is the model's central promise."""
    scratch.execute("INSERT INTO fct_hospital_activity SELECT * FROM fct_hospital_activity LIMIT 1")
    assert _failing_rows(scratch, suite["fct_activity_grain_is_unique"]) > 0


# -- compiler -------------------------------------------------------------


def test_every_declared_test_compiles():
    for test in load_tests():
        assert compile_test(test).strip()


def test_unknown_test_type_is_rejected():
    with pytest.raises(QualityConfigError, match="unknown test type"):
        compile_test({"name": "x", "type": "vibes", "model": "dim_date"})


def test_row_count_without_bounds_is_rejected():
    with pytest.raises(QualityConfigError, match="needs min"):
        compile_test({"name": "x", "type": "row_count", "model": "dim_date"})


def test_accepted_values_escapes_quotes():
    sql = compile_test(
        {"name": "x", "type": "accepted_values", "model": "m", "column": "c", "values": ["O'Brien"]}
    )
    assert "'O''Brien'" in sql


def test_where_clause_scopes_a_test():
    sql = compile_test(
        {"name": "x", "type": "not_null", "model": "m", "column": "c", "where": "key <> -1"}
    )
    assert "key <> -1" in sql
