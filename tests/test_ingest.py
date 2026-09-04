"""Ingestion: provenance, atomicity, and retry behaviour.

Network calls are stubbed - these tests are about the ingest layer's own
guarantees, not about whether a publisher is up.
"""

from __future__ import annotations

import json

import pytest
import requests

from pipelines import ingest
from pipelines.config import Paths, Source
from pipelines.ingest import MANIFEST_FILENAME, IngestionError, fetch_source


@pytest.fixture
def layout(tmp_path) -> Paths:
    return Paths(
        data=tmp_path,
        raw=tmp_path / "raw",
        staged=tmp_path / "staged",
        warehouse=tmp_path / "warehouse",
        database=tmp_path / "warehouse" / "db.duckdb",
    ).ensure()


def _source(url: str, **overrides) -> Source:
    defaults = {
        "name": "demo",
        "title": "demo",
        "publisher": "p",
        "licence": "l",
        "url": url,
        "format": "csv",
        "landing_file": "demo.csv",
        "enabled": True,
        "role": "fact",
        "max_retries": 3,
        "backoff_seconds": 0,
    }
    defaults.update(overrides)
    return Source(**defaults)


class _Response:
    """Minimal stand-in for a streaming requests.Response."""

    def __init__(self, status: int, payload: bytes = b"col\n1\n"):
        self.status_code = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def iter_content(self, chunk_size=None):
        yield self._payload


def test_local_file_sources_are_copied_and_checksummed(layout):
    source = _source("file://tests/fixtures/iso_3166_countries.csv")
    result = fetch_source(source, "run-1", layout)

    assert (layout.raw / "demo.csv").exists()
    assert result.bytes_downloaded > 0
    assert len(result.sha256) == 64
    assert result.content_changed is True  # nothing previous to compare against


def test_a_second_identical_fetch_is_reported_as_unchanged(layout):
    source = _source("file://tests/fixtures/iso_3166_countries.csv")
    first = fetch_source(source, "run-1", layout)
    ingest.append_manifest([first], layout)

    second = fetch_source(source, "run-2", layout)
    assert second.sha256 == first.sha256
    assert second.content_changed is False


def test_offline_mode_reuses_the_landing_zone(layout):
    source = _source("file://tests/fixtures/iso_3166_countries.csv")
    fetch_source(source, "run-1", layout)

    result = fetch_source(source, "run-2", layout, offline=True)
    assert result.from_cache is True
    assert result.http_status is None


def test_offline_mode_fails_when_nothing_has_been_ingested(layout):
    with pytest.raises(IngestionError, match="offline mode"):
        fetch_source(_source("https://example.test/x.csv"), "run", layout, offline=True)


def test_missing_local_file_is_reported_clearly(layout):
    with pytest.raises(IngestionError, match="local file not found"):
        fetch_source(_source("file://does/not/exist.csv"), "run", layout)


def test_transient_failures_are_retried(layout, monkeypatch):
    attempts = {"n": 0}

    def flaky(*_args, **_kwargs):
        attempts["n"] += 1
        return _Response(503 if attempts["n"] < 3 else 200)

    monkeypatch.setattr(requests, "get", flaky)
    result = fetch_source(_source("https://example.test/x.csv"), "run", layout)
    assert result.attempts == 3
    assert attempts["n"] == 3


def test_a_404_is_not_retried(layout, monkeypatch):
    attempts = {"n": 0}

    def missing(*_args, **_kwargs):
        attempts["n"] += 1
        return _Response(404)

    monkeypatch.setattr(requests, "get", missing)
    with pytest.raises(IngestionError, match="not retryable"):
        fetch_source(_source("https://example.test/x.csv"), "run", layout)
    assert attempts["n"] == 1, "a 404 will not fix itself; retrying only wastes time"


def test_exhausted_retries_raise(layout, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response(500))
    with pytest.raises(IngestionError, match="giving up after"):
        fetch_source(_source("https://example.test/x.csv"), "run", layout)


def test_an_empty_payload_is_rejected(layout, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response(200, b""))
    with pytest.raises(IngestionError, match="empty"):
        fetch_source(_source("https://example.test/x.csv"), "run", layout)


def test_a_failed_download_leaves_no_partial_file(layout, monkeypatch):
    """The landing zone must never hold a half-written file that the staging
    layer would happily parse."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response(500))
    with pytest.raises(IngestionError):
        fetch_source(_source("https://example.test/x.csv"), "run", layout)

    assert not (layout.raw / "demo.csv").exists()
    assert not list(layout.raw.glob("*.part"))


def test_a_failed_download_does_not_destroy_the_previous_file(layout, monkeypatch):
    good = fetch_source(_source("file://tests/fixtures/iso_3166_countries.csv"), "r1", layout)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response(500))
    with pytest.raises(IngestionError):
        fetch_source(_source("https://example.test/x.csv"), "r2", layout)

    assert (layout.raw / "demo.csv").read_bytes()
    assert ingest._sha256(layout.raw / "demo.csv") == good.sha256


def test_manifest_records_one_json_line_per_source(layout):
    results = [fetch_source(_source("file://tests/fixtures/iso_3166_countries.csv"), "r", layout)]
    path = ingest.append_manifest(results, layout)

    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert {"run_id", "source_name", "url", "sha256", "bytes_downloaded"} <= set(lines[0])
    assert path.name == MANIFEST_FILENAME


def test_a_corrupt_manifest_line_does_not_break_the_next_run(layout):
    (layout.raw / MANIFEST_FILENAME).write_text('{"truncated": \n')
    result = fetch_source(_source("file://tests/fixtures/iso_3166_countries.csv"), "r", layout)
    assert result.sha256
