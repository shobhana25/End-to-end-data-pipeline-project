"""Data quality: declarative assertions against the built warehouse.

Tests are declared in ``config/quality_tests.yml``, not written as code, so
adding a check is a one-paragraph config change that a reviewer can read
without opening Python. Each test compiles to a SQL statement that returns the
*failing* rows; a test passes when that count is zero.

Test types
----------
``not_null``            column has no NULLs
``unique``              column has no repeated values
``unique_combination``  a set of columns is unique together (grain enforcement)
``accepted_values``     column only ever holds values from a fixed list
``relationships``       every value in a column exists in a parent table
                        (referential integrity across the star)
``row_count``           table size is within expected bounds
``expression``          a boolean SQL expression holds for every row
``sql``                 an arbitrary query whose result set must be empty

Severity
--------
``error`` fails the run and makes the CLI exit non-zero. ``warn`` is recorded
and printed but does not fail the build - used for signals that need a human
eye rather than a broken pipeline.

Scope
-----
A test marked ``requires_full_volume: true`` has a threshold calibrated to the
production feeds - a row-count floor, a last-known date, a tolerance tuned to
the real mix of countries. Those are meaningless against the small committed
fixtures, so the offline test run skips them and exercises everything else.

Every result is written to ``meta_quality_results`` in the warehouse, so the
quality history is queryable alongside the data it describes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from pipelines.config import QUALITY_TESTS_FILE, Paths, paths
from pipelines.logging_conf import get_logger
from pipelines.warehouse import connect

log = get_logger("pipelines.quality")

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"
_VALID_SEVERITIES = {SEVERITY_ERROR, SEVERITY_WARN}


class QualityConfigError(RuntimeError):
    """Raised when a test declaration is malformed."""


@dataclass(frozen=True)
class QualityResult:
    """Outcome of one assertion."""

    run_id: str
    executed_at: str
    name: str
    model: str
    test_type: str
    severity: str
    status: str  # pass | fail | error
    failing_rows: int
    duration_seconds: float
    description: str
    message: str

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def is_blocking_failure(self) -> bool:
        return not self.passed and self.severity == SEVERITY_ERROR


def _quote_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _where(test: dict[str, Any]) -> str:
    """Optional row filter, so a test can exempt the Unknown dimension member."""
    clause = test.get("where")
    return f" AND ({clause})" if clause else ""


def compile_test(test: dict[str, Any]) -> str:
    """Turn one declaration into SQL that returns the failing rows."""
    test_type = test["type"]
    model = test.get("model", "")

    if test_type == "not_null":
        column = test["column"]
        return f"SELECT {column} FROM {model} WHERE {column} IS NULL{_where(test)}"

    if test_type == "unique":
        column = test["column"]
        return (
            f"SELECT {column}, count(*) AS occurrences FROM {model} "
            f"WHERE TRUE{_where(test)} GROUP BY {column} HAVING count(*) > 1"
        )

    if test_type == "unique_combination":
        columns = ", ".join(test["columns"])
        return (
            f"SELECT {columns}, count(*) AS occurrences FROM {model} "
            f"WHERE TRUE{_where(test)} GROUP BY {columns} HAVING count(*) > 1"
        )

    if test_type == "accepted_values":
        column = test["column"]
        values = ", ".join(_quote_literal(v) for v in test["values"])
        return (
            f"SELECT DISTINCT {column} FROM {model} "
            f"WHERE {column} IS NOT NULL AND {column} NOT IN ({values}){_where(test)}"
        )

    if test_type == "relationships":
        column, parent, parent_column = test["column"], test["to_model"], test["to_column"]
        return (
            f"SELECT c.{column} FROM {model} AS c "
            f"LEFT JOIN {parent} AS p ON p.{parent_column} = c.{column} "
            f"WHERE c.{column} IS NOT NULL AND p.{parent_column} IS NULL{_where(test)}"
        )

    if test_type == "row_count":
        minimum, maximum = test.get("min"), test.get("max")
        if minimum is None and maximum is None:
            raise QualityConfigError(f"{test['name']}: row_count needs min and/or max")
        conditions = []
        if minimum is not None:
            conditions.append(f"n < {minimum}")
        if maximum is not None:
            conditions.append(f"n > {maximum}")
        return f"SELECT n FROM (SELECT count(*) AS n FROM {model}) WHERE {' OR '.join(conditions)}"

    if test_type == "expression":
        return f"SELECT * FROM {model} WHERE NOT ({test['expression']}){_where(test)}"

    if test_type == "sql":
        return test["sql"]

    raise QualityConfigError(f"{test.get('name', '<unnamed>')}: unknown test type {test_type!r}")


def load_tests(
    path: Path | None = None, include_volume_dependent: bool = True
) -> list[dict[str, Any]]:
    """Read and validate config/quality_tests.yml.

    Set ``include_volume_dependent=False`` to drop the assertions whose
    thresholds only make sense against the full production feeds.
    """
    path = path or QUALITY_TESTS_FILE
    if not path.exists():
        raise QualityConfigError(f"quality test suite not found: {path}")

    document = yaml.safe_load(path.read_text()) or {}
    defaults = document.get("defaults") or {}
    declarations = document.get("tests") or []
    if not declarations:
        raise QualityConfigError(f"{path} declares no tests")

    tests: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise QualityConfigError(f"test #{index} is not a mapping")
        test = {**defaults, **declaration}
        for key in ("name", "type"):
            if key not in test:
                raise QualityConfigError(f"test #{index} is missing {key!r}")
        if test["name"] in seen:
            raise QualityConfigError(f"duplicate test name: {test['name']}")
        seen.add(test["name"])
        if test["type"] != "sql" and "model" not in test:
            raise QualityConfigError(f"{test['name']}: {test['type']} tests need a model")
        severity = test.setdefault("severity", SEVERITY_ERROR)
        if severity not in _VALID_SEVERITIES:
            raise QualityConfigError(
                f"{test['name']}: severity must be one of {sorted(_VALID_SEVERITIES)}"
            )
        compile_test(test)  # fail fast on a malformed declaration
        if not include_volume_dependent and test.get("requires_full_volume"):
            continue
        tests.append(test)
    return tests


def _execute(
    connection: duckdb.DuckDBPyConnection, test: dict[str, Any], run_id: str
) -> QualityResult:
    started = time.monotonic()
    executed_at = datetime.now(UTC).isoformat(timespec="seconds")
    statement = compile_test(test)

    try:
        failing = connection.execute(f"SELECT count(*) FROM ({statement}) AS failures").fetchone()[
            0
        ]
        status = "pass" if failing == 0 else "fail"
        message = "" if failing == 0 else f"{failing:,} failing row(s)"
    except duckdb.Error as error:
        failing, status, message = -1, "error", str(error).splitlines()[0]

    return QualityResult(
        run_id=run_id,
        executed_at=executed_at,
        name=test["name"],
        model=test.get("model", "-"),
        test_type=test["type"],
        severity=test["severity"],
        status=status,
        failing_rows=failing,
        duration_seconds=round(time.monotonic() - started, 4),
        description=(test.get("description") or "").strip(),
        message=message,
    )


def _persist(connection: duckdb.DuckDBPyConnection, results: list[QualityResult]) -> None:
    """Append this run's results to meta_quality_results."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS meta_quality_results (
            run_id           VARCHAR,
            executed_at      VARCHAR,
            test_name        VARCHAR,
            model            VARCHAR,
            test_type        VARCHAR,
            severity         VARCHAR,
            status           VARCHAR,
            failing_rows     BIGINT,
            duration_seconds DOUBLE,
            description      VARCHAR,
            message          VARCHAR
        )
        """
    )
    connection.executemany(
        "INSERT INTO meta_quality_results VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r.run_id,
                r.executed_at,
                r.name,
                r.model,
                r.test_type,
                r.severity,
                r.status,
                r.failing_rows,
                r.duration_seconds,
                r.description,
                r.message,
            )
            for r in results
        ],
    )


def run(
    layout: Paths | None = None, tests: list[dict[str, Any]] | None = None
) -> list[QualityResult]:
    """Run the suite against the warehouse and persist the results."""
    layout = layout or paths()
    tests = tests if tests is not None else load_tests()
    run_id = str(uuid.uuid4())

    with connect(layout) as connection:
        results = [_execute(connection, test, run_id) for test in tests]
        _persist(connection, results)

    for result in results:
        if result.passed:
            log.info("PASS  %-46s %s", result.name, result.model)
        else:
            log.log(
                40 if result.severity == SEVERITY_ERROR else 30,
                "%s  %-46s %s  %s",
                result.status.upper().ljust(5),
                result.name,
                result.model,
                result.message,
            )

    passed = sum(1 for r in results if r.passed)
    blocking = [r for r in results if r.is_blocking_failure]
    warnings = [r for r in results if not r.passed and r.severity == SEVERITY_WARN]
    log.info(
        "quality: %d/%d passed, %d warning(s), %d blocking failure(s)",
        passed,
        len(results),
        len(warnings),
        len(blocking),
    )
    return results


def summarise(results: list[QualityResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "warnings": sum(1 for r in results if not r.passed and r.severity == SEVERITY_WARN),
        "blocking": sum(1 for r in results if r.is_blocking_failure),
    }
