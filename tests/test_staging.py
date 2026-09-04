"""Unit tests for the Python cleaning layer.

The indicator parser gets the most attention here: it is the step that turns a
free-text publisher label into the dimension attributes the whole model rests
on, and a silent mis-parse would propagate into every aggregation.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pipelines.config import Source
from pipelines.stage import (
    StagingError,
    enforce_contract,
    flag_outliers,
    parse_indicator,
    snake_case,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Country Name", "country_name"),
        ("alpha-3", "alpha_3"),
        ("  Mixed CASE  ", "mixed_case"),
        ("intermediate-region-code", "intermediate_region_code"),
        ("Café", "cafe"),  # accents fold to their base letter, not dropped
    ],
)
def test_snake_case(raw, expected):
    assert snake_case(raw) == expected


@pytest.mark.parametrize(
    "label,frequency,setting,metric,unit,semantics",
    [
        (
            "Daily ICU occupancy",
            "Daily",
            "Intensive care",
            "Occupancy",
            "Count",
            "Stock (point-in-time)",
        ),
        (
            "Daily ICU occupancy per million",
            "Daily",
            "Intensive care",
            "Occupancy",
            "Per million",
            "Stock (point-in-time)",
        ),
        (
            "Daily hospital occupancy",
            "Daily",
            "All hospital",
            "Occupancy",
            "Count",
            "Stock (point-in-time)",
        ),
        (
            "Weekly new ICU admissions",
            "Weekly",
            "Intensive care",
            "New admissions",
            "Count",
            "Flow (period total)",
        ),
        (
            "Weekly new hospital admissions per million",
            "Weekly",
            "All hospital",
            "New admissions",
            "Per million",
            "Flow (period total)",
        ),
    ],
)
def test_parse_indicator_decomposes_every_published_label(
    label, frequency, setting, metric, unit, semantics
):
    attributes = parse_indicator(label)
    assert attributes.reporting_frequency == frequency
    assert attributes.care_setting == setting
    assert attributes.metric_type == metric
    assert attributes.unit == unit
    assert attributes.measure_semantics == semantics


def test_count_and_rate_variants_share_a_base_indicator():
    """The unit is pivoted out of the model, so both variants must agree on the
    measure they describe - otherwise the fact pivot would split them."""
    count = parse_indicator("Daily ICU occupancy")
    rate = parse_indicator("Daily ICU occupancy per million")
    assert count.base_indicator_code == rate.base_indicator_code
    assert count.indicator_code != rate.indicator_code


def test_occupancy_is_a_stock_and_admissions_are_a_flow():
    """This distinction decides whether a measure may be summed over time."""
    assert parse_indicator("Daily ICU occupancy").measure_semantics.startswith("Stock")
    assert parse_indicator("Weekly new ICU admissions").measure_semantics.startswith("Flow")


@pytest.mark.parametrize(
    "label", ["", "   ", "Monthly ward throughput", "Daily occupancy", "ICU occupancy"]
)
def test_unknown_indicator_labels_fail_loudly(label):
    """A new upstream indicator must break the run, not vanish from the model."""
    with pytest.raises(StagingError):
        parse_indicator(label)


def _source(**overrides) -> Source:
    defaults = {
        "name": "demo",
        "title": "demo",
        "publisher": "p",
        "licence": "l",
        "url": "http://x",
        "format": "csv",
        "landing_file": "x.csv",
        "enabled": True,
        "role": "fact",
        "expected_columns": ["a", "b"],
        "min_rows": 2,
    }
    defaults.update(overrides)
    return Source(**defaults)


def test_contract_accepts_a_conforming_frame():
    frame = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    enforce_contract(frame, _source())


def test_contract_rejects_a_renamed_column():
    frame = pd.DataFrame({"a": [1, 2], "renamed": [3, 4]})
    with pytest.raises(StagingError, match="missing expected column"):
        enforce_contract(frame, _source())


def test_contract_rejects_a_truncated_download():
    frame = pd.DataFrame({"a": [1], "b": [2]})
    with pytest.raises(StagingError, match="truncated"):
        enforce_contract(frame, _source(min_rows=100))


def test_outlier_flag_is_scoped_to_its_own_series():
    """A value that is extreme for one country must not flag another country's
    rows, so the statistic is computed within each series."""
    frame = pd.DataFrame(
        {
            "group": ["a"] * 10 + ["b"] * 10,
            "value": [
                *[10, 11, 10, 12, 11, 10, 11, 12, 10, 9000],
                *[1000, 1100, 1050, 1010, 1090, 1020, 1080, 1030, 1070, 1040],
            ],
        }
    )
    flags = flag_outliers(frame, "value", ["group"])
    assert flags.tolist()[:9] == [False] * 9
    assert flags.tolist()[9] is True or bool(flags.iloc[9])
    assert not flags[frame["group"] == "b"].any()


def test_flat_series_is_never_flagged():
    """A zero MAD would divide by zero; those series must simply not flag."""
    frame = pd.DataFrame({"group": ["a"] * 5, "value": [7, 7, 7, 7, 7]})
    assert not flag_outliers(frame, "value", ["group"]).any()
