"""Warehouse: run the SQL models that build the star schema in DuckDB.

The SQL files are plain SQL - no templating language, no macros. Python's only
job here is to (a) expose the staged Parquet files as ``raw_*`` views so the
SQL never has to hardcode a filesystem path, and (b) execute the model files in
lexical order inside a single transaction per layer.

Every model is written as ``CREATE OR REPLACE``, so the whole build is
idempotent: running it twice produces the same warehouse, and a failed run
leaves the previous warehouse intact.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb

from pipelines.config import SQL_DIR, Paths, paths
from pipelines.ingest import MANIFEST_FILENAME
from pipelines.logging_conf import get_logger

log = get_logger("pipelines.warehouse")

# staged Parquet file -> the view name the SQL layer reads it through
_STAGED_VIEWS = {
    "stg_hospital_activity.parquet": "raw_hospital_activity",
    "stg_iso_countries.parquet": "raw_iso_countries",
    "stg_population.parquet": "raw_population",
    "stg_location_overrides.parquet": "raw_location_overrides",
}


class WarehouseError(RuntimeError):
    """Raised when a SQL model fails to build."""


@dataclass(frozen=True)
class ModelResult:
    """Outcome of building one SQL model."""

    layer: str
    name: str
    rows: int
    seconds: float


@contextmanager
def connect(
    layout: Paths | None = None, read_only: bool = False
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open the warehouse, always closing it again."""
    layout = layout or paths()
    layout.ensure()
    connection = duckdb.connect(str(layout.database), read_only=read_only)
    try:
        yield connection
    finally:
        connection.close()


def register_staged_views(
    connection: duckdb.DuckDBPyConnection, layout: Paths | None = None
) -> list[str]:
    """Expose the staged Parquet files to SQL as ``raw_*`` views."""
    layout = layout or paths()
    registered: list[str] = []
    for filename, view in _STAGED_VIEWS.items():
        path = layout.staged / filename
        if not path.exists():
            raise WarehouseError(f"staged file missing: {path} (run `stage` before `transform`)")
        connection.execute(
            f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{path.as_posix()}')"
        )
        registered.append(view)

    # Ingestion provenance: one row per source per run, so any fact row can be
    # traced back to the URL and checksum it came from.
    manifest = layout.raw / MANIFEST_FILENAME
    if manifest.exists():
        connection.execute(
            "CREATE OR REPLACE TABLE meta_ingestion_runs AS "
            f"SELECT * FROM read_json_auto('{manifest.as_posix()}', format='newline_delimited')"
        )
        registered.append("meta_ingestion_runs")
    return registered


def _model_files(layer: str) -> list[Path]:
    directory = SQL_DIR / layer
    if not directory.is_dir():
        raise WarehouseError(f"no such SQL layer: {directory}")
    return sorted(directory.glob("*.sql"))


def _model_name(path: Path) -> str:
    """``02_dim_location.sql`` -> ``dim_location``."""
    stem = path.stem
    prefix, _, remainder = stem.partition("_")
    return remainder if prefix.isdigit() and remainder else stem


def run_layer(connection: duckdb.DuckDBPyConnection, layer: str) -> list[ModelResult]:
    """Execute every ``.sql`` file in ``sql/<layer>/`` in lexical order."""
    results: list[ModelResult] = []
    for path in _model_files(layer):
        name = _model_name(path)
        statement = path.read_text()
        started = time.monotonic()
        try:
            connection.execute(statement)
        except duckdb.Error as error:
            raise WarehouseError(f"{layer}/{path.name} failed: {error}") from error
        elapsed = time.monotonic() - started

        try:
            rows = connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        except duckdb.Error:
            rows = -1  # a model that creates several objects, or none countable

        results.append(ModelResult(layer=layer, name=name, rows=rows, seconds=elapsed))
        log.info("%-9s %-28s %10s rows  %5.2fs", layer, name, f"{rows:,}", elapsed)
    return results


def run(layers: tuple[str, ...] = ("staging", "marts")) -> list[ModelResult]:
    """Build the warehouse from the staged Parquet files."""
    layout = paths()
    results: list[ModelResult] = []
    with connect(layout) as connection:
        views = register_staged_views(connection, layout)
        log.info("registered %d source views: %s", len(views), ", ".join(views))
        for layer in layers:
            results.extend(run_layer(connection, layer))
        connection.execute("CHECKPOINT")

    size_mib = layout.database.stat().st_size / 1024 / 1024
    log.info("built %d models -> %s (%.1f MiB)", len(results), layout.database.name, size_mib)
    return results


def query(sql: str, layout: Paths | None = None):
    """Convenience read-only query returning a DataFrame."""
    with connect(layout, read_only=True) as connection:
        return connection.execute(sql).df()
