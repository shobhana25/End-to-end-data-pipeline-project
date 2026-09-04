"""Inline-SVG chart builders for the static dashboard.

Charts are rendered server-side into plain SVG with no charting library and no
CDN: the published dashboard is one self-contained HTML file that works
offline, on GitHub Pages, and inside a corporate network that blocks third
party scripts.

Colour comes from CSS custom properties defined once in the page, so both
themes swap in one place and nothing here hardcodes a hex value.

Design rules followed here (and why):
* one y-axis per plot - two measures at different scales become two stacked
  panels rather than a dual axis, which would invent a correlation;
* emphasis over rainbow - a ranked comparison highlights one entity and greys
  the rest instead of spending eight hues on one story;
* thin marks, hairline grid, labels in text ink rather than series colour;
* every mark carries the data needed for its tooltip, so hover works without a
  round trip.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass, field
from datetime import date


def esc(value: object) -> str:
    """Escape a value for use as SVG/HTML text."""
    return html.escape(str(value), quote=True)


def _fmt(value: float, places: int = 2) -> str:
    """Format a number for a label: no trailing zeros, thousands separated."""
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    text = f"{value:,.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


_NICE_MULTIPLES = (1, 2, 2.5, 5, 10)


def _nice_step(value: float) -> float:
    """Round a tick interval up to the nearest 1 / 2 / 2.5 / 5 x 10^n."""
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    magnitude = 10.0**exponent
    for multiple in _NICE_MULTIPLES:
        if magnitude * multiple >= value - 1e-12:
            return magnitude * multiple
    return magnitude * 10


def _nice_axis(value: float, tick_options: tuple[int, ...] = (4, 5)) -> tuple[float, int]:
    """Pick an axis maximum and tick count that give round labels, minimising headroom.

    Choosing the *interval* first and multiplying up is what keeps tick labels
    on round numbers - taking a nice maximum and dividing it by four is what
    produces ticks like 0.38 and 1.12. Trying a couple of tick counts then
    stops a round interval from wasting half the plot.
    """
    if value <= 0:
        return 1.0, tick_options[0]
    candidates = [(_nice_step(value / t) * t, t) for t in tick_options]
    return min(candidates, key=lambda c: c[0])


def _nice_ceiling(value: float) -> float:
    """Axis maximum for a plot that draws no ticks (bar lengths)."""
    return _nice_axis(value)[0]


def _rounded_end_bar(x: float, y: float, width: float, height: float, radius: float) -> str:
    """Horizontal bar path: square at the baseline, rounded at the data end."""
    radius = max(0.0, min(radius, width, height / 2))
    if radius <= 0 or width <= 0:
        return f"M {x} {y} h {max(width, 0)} v {height} h {-max(width, 0)} Z"
    return (
        f"M {x} {y} "
        f"h {width - radius} "
        f"a {radius} {radius} 0 0 1 {radius} {radius} "
        f"v {height - 2 * radius} "
        f"a {radius} {radius} 0 0 1 {-radius} {radius} "
        f"h {-(width - radius)} Z"
    )


# ---------------------------------------------------------------------------
# Time-series panel (small multiple)
# ---------------------------------------------------------------------------


@dataclass
class SeriesPoint:
    """One plotted observation."""

    label: str  # x-axis category, e.g. "2022-01"
    value: float
    tooltip: str = ""


@dataclass
class LinePanel:
    """One panel of a small-multiple time series - a single series, one axis."""

    title: str
    subtitle: str
    points: list[SeriesPoint]
    colour_var: str = "--series-1"
    unit_suffix: str = ""
    width: int = 900
    height: int = 210
    peak_label: str = "peak"
    margin: dict[str, int] = field(
        default_factory=lambda: {"top": 16, "right": 20, "bottom": 30, "left": 52}
    )

    def render(self, panel_id: str) -> str:
        if not self.points:
            return '<p class="empty">No observations for this measure.</p>'

        m = self.margin
        plot_w = self.width - m["left"] - m["right"]
        plot_h = self.height - m["top"] - m["bottom"]
        y_max, ticks = _nice_axis(max(p.value for p in self.points))
        n = len(self.points)
        step = plot_w / max(n - 1, 1)

        def px(index: int) -> float:
            return m["left"] + index * step

        def py(value: float) -> float:
            return m["top"] + plot_h - (value / y_max) * plot_h

        parts: list[str] = [
            f'<svg class="chart" viewBox="0 0 {self.width} {self.height}" '
            f'role="img" preserveAspectRatio="xMidYMid meet" '
            f'aria-label="{esc(self.title)}. {esc(self.subtitle)}">'
        ]

        # Recessive gridlines and y ticks - solid hairlines, one shade off surface.
        for i in range(ticks + 1):
            value = y_max * i / ticks
            y = py(value)
            parts.append(
                f'<line class="grid" x1="{m["left"]}" y1="{y:.1f}" '
                f'x2="{m["left"] + plot_w}" y2="{y:.1f}" />'
            )
            parts.append(
                f'<text class="tick tick-y" x="{m["left"] - 8}" y="{y + 3.5:.1f}" '
                f'text-anchor="end">{_fmt(value)}</text>'
            )

        # X ticks at January of each year, plus the first point.
        seen_years: set[str] = set()
        for index, point in enumerate(self.points):
            year = point.label[:4]
            is_january = point.label.endswith("-01")
            if (is_january and year not in seen_years) or index == 0:
                seen_years.add(year)
                parts.append(
                    f'<text class="tick tick-x" x="{px(index):.1f}" '
                    f'y="{m["top"] + plot_h + 18}" text-anchor="middle">{esc(year)}</text>'
                )

        parts.append(
            f'<line class="axis" x1="{m["left"]}" y1="{m["top"] + plot_h}" '
            f'x2="{m["left"] + plot_w}" y2="{m["top"] + plot_h}" />'
        )

        coordinates = [(px(i), py(p.value)) for i, p in enumerate(self.points)]
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coordinates)
        area = (
            f"M {coordinates[0][0]:.1f} {m['top'] + plot_h} L "
            + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coordinates)
            + f" L {coordinates[-1][0]:.1f} {m['top'] + plot_h} Z"
        )
        parts.append(f'<path d="{area}" fill="var({self.colour_var})" opacity="0.10" />')
        parts.append(
            f'<path d="{path}" fill="none" stroke="var({self.colour_var})" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />'
        )

        # Selective direct label: the peak only. A value on every point is noise.
        peak_index = max(range(n), key=lambda i: self.points[i].value)
        peak = self.points[peak_index]
        peak_x, peak_y = coordinates[peak_index]
        parts.append(
            f'<circle cx="{peak_x:.1f}" cy="{peak_y:.1f}" r="4" '
            f'fill="var({self.colour_var})" stroke="var(--surface-1)" stroke-width="2" />'
        )
        anchor = "end" if peak_index > n * 0.75 else "start"
        offset = -8 if anchor == "end" else 8
        parts.append(
            f'<text class="peak-label" x="{peak_x + offset:.1f}" y="{peak_y - 10:.1f}" '
            f'text-anchor="{anchor}">{esc(self.peak_label)} '
            f"{_fmt(peak.value)}{esc(self.unit_suffix)} · {esc(peak.label)}</text>"
        )

        parts.append("</svg>")

        hover_data = json.dumps(
            {
                "left": m["left"],
                "top": m["top"],
                "plotWidth": plot_w,
                "plotHeight": plot_h,
                "step": step,
                "points": [
                    {
                        "x": round(x, 1),
                        "y": round(y, 1),
                        "label": p.label,
                        "value": _fmt(p.value) + self.unit_suffix,
                        "tip": p.tooltip,
                    }
                    for (x, y), p in zip(coordinates, self.points, strict=True)
                ],
            }
        )

        return (
            f'<figure class="panel" id="{esc(panel_id)}">'
            f'<figcaption><span class="panel-title">'
            f'<span class="swatch" style="background:var({self.colour_var})"></span>'
            f"{esc(self.title)}</span>"
            f'<span class="panel-sub">{esc(self.subtitle)}</span></figcaption>'
            f'<div class="chart-wrap" data-hover="line" data-series=\'{esc(hover_data)}\'>'
            f"{''.join(parts)}"
            f'<div class="tooltip" hidden></div></div>'
            f"</figure>"
        )


# ---------------------------------------------------------------------------
# Ranked horizontal bar chart
# ---------------------------------------------------------------------------


@dataclass
class BarRow:
    """One bar: a label, a value, and whether it is the emphasised entity."""

    label: str
    value: float
    highlight: bool = False
    annotation: str = ""
    tooltip: str = ""
    colour_var: str | None = None  # set to use an ordinal ramp instead of emphasis


def bar_chart(
    rows: list[BarRow],
    *,
    unit_suffix: str = "",
    width: int = 900,
    row_height: int = 26,
    label_width: int = 150,
    value_width: int = 92,
) -> str:
    """Ranked horizontal bars.

    Default colouring is *emphasis*: the highlighted entity takes the series
    hue and everything else is neutral, so the reader's eye lands on the one
    comparison the chart is making. A row may override with ``colour_var`` when
    the categories are genuinely ordered (an ordinal ramp).
    """
    if not rows:
        return '<p class="empty">No rows to chart.</p>'

    height = row_height * len(rows) + 8
    plot_w = width - label_width - value_width
    v_max = _nice_ceiling(max(r.value for r in rows))
    bar_h = row_height - 10  # 2px+ surface gap between adjacent bars

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="xMidYMid meet" aria-label="Ranked comparison">'
    ]

    for index, row in enumerate(rows):
        y = 4 + index * row_height
        bar_w = (row.value / v_max) * plot_w if v_max else 0
        colour = (
            f"var({row.colour_var})"
            if row.colour_var
            else ("var(--series-1)" if row.highlight else "var(--neutral-mark)")
        )
        classes = "bar" + (" bar-highlight" if row.highlight else "")
        tooltip = row.tooltip or f"{row.label}: {_fmt(row.value)}{unit_suffix}"
        parts.append(
            f'<g class="{classes}" data-tip="{esc(tooltip)}">'
            f'<text class="bar-label{" is-highlight" if row.highlight else ""}" '
            f'x="{label_width - 10}" y="{y + bar_h / 2 + 4:.1f}" text-anchor="end">'
            f"{esc(row.label)}</text>"
            f'<path d="{_rounded_end_bar(label_width, y, bar_w, bar_h, 4)}" fill="{colour}" />'
            f'<rect class="bar-hit" x="{label_width}" y="{y - 3}" '
            f'width="{plot_w}" height="{row_height}" fill="transparent" />'
            f'<text class="bar-value" x="{label_width + bar_w + 8:.1f}" '
            f'y="{y + bar_h / 2 + 4:.1f}">{_fmt(row.value)}{esc(unit_suffix)}'
            f"{(' ' + esc(row.annotation)) if row.annotation else ''}</text>"
            f"</g>"
        )

    parts.append("</svg>")
    return (
        f'<div class="chart-wrap" data-hover="bar">{"".join(parts)}'
        f'<div class="tooltip" hidden></div></div>'
    )


def month_label(value: date | str) -> str:
    """Normalise a month key to ``YYYY-MM``."""
    return value[:7] if isinstance(value, str) else value.strftime("%Y-%m")
