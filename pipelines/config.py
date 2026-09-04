"""Project paths and the source registry loader.

Everything the pipeline needs to know about *where* things live is resolved
here, from the repository root, so the pipeline behaves identically whether it
is invoked from the repo root, from CI, or from a test tmpdir.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# pipelines/config.py -> pipelines/ -> repository root
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
SQL_DIR = PROJECT_ROOT / "sql"
DOCS_DIR = PROJECT_ROOT / "docs"

DEFAULT_SOURCES_FILE = CONFIG_DIR / "sources.yml"
LOCATION_OVERRIDES_FILE = CONFIG_DIR / "location_overrides.csv"
QUALITY_TESTS_FILE = CONFIG_DIR / "quality_tests.yml"


def sources_file() -> Path:
    """The active source registry.

    Overridable with PIPELINE_SOURCES so a run can be pointed at a different
    registry - the fixture registry in CI, say - without editing config.
    """
    override = os.environ.get("PIPELINE_SOURCES")
    if not override:
        return DEFAULT_SOURCES_FILE
    candidate = Path(override)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _data_root() -> Path:
    """Data root, overridable so tests and CI can use a scratch directory."""
    override = os.environ.get("PIPELINE_DATA_DIR")
    return Path(override).resolve() if override else PROJECT_ROOT / "data"


@dataclass(frozen=True)
class Paths:
    """Resolved filesystem layout for one pipeline run."""

    data: Path
    raw: Path
    staged: Path
    warehouse: Path
    database: Path

    @classmethod
    def build(cls) -> Paths:
        data = _data_root()
        return cls(
            data=data,
            raw=data / "raw",
            staged=data / "staged",
            warehouse=data / "warehouse",
            database=data / "warehouse" / "health_capacity.duckdb",
        )

    def ensure(self) -> Paths:
        for directory in (self.data, self.raw, self.staged, self.warehouse):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def paths() -> Paths:
    """Resolve (and create) the data directories for this run."""
    return Paths.build().ensure()


@dataclass(frozen=True)
class Source:
    """One declared dataset from config/sources.yml."""

    name: str
    title: str
    publisher: str
    licence: str
    url: str | None
    format: str
    landing_file: str
    enabled: bool
    role: str
    description: str = ""
    licence_url: str = ""
    homepage: str = ""
    expected_columns: list[str] = field(default_factory=list)
    min_rows: int = 0
    timeout_seconds: int = 120
    max_retries: int = 4
    backoff_seconds: int = 2

    @property
    def is_local(self) -> bool:
        """True when the source points at a file:// path instead of HTTP(S)."""
        return bool(self.url) and self.url.startswith("file://")

    def local_path(self) -> Path:
        """Resolve a file:// URL relative to the repository root."""
        if not self.is_local:
            raise ValueError(f"source {self.name!r} is not a file:// source")
        raw = self.url[len("file://") :]
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


class ConfigError(RuntimeError):
    """Raised when the source registry is malformed."""


_REQUIRED_SOURCE_KEYS = ("name", "url", "format", "landing_file", "enabled")


def load_sources(path: Path | None = None, enabled_only: bool = True) -> list[Source]:
    """Read config/sources.yml into validated :class:`Source` objects."""
    path = path or sources_file()
    if not path.exists():
        raise ConfigError(f"source registry not found: {path}")

    document: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    defaults: dict[str, Any] = document.get("defaults") or {}
    entries = document.get("sources")
    if not entries:
        raise ConfigError(f"{path} declares no sources")

    sources: list[Source] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"source #{index} in {path} is not a mapping")
        missing = [k for k in _REQUIRED_SOURCE_KEYS if k not in entry]
        if missing:
            raise ConfigError(
                f"source {entry.get('name', f'#{index}')} is missing keys: {', '.join(missing)}"
            )
        if entry["name"] in seen:
            raise ConfigError(f"duplicate source name: {entry['name']}")
        seen.add(entry["name"])

        if entry["enabled"] and not entry.get("url"):
            raise ConfigError(
                f"source {entry['name']!r} is enabled but has no url; "
                "set a URL (or a file:// path) or disable it"
            )

        sources.append(
            Source(
                name=entry["name"],
                title=entry.get("title", entry["name"]),
                publisher=entry.get("publisher", "unknown"),
                licence=entry.get("licence", "unknown"),
                licence_url=entry.get("licence_url", ""),
                homepage=entry.get("homepage", ""),
                url=entry.get("url"),
                format=entry["format"],
                landing_file=entry["landing_file"],
                enabled=bool(entry["enabled"]),
                role=entry.get("role", "fact"),
                description=(entry.get("description") or "").strip(),
                expected_columns=list(entry.get("expected_columns") or []),
                min_rows=int(entry.get("min_rows") or 0),
                timeout_seconds=int(
                    entry.get("timeout_seconds", defaults.get("timeout_seconds", 120))
                ),
                max_retries=int(entry.get("max_retries", defaults.get("max_retries", 4))),
                backoff_seconds=int(
                    entry.get("backoff_seconds", defaults.get("backoff_seconds", 2))
                ),
            )
        )

    return [s for s in sources if s.enabled] if enabled_only else sources
