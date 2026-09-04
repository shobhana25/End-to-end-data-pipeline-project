"""The whole pipeline, run offline against the committed fixtures.

If this passes, `python -m pipelines.cli all` works: sources are fetched,
cleaned, modelled, asserted and published, and the published page reflects the
warehouse it was built from.
"""

from __future__ import annotations

import re

import pytest

from pipelines import quality
from pipelines.cli import EXIT_OK, EXIT_QUALITY_FAILURE, build_parser, main


def test_the_quality_suite_passes_on_the_fixture_warehouse(build_runs, pipeline_modules):
    results = pipeline_modules["quality"].run(
        tests=quality.load_tests(include_volume_dependent=False)
    )
    blocking = [r.name for r in results if r.is_blocking_failure]
    assert not blocking, f"blocking failures: {blocking}"
    assert len(results) >= 20


def test_quality_results_are_persisted_for_later_inspection(build_runs, pipeline_modules):
    pipeline_modules["quality"].run(tests=quality.load_tests(include_volume_dependent=False))
    import duckdb

    connection = duckdb.connect(str(pipeline_modules["config"].paths().database), read_only=True)
    try:
        rows = connection.execute(
            "SELECT count(*), count(DISTINCT run_id) FROM meta_quality_results"
        ).fetchone()
    finally:
        connection.close()
    assert rows[0] > 0 and rows[1] >= 1


def test_ingestion_provenance_is_queryable_from_the_warehouse(query):
    rows = query("SELECT source_name, url, sha256, bytes_downloaded FROM meta_ingestion_runs")
    assert rows
    for _, url, checksum, size in rows:
        assert url and len(checksum) == 64 and size > 0


@pytest.fixture(scope="module")
def rendered(build_runs, pipeline_modules, tmp_path_factory):
    output = tmp_path_factory.mktemp("docs") / "index.html"
    pipeline_modules["dashboard"].run(focus="AUS", output=output)
    return output.read_text()


def test_dashboard_is_a_complete_self_contained_document(rendered):
    assert rendered.startswith("<!doctype html>")
    assert rendered.rstrip().endswith("</html>")
    assert "<title>" in rendered and "<style>" in rendered


def test_dashboard_loads_nothing_from_the_network(rendered):
    """The published page has to work offline and behind a proxy that blocks
    third-party scripts, so no external resource may sneak in."""
    for pattern in (r"<script[^>]+src=", r"<link[^>]+href=", r"@import", r"https?://cdn"):
        assert not re.search(pattern, rendered, re.I), f"external resource: {pattern}"


def test_dashboard_supports_both_themes(rendered):
    assert "prefers-color-scheme: dark" in rendered
    assert '[data-theme="dark"]' in rendered


def test_dashboard_reports_figures_from_the_warehouse(rendered, scalar):
    facts = scalar("SELECT count(*) FROM fct_hospital_activity")
    assert f"{facts:,}" in rendered

    peak = scalar(
        """
        SELECT round(peak_per_100k, 2) FROM mart_peak_pressure
        WHERE source_location_code = 'AUS'
          AND indicator_code = 'daily_intensive_care_occupancy'
        """
    )
    assert peak is not None
    assert f"{peak:.2f}" in rendered


def test_dashboard_names_every_source_it_used(rendered, query):
    for (name,) in query("SELECT DISTINCT source_name FROM meta_ingestion_runs"):
        assert name in rendered


def test_dashboard_surfaces_the_quality_result(rendered):
    assert "quality" in rendered.lower()
    assert "pass" in rendered.lower()


def test_dashboard_can_lead_with_a_different_country(build_runs, pipeline_modules, tmp_path):
    """The marts are generic; only the presentation layer picks a focus."""
    output = tmp_path / "fr.html"
    pipeline_modules["dashboard"].run(focus="FRA", output=output)
    assert "France" in output.read_text()


# -- CLI -------------------------------------------------------------------


def test_cli_exposes_every_stage():
    parser = build_parser()
    for command in ("ingest", "stage", "transform", "quality", "dashboard", "all"):
        assert parser.parse_args([command]).command == command


def test_cli_rejects_an_unknown_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["deploy-to-prod"])


def test_cli_can_point_at_an_alternative_source_registry(monkeypatch, tmp_path):
    """CI runs the whole pipeline against the fixture registry, so the override
    has to actually reach config.load_sources()."""
    from pipelines import config

    monkeypatch.delenv("PIPELINE_SOURCES", raising=False)
    assert config.sources_file() == config.DEFAULT_SOURCES_FILE

    monkeypatch.setenv("PIPELINE_SOURCES", "tests/fixtures/sources.yml")
    assert config.sources_file().name == "sources.yml"
    assert "fixtures" in str(config.sources_file())
    assert {s.name for s in config.load_sources()} == {
        "owid_hospitalisations",
        "iso_3166_countries",
        "world_bank_population",
    }


def test_skip_volume_tests_drops_only_the_production_calibrated_assertions():
    full = {t["name"] for t in quality.load_tests()}
    reduced = {t["name"] for t in quality.load_tests(include_volume_dependent=False)}
    assert reduced < full
    for name in full - reduced:
        declaration = next(t for t in quality.load_tests() if t["name"] == name)
        assert declaration["requires_full_volume"] is True


def test_cli_returns_a_distinct_exit_code_for_a_quality_failure(build_runs, monkeypatch):
    """A scheduler needs to tell 'the data is wrong' from 'the pipeline broke'."""
    monkeypatch.setattr(
        quality,
        "run",
        lambda *a, **k: [
            quality.QualityResult(
                run_id="r",
                executed_at="now",
                name="fake",
                model="m",
                test_type="sql",
                severity="error",
                status="fail",
                failing_rows=1,
                duration_seconds=0.0,
                description="",
                message="",
            )
        ],
    )
    assert main(["quality"]) == EXIT_QUALITY_FAILURE


def test_cli_treats_a_warning_as_non_blocking(build_runs, monkeypatch):
    monkeypatch.setattr(
        quality,
        "run",
        lambda *a, **k: [
            quality.QualityResult(
                run_id="r",
                executed_at="now",
                name="fake",
                model="m",
                test_type="sql",
                severity="warn",
                status="fail",
                failing_rows=1,
                duration_seconds=0.0,
                description="",
                message="",
            )
        ],
    )
    assert main(["quality"]) == EXIT_OK
