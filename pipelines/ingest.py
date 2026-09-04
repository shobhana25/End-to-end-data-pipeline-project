"""Ingestion: declared sources -> immutable landing zone, with provenance.

Design notes
------------
* **The landing zone is immutable.** Files are streamed to a temporary file and
  atomically renamed into place, so a failed or interrupted download can never
  leave a half-written file that the staging layer would happily parse.
* **Every fetch is checksummed.** The SHA-256 of the payload is recorded, and
  compared against the previous run so the pipeline can report whether the
  publisher actually changed anything.
* **Every fetch is recorded.** One JSON line per source per run is appended to
  ``data/raw/_ingestion_manifest.jsonl``. That file is loaded into the
  warehouse as ``meta_ingestion_runs``, which is what makes a fact row
  traceable back to a URL and a byte-for-byte payload.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

from pipelines.config import Paths, Source, load_sources, paths
from pipelines.logging_conf import get_logger

log = get_logger("pipelines.ingest")

MANIFEST_FILENAME = "_ingestion_manifest.jsonl"
_CHUNK_BYTES = 1 << 16  # 64 KiB
_USER_AGENT = "health-capacity-pipeline/1.0 (+https://github.com/shobhana25)"


class IngestionError(RuntimeError):
    """Raised when a source cannot be fetched after all retries."""


@dataclass(frozen=True)
class IngestResult:
    """Provenance record for a single source in a single run."""

    run_id: str
    source_name: str
    url: str
    fetched_at_utc: str
    landing_path: str
    bytes_downloaded: int
    sha256: str
    http_status: int | None
    attempts: int
    duration_seconds: float
    content_changed: bool
    from_cache: bool

    def as_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _previous_checksum(manifest_path: Path, source_name: str) -> str | None:
    """Checksum from the most recent successful run of this source, if any."""
    if not manifest_path.exists():
        return None
    latest: str | None = None
    for line in manifest_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a truncated line must not break the next run
        if record.get("source_name") == source_name:
            latest = record.get("sha256")
    return latest


def _download_http(source: Source, destination: Path) -> tuple[int, int]:
    """Stream ``source.url`` to ``destination``. Returns (status, attempts).

    Retries with exponential backoff on transport errors and 5xx/429 responses.
    A 4xx other than 429 is not retried - it will not fix itself.
    """
    delay = source.backoff_seconds
    last_error: Exception | None = None

    for attempt in range(1, source.max_retries + 1):
        try:
            with requests.get(
                source.url,
                stream=True,
                timeout=source.timeout_seconds,
                headers={"User-Agent": _USER_AGENT},
            ) as response:
                if response.status_code >= 500 or response.status_code == 429:
                    response.raise_for_status()
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                        if chunk:
                            handle.write(chunk)
                return response.status_code, attempt
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            last_error = error
            if status is not None and 400 <= status < 500 and status != 429:
                raise IngestionError(
                    f"{source.name}: {source.url} returned HTTP {status} (not retryable)"
                ) from error
        except requests.RequestException as error:
            last_error = error

        if attempt < source.max_retries:
            log.warning(
                "%s: attempt %d/%d failed (%s); retrying in %ds",
                source.name,
                attempt,
                source.max_retries,
                type(last_error).__name__,
                delay,
            )
            time.sleep(delay)
            delay *= 2

    raise IngestionError(
        f"{source.name}: giving up after {source.max_retries} attempts - {last_error}"
    )


def fetch_source(
    source: Source,
    run_id: str,
    layout: Paths,
    offline: bool = False,
) -> IngestResult:
    """Fetch one source into the landing zone and return its provenance record.

    When ``offline`` is set the existing landing file is reused if present,
    which is how CI and the test suite exercise the pipeline without network
    access.
    """
    layout.ensure()
    landing_path = layout.raw / source.landing_file
    manifest_path = layout.raw / MANIFEST_FILENAME
    previous = _previous_checksum(manifest_path, source.name)
    started = time.monotonic()

    if offline:
        if not landing_path.exists():
            raise IngestionError(
                f"{source.name}: offline mode requested but {landing_path} does not exist"
            )
        checksum = _sha256(landing_path)
        log.info("%-24s cached  %s", source.name, landing_path.name)
        return IngestResult(
            run_id=run_id,
            source_name=source.name,
            url=source.url or "",
            fetched_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
            landing_path=str(landing_path),
            bytes_downloaded=landing_path.stat().st_size,
            sha256=checksum,
            http_status=None,
            attempts=0,
            duration_seconds=round(time.monotonic() - started, 3),
            content_changed=checksum != previous,
            from_cache=True,
        )

    # Download beside the target so the rename is atomic (same filesystem).
    temp_path = landing_path.with_suffix(landing_path.suffix + f".{run_id[:8]}.part")
    try:
        if source.is_local:
            origin = source.local_path()
            if not origin.exists():
                raise IngestionError(f"{source.name}: local file not found: {origin}")
            shutil.copyfile(origin, temp_path)
            status, attempts = None, 1
        else:
            status, attempts = _download_http(source, temp_path)

        checksum = _sha256(temp_path)
        size = temp_path.stat().st_size
        if size == 0:
            raise IngestionError(f"{source.name}: downloaded payload was empty")
        os.replace(temp_path, landing_path)
    finally:
        temp_path.unlink(missing_ok=True)

    changed = checksum != previous
    log.info(
        "%-24s %s %8.1f KiB  sha256=%s  %s",
        source.name,
        "ok " if status in (None, 200) else f"{status}",
        size / 1024,
        checksum[:12],
        "changed" if changed else "unchanged",
    )

    return IngestResult(
        run_id=run_id,
        source_name=source.name,
        url=source.url or "",
        fetched_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        landing_path=str(landing_path),
        bytes_downloaded=size,
        sha256=checksum,
        http_status=status,
        attempts=attempts,
        duration_seconds=round(time.monotonic() - started, 3),
        content_changed=changed,
        from_cache=False,
    )


def append_manifest(results: list[IngestResult], layout: Paths) -> Path:
    """Append this run's provenance records to the manifest."""
    manifest_path = layout.raw / MANIFEST_FILENAME
    with manifest_path.open("a", encoding="utf-8") as handle:
        for result in results:
            handle.write(result.as_json() + "\n")
    return manifest_path


def run(offline: bool = False, sources: list[Source] | None = None) -> list[IngestResult]:
    """Ingest every enabled source. Returns one provenance record per source."""
    layout = paths()
    sources = sources if sources is not None else load_sources()
    run_id = str(uuid.uuid4())
    log.info("ingestion run %s (%d sources, offline=%s)", run_id[:8], len(sources), offline)

    results = [fetch_source(source, run_id, layout, offline=offline) for source in sources]
    manifest_path = append_manifest(results, layout)

    total_kib = sum(r.bytes_downloaded for r in results) / 1024
    log.info(
        "ingested %d sources (%.1f KiB); manifest -> %s",
        len(results),
        total_kib,
        manifest_path.relative_to(layout.data.parent)
        if manifest_path.is_relative_to(layout.data.parent)
        else manifest_path,
    )
    return results
