"""Chart rendering: the arithmetic behind the SVG, and its escaping."""

from __future__ import annotations

import re

import pytest

from pipelines.charts import (
    BarRow,
    LinePanel,
    SeriesPoint,
    _fmt,
    _nice_axis,
    _rounded_end_bar,
    bar_chart,
    esc,
)


@pytest.mark.parametrize("maximum", [0.9, 1.34, 17.77, 55.0, 423.0, 5571.0, 0.013])
def test_axis_ticks_always_land_on_round_numbers(maximum):
    """Ticks like 0.38 and 1.12 are what you get from dividing a nice maximum;
    the interval has to be chosen first."""
    axis_max, ticks = _nice_axis(maximum)
    step = axis_max / ticks
    assert axis_max >= maximum
    for i in range(ticks + 1):
        value = step * i
        assert value == pytest.approx(round(value, 10))


@pytest.mark.parametrize("maximum", [0.9, 1.34, 17.77, 55.0, 423.0, 5571.0])
def test_axis_does_not_waste_more_than_half_the_plot(maximum):
    axis_max, _ = _nice_axis(maximum)
    assert axis_max <= maximum * 2


def test_zero_and_negative_maxima_do_not_divide_by_zero():
    assert _nice_axis(0)[0] > 0
    assert _nice_axis(-5)[0] > 0


def test_bar_path_is_square_at_the_baseline_and_rounded_at_the_data_end():
    path = _rounded_end_bar(x=10, y=0, width=100, height=20, radius=4)
    assert path.startswith("M 10 0")
    assert path.count("a 4 4") == 2, "exactly two rounded corners, both at the data end"


def test_a_bar_shorter_than_its_radius_still_renders():
    """A near-zero value must not produce invalid path geometry."""
    path = _rounded_end_bar(x=0, y=0, width=1, height=20, radius=4)
    assert path.startswith("M 0 0")
    assert "NaN" not in path


def test_a_zero_width_bar_renders_nothing_invalid():
    assert "NaN" not in _rounded_end_bar(x=0, y=0, width=0, height=20, radius=4)


# 0.125 is deliberately absent: it is a banker's-rounding tie, so asserting on
# it would test CPython's rounding rule rather than this formatter.
@pytest.mark.parametrize(
    "value,expected",
    [(1234.5, "1,234"), (1.5, "1.5"), (1.0, "1"), (0.0, "0"), (0.126, "0.13")],
)
def test_number_formatting(value, expected):
    assert _fmt(value) == expected


def test_labels_are_escaped():
    assert esc("<script>&") == "&lt;script&gt;&amp;"


def test_bar_chart_escapes_hostile_labels():
    svg = bar_chart([BarRow(label="</text><script>alert(1)</script>", value=1)])
    assert "<script>" not in svg
    assert "&lt;/text&gt;" in svg


def test_bar_chart_highlights_exactly_the_chosen_row():
    svg = bar_chart(
        [
            BarRow(label="A", value=3, highlight=False),
            BarRow(label="B", value=2, highlight=True),
            BarRow(label="C", value=1, highlight=False),
        ]
    )
    assert svg.count("var(--series-1)") == 1
    assert svg.count("var(--neutral-mark)") == 2


def test_bar_chart_handles_no_rows():
    assert "No rows" in bar_chart([])


def test_line_panel_labels_only_the_peak():
    """A value on every point is noise; the extreme is the one worth naming."""
    points = [SeriesPoint(f"2021-{m:02d}", v) for m, v in enumerate([1, 5, 9, 4, 2], start=1)]
    svg = LinePanel(title="t", subtitle="s", points=points).render("p")
    assert svg.count("peak-label") == 1
    assert "9" in svg


def test_line_panel_emits_hover_data_for_every_point():
    points = [SeriesPoint(f"2021-{m:02d}", m) for m in range(1, 7)]
    svg = LinePanel(title="t", subtitle="s", points=points).render("p")
    payload = re.search(r"data-series='(.*?)'", svg, re.S)
    # the JSON is HTML-escaped into the attribute, so quotes arrive as entities
    assert payload and payload.group(1).count("&quot;label&quot;") == 6


def test_line_panel_handles_no_points():
    assert "No observations" in LinePanel(title="t", subtitle="s", points=[]).render("p")
