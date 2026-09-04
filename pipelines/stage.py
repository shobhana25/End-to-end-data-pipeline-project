"""Staging: raw landing files -> cleaned, conformed, typed Parquet.

This is the Python half of the transformation. It does the work that is
awkward or unreadable in SQL:

* **Contract enforcement.** Each landing file is checked against the column
  contract declared in ``config/sources.yml`` before anything else happens, so
  an upstream schema change fails loudly here instead of silently producing a
  wrong number three layers downstream.
* **Parsing free text into structure.** The publisher encodes four separate
  facts inside one indicator string ("Daily ICU occupancy per million").
  :func:`parse_indicator` turns that string into typed attributes, which is
  what lets the SQL layer build a real indicator dimension.
* **Statistical flagging.** Outliers are flagged with a median/MAD modified
  z-score computed per series - a windowed statistic that SQL can express but
  Python expresses more legibly.

The SQL layer that follows is then free to be about *modelling*: surrogate
keys, conformed dimensions, grain and aggregation.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from pipelines.config import (
    LOCATION_OVERRIDES_FILE,
    Paths,
    Source,
    load_sources,
    paths,
)
from pipelines.logging_conf import get_logger

log = get_logger("pipelines.stage")

# A modified z-score above this is flagged for review. Epidemic curves are
# legitimately spiky, so the threshold is deliberately lenient: this flags
# "look at this" rows, it never deletes them.
OUTLIER_Z_THRESHOLD = 10.0


class StagingError(RuntimeError):
    """Raised when a landing file violates its declared contract."""


@dataclass(frozen=True)
class StageResult:
    """Outcome of staging one source."""

    source_name: str
    output_path: Path
    rows_in: int
    rows_out: int
    duplicates_dropped: int
    nulls_dropped: int
    outliers_flagged: int

    @property
    def rows_rejected(self) -> int:
        return self.rows_in - self.rows_out


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

_NON_WORD = re.compile(r"[^0-9a-z]+")


def snake_case(name: str) -> str:
    """Normalise a publisher's column heading to a stable snake_case name.

    ``"Country Name"`` -> ``country_name``; ``"alpha-3"`` -> ``alpha_3``.
    """
    ascii_name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    return _NON_WORD.sub("_", ascii_name.strip().lower()).strip("_")


def enforce_contract(frame: pd.DataFrame, source: Source) -> None:
    """Fail fast if the publisher dropped or renamed a column we depend on."""
    if source.expected_columns:
        missing = [c for c in source.expected_columns if c not in frame.columns]
        if missing:
            raise StagingError(
                f"{source.name}: landing file is missing expected column(s) "
                f"{missing}. Upstream schema changed - review before rerunning."
            )
    if source.min_rows and len(frame) < source.min_rows:
        raise StagingError(
            f"{source.name}: got {len(frame):,} rows, expected at least "
            f"{source.min_rows:,}. Suspected truncated download."
        )


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise StagingError(f"landing file not found: {path} (run `ingest` first)")
    return pd.read_csv(path, dtype=str, keep_default_na=True, na_values=[""])


def flag_outliers(
    frame: pd.DataFrame,
    value_column: str,
    group_columns: list[str],
    threshold: float = OUTLIER_Z_THRESHOLD,
) -> pd.Series:
    """Median/MAD modified z-score flag, computed within each series.

    The MAD is used rather than the standard deviation because a single
    extreme value inflates the standard deviation enough to hide itself.
    Series whose MAD is zero (flat, or too short) are never flagged.
    """
    values = frame[value_column]
    grouped = values.groupby([frame[c] for c in group_columns])
    median = grouped.transform("median")
    absolute_deviation = (values - median).abs()
    mad = absolute_deviation.groupby([frame[c] for c in group_columns]).transform("median")
    modified_z = 0.6745 * (values - median).abs() / mad.where(mad > 0)
    return (modified_z > threshold).fillna(False)


# ---------------------------------------------------------------------------
# Indicator parsing - the free-text -> structure step
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndicatorAttributes:
    """The four facts encoded in one publisher indicator string."""

    indicator_code: str
    base_indicator_code: str
    reporting_frequency: str
    care_setting: str
    metric_type: str
    unit: str
    measure_semantics: str


_FREQUENCIES = {"daily": "Daily", "weekly": "Weekly"}
_CARE_SETTINGS = {"icu": "Intensive care", "hospital": "All hospital"}
_METRIC_TYPES = {"occupancy": "Occupancy", "admissions": "New admissions"}

# Occupancy is a stock: a count of beds filled at a point in time, so it must
# never be summed across dates. Admissions are a flow: a count of events over a
# period, so they may be summed. Recording this on the dimension is what stops
# a dashboard from averaging one and totalling the other by accident.
_SEMANTICS = {
    "Occupancy": "Stock (point-in-time)",
    "New admissions": "Flow (period total)",
}


def parse_indicator(label: str) -> IndicatorAttributes:
    """Decompose an indicator label such as ``"Daily ICU occupancy per million"``.

    Raises :class:`StagingError` on any label the vocabulary does not cover, so
    a new upstream indicator surfaces as a failed run rather than as rows that
    quietly vanish from the model.
    """
    if not label or not str(label).strip():
        raise StagingError("empty indicator label")

    text = str(label).strip()
    lowered = text.lower()

    unit = "Per million" if "per million" in lowered else "Count"
    stem = lowered.replace("per million", "").strip()

    frequency = next((v for k, v in _FREQUENCIES.items() if stem.startswith(k)), None)
    setting = next((v for k, v in _CARE_SETTINGS.items() if k in stem), None)
    metric = next((v for k, v in _METRIC_TYPES.items() if k in stem), None)

    if not (frequency and setting and metric):
        raise StagingError(
            f"unrecognised indicator {label!r} "
            f"(frequency={frequency}, setting={setting}, metric={metric}). "
            "Extend the vocabulary in pipelines/stage.py before rerunning."
        )

    base = snake_case(f"{frequency} {setting} {metric}")
    return IndicatorAttributes(
        indicator_code=snake_case(f"{base} {unit}") if unit == "Per million" else base,
        base_indicator_code=base,
        reporting_frequency=frequency,
        care_setting=setting,
        metric_type=metric,
        unit=unit,
        measure_semantics=_SEMANTICS[metric],
    )


# ---------------------------------------------------------------------------
# Per-source staging
# ---------------------------------------------------------------------------


def stage_hospitalisations(source: Source, layout: Paths) -> StageResult:
    """Clean the hospital/ICU activity fact feed.

    Grain in, grain out: one row per location, date and indicator. The
    indicator string is expanded into typed attributes; nothing is aggregated.
    """
    frame = _read_csv(layout.raw / source.landing_file)
    rows_in = len(frame)
    enforce_contract(frame, source)

    frame = frame.rename(columns={c: snake_case(c) for c in frame.columns})
    frame = frame.rename(
        columns={
            "entity": "location_name",
            "iso_code": "source_location_code",
            "date": "event_date",
            "indicator": "source_indicator_label",
            "value": "metric_value",
        }
    )

    for column in ("location_name", "source_location_code", "source_indicator_label"):
        frame[column] = frame[column].str.strip()

    frame["event_date"] = pd.to_datetime(frame["event_date"], errors="coerce").dt.date
    frame["metric_value"] = pd.to_numeric(frame["metric_value"], errors="coerce")

    key = ["source_location_code", "event_date", "source_indicator_label"]
    before = len(frame)
    frame = frame.dropna(subset=[*key, "metric_value"])
    nulls_dropped = before - len(frame)

    # Deterministic tie-break: on the rare republished row, the last one in
    # file order is the correction, so keep it.
    before = len(frame)
    frame = frame.drop_duplicates(subset=key, keep="last")
    duplicates_dropped = before - len(frame)

    # Free text -> structure. Parse the distinct labels only (8 of them, versus
    # 230k rows) and map back.
    attributes = {
        label: parse_indicator(label) for label in frame["source_indicator_label"].dropna().unique()
    }
    for field in (
        "indicator_code",
        "base_indicator_code",
        "reporting_frequency",
        "care_setting",
        "metric_type",
        "unit",
        "measure_semantics",
    ):
        frame[field] = frame["source_indicator_label"].map(
            {label: getattr(attrs, field) for label, attrs in attributes.items()}
        )

    frame["is_statistical_outlier"] = flag_outliers(
        frame, "metric_value", ["source_location_code", "source_indicator_label"]
    )
    frame["is_negative_value"] = frame["metric_value"] < 0

    columns = [
        "source_location_code",
        "location_name",
        "event_date",
        "source_indicator_label",
        "indicator_code",
        "base_indicator_code",
        "reporting_frequency",
        "care_setting",
        "metric_type",
        "unit",
        "measure_semantics",
        "metric_value",
        "is_statistical_outlier",
        "is_negative_value",
    ]
    frame = frame[columns].sort_values(key).reset_index(drop=True)

    output = layout.staged / "stg_hospital_activity.parquet"
    frame.to_parquet(output, index=False)
    return StageResult(
        source_name=source.name,
        output_path=output,
        rows_in=rows_in,
        rows_out=len(frame),
        duplicates_dropped=duplicates_dropped,
        nulls_dropped=nulls_dropped,
        outliers_flagged=int(frame["is_statistical_outlier"].sum()),
    )


def stage_iso_countries(source: Source, layout: Paths) -> StageResult:
    """Clean the ISO 3166 reference feed into the geography hierarchy."""
    frame = _read_csv(layout.raw / source.landing_file)
    rows_in = len(frame)
    enforce_contract(frame, source)

    frame = frame.rename(columns={c: snake_case(c) for c in frame.columns})
    frame = frame.rename(
        columns={
            "name": "country_name",
            "region": "region",
            "sub_region": "sub_region",
            "intermediate_region": "intermediate_region",
        }
    )

    keep = [
        "country_name",
        "alpha_2",
        "alpha_3",
        "region",
        "sub_region",
        "intermediate_region",
    ]
    frame = frame[keep]
    for column in keep:
        frame[column] = frame[column].str.strip().replace({"": None})

    # Antarctica and a few territories have no UN region assignment. Label them
    # rather than leaving a NULL that would silently drop rows from a join.
    for column in ("region", "sub_region"):
        frame[column] = frame[column].fillna("Unclassified")

    before = len(frame)
    frame = frame.dropna(subset=["alpha_3"])
    nulls_dropped = before - len(frame)

    before = len(frame)
    frame = frame.drop_duplicates(subset=["alpha_3"], keep="first")
    duplicates_dropped = before - len(frame)

    frame = frame.sort_values("alpha_3").reset_index(drop=True)
    output = layout.staged / "stg_iso_countries.parquet"
    frame.to_parquet(output, index=False)
    return StageResult(
        source_name=source.name,
        output_path=output,
        rows_in=rows_in,
        rows_out=len(frame),
        duplicates_dropped=duplicates_dropped,
        nulls_dropped=nulls_dropped,
        outliers_flagged=0,
    )


def stage_population(source: Source, layout: Paths) -> StageResult:
    """Clean the annual population feed.

    The World Bank file interleaves ~50 *aggregates* ("World", "OECD members",
    "Upper middle income") with real countries under the same schema. Summing
    the file without separating them double-counts every person on earth
    several times over, so each row is classified against the ISO 3166
    reference list and flagged. The marts then keep only real countries.
    """
    frame = _read_csv(layout.raw / source.landing_file)
    rows_in = len(frame)
    enforce_contract(frame, source)

    frame = frame.rename(columns={c: snake_case(c) for c in frame.columns})
    frame = frame.rename(
        columns={
            "country_name": "location_name",
            "country_code": "iso_alpha_3",
            "year": "calendar_year",
            "value": "population",
        }
    )

    frame["iso_alpha_3"] = frame["iso_alpha_3"].str.strip()
    frame["location_name"] = frame["location_name"].str.strip()
    frame["calendar_year"] = pd.to_numeric(frame["calendar_year"], errors="coerce").astype("Int64")
    frame["population"] = pd.to_numeric(frame["population"], errors="coerce")

    before = len(frame)
    frame = frame.dropna(subset=["iso_alpha_3", "calendar_year", "population"])
    nulls_dropped = before - len(frame)

    before = len(frame)
    frame = frame.drop_duplicates(subset=["iso_alpha_3", "calendar_year"], keep="last")
    duplicates_dropped = before - len(frame)

    iso_path = layout.staged / "stg_iso_countries.parquet"
    if not iso_path.exists():
        raise StagingError(
            "stg_iso_countries.parquet must be staged before population "
            "(it is the reference list used to identify aggregates)"
        )
    iso_codes = set(pd.read_parquet(iso_path)["alpha_3"])
    frame["is_aggregate"] = ~frame["iso_alpha_3"].isin(iso_codes)

    frame = frame[["iso_alpha_3", "location_name", "calendar_year", "population", "is_aggregate"]]
    frame = frame.sort_values(["iso_alpha_3", "calendar_year"]).reset_index(drop=True)

    output = layout.staged / "stg_population.parquet"
    frame.to_parquet(output, index=False)
    log.debug("population: %d aggregate rows flagged", int(frame["is_aggregate"].sum()))
    return StageResult(
        source_name=source.name,
        output_path=output,
        rows_in=rows_in,
        rows_out=len(frame),
        duplicates_dropped=duplicates_dropped,
        nulls_dropped=nulls_dropped,
        outliers_flagged=0,
    )


def stage_location_overrides(layout: Paths) -> StageResult:
    """Stage the hand-maintained map for publisher codes that are not ISO."""
    frame = pd.read_csv(LOCATION_OVERRIDES_FILE, comment="#", dtype=str)
    frame.columns = [snake_case(c) for c in frame.columns]
    frame["has_population_data"] = frame["has_population_data"].str.strip().str.lower().eq("true")
    output = layout.staged / "stg_location_overrides.parquet"
    frame.to_parquet(output, index=False)
    return StageResult(
        source_name="location_overrides",
        output_path=output,
        rows_in=len(frame),
        rows_out=len(frame),
        duplicates_dropped=0,
        nulls_dropped=0,
        outliers_flagged=0,
    )


# ISO must be staged before population: it is the reference list population is
# classified against.
_HANDLERS = {
    "iso_3166_countries": stage_iso_countries,
    "owid_hospitalisations": stage_hospitalisations,
    "world_bank_population": stage_population,
}


def run(sources: list[Source] | None = None) -> list[StageResult]:
    """Stage every enabled source that has a handler."""
    layout = paths()
    sources = sources if sources is not None else load_sources()
    ordered = sorted(
        (s for s in sources if s.name in _HANDLERS),
        key=lambda s: list(_HANDLERS).index(s.name),
    )

    unhandled = [s.name for s in sources if s.name not in _HANDLERS]
    if unhandled:
        raise StagingError(
            f"no staging handler for enabled source(s): {', '.join(unhandled)}. "
            "Add one in pipelines/stage.py - see docs/adding_a_source.md."
        )

    results = [_HANDLERS[s.name](s, layout) for s in ordered]
    results.append(stage_location_overrides(layout))

    for result in results:
        log.info(
            "%-24s %8d -> %8d rows  (%d dup, %d null, %d outlier flags)",
            result.source_name,
            result.rows_in,
            result.rows_out,
            result.duplicates_dropped,
            result.nulls_dropped,
            result.outliers_flagged,
        )
    return results
