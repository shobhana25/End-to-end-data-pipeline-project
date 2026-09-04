"""Shared fixtures.

``warehouse`` builds the entire pipeline - ingest, stage, transform - against
the small committed fixtures in a throwaway directory. Every test that needs
data gets a real, freshly built star schema rather than a mock, so the tests
exercise the SQL models and not just the Python around them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
FIXTURE_SOURCES = FIXTURE_DIR / "sources.yml"

sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    """An isolated data root, so tests never touch the real warehouse."""
    return tmp_path_factory.mktemp("pipeline-data")


@pytest.fixture(scope="session")
def pipeline_modules(data_dir, monkeypatch_session):
    """The pipeline modules, with PIPELINE_DATA_DIR pointed at the scratch root.

    No module reload is needed: ``config.paths()`` reads the environment on
    every call, so redirecting the data root is a pure environment change.
    (Reloading here would also re-create the exception classes, quietly
    breaking every ``pytest.raises`` in the suite that imported them earlier.)
    """
    monkeypatch_session.setenv("PIPELINE_DATA_DIR", str(data_dir))
    from pipelines import config, dashboard, ingest, quality, stage, warehouse

    assert config.paths().data == data_dir
    return {
        "config": config,
        "ingest": ingest,
        "stage": stage,
        "warehouse": warehouse,
        "quality": quality,
        "dashboard": dashboard,
    }


@pytest.fixture(scope="session")
def monkeypatch_session():
    from _pytest.monkeypatch import MonkeyPatch

    patcher = MonkeyPatch()
    yield patcher
    patcher.undo()


@pytest.fixture(scope="session")
def build_runs(pipeline_modules, data_dir):
    """Build the warehouse from the fixtures - twice.

    The second build is not waste: every model is CREATE OR REPLACE, so running
    the transform again must reproduce the same warehouse rather than append to
    it. Building twice here lets the idempotency test compare two real runs
    without fighting DuckDB over a second writable connection.
    """
    config = pipeline_modules["config"]
    sources = config.load_sources(FIXTURE_SOURCES)

    pipeline_modules["ingest"].run(sources=sources)
    pipeline_modules["stage"].run(sources=sources)
    first = pipeline_modules["warehouse"].run()
    second = pipeline_modules["warehouse"].run()
    return first, second


@pytest.fixture
def warehouse(build_runs, pipeline_modules):
    """A read-only connection, open only for the duration of one test.

    DuckDB refuses a second connection to the same file with a different
    configuration, so nothing may hold a read-only handle open while pipeline
    code opens its own writable one. Keeping connections short-lived is what
    lets a test run the real ``quality.run()`` against the same warehouse.
    """
    import duckdb

    connection = duckdb.connect(str(pipeline_modules["config"].paths().database), read_only=True)
    yield connection
    connection.close()


@pytest.fixture
def query(warehouse):
    """Run a query against the built warehouse and return rows as tuples."""

    def _query(sql: str, params: list | None = None):
        return warehouse.execute(sql, params or []).fetchall()

    return _query


@pytest.fixture
def scalar(query):
    """Run a query and return the first column of its first row."""

    def _scalar(sql: str, params: list | None = None):
        rows = query(sql, params)
        return rows[0][0] if rows else None

    return _scalar
