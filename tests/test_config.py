"""The source registry is the pipeline's contract with the outside world."""

from __future__ import annotations

import pytest
import yaml

from pipelines.config import ConfigError, Source, load_sources


def _write(tmp_path, document) -> object:
    path = tmp_path / "sources.yml"
    path.write_text(yaml.safe_dump(document))
    return path


_MINIMAL = {
    "name": "demo",
    "url": "https://example.test/demo.csv",
    "format": "csv",
    "landing_file": "demo.csv",
    "enabled": True,
}


def test_loads_the_real_registry():
    sources = load_sources(enabled_only=False)
    assert {"owid_hospitalisations", "iso_3166_countries", "world_bank_population"} <= {
        s.name for s in sources
    }


def test_enabled_only_is_the_default():
    assert all(s.enabled for s in load_sources())
    assert len(load_sources(enabled_only=False)) > len(load_sources())


def test_defaults_apply_to_every_source(tmp_path):
    path = _write(tmp_path, {"defaults": {"max_retries": 9}, "sources": [_MINIMAL]})
    assert load_sources(path)[0].max_retries == 9


def test_per_source_settings_beat_defaults(tmp_path):
    path = _write(
        tmp_path,
        {"defaults": {"max_retries": 9}, "sources": [{**_MINIMAL, "max_retries": 2}]},
    )
    assert load_sources(path)[0].max_retries == 2


def test_missing_key_is_rejected(tmp_path):
    broken = {k: v for k, v in _MINIMAL.items() if k != "landing_file"}
    with pytest.raises(ConfigError, match="missing keys"):
        load_sources(_write(tmp_path, {"sources": [broken]}))


def test_duplicate_source_name_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="duplicate source name"):
        load_sources(_write(tmp_path, {"sources": [_MINIMAL, dict(_MINIMAL)]}))


def test_enabled_source_without_a_url_is_rejected(tmp_path):
    """The AIHW template ships disabled with url: null - enabling it without
    pinning a URL must fail at config load, not halfway through a fetch."""
    with pytest.raises(ConfigError, match="enabled but has no url"):
        load_sources(_write(tmp_path, {"sources": [{**_MINIMAL, "url": None}]}))


def test_disabled_source_may_omit_its_url(tmp_path):
    path = _write(tmp_path, {"sources": [{**_MINIMAL, "url": None, "enabled": False}]})
    assert load_sources(path, enabled_only=False)[0].url is None


def test_empty_registry_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="declares no sources"):
        load_sources(_write(tmp_path, {"sources": []}))


def test_file_urls_resolve_against_the_repository_root():
    source = Source(
        name="local",
        title="t",
        publisher="p",
        licence="l",
        url="file://tests/fixtures/iso_3166_countries.csv",
        format="csv",
        landing_file="x.csv",
        enabled=True,
        role="dimension",
    )
    assert source.is_local
    assert source.local_path().exists()
