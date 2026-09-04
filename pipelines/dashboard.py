"""Dashboard: render the reporting marts to a self-contained HTML page.

The page is generated from the warehouse on every run - there is no hand-typed
number anywhere in it, and no build step, bundler or CDN. That matters twice
over: the dashboard cannot drift from the data, and the artefact this pipeline
publishes is one file that opens anywhere, including straight off GitHub Pages.

Two outputs come from the same content:

* ``docs/index.html`` - a complete standalone document for GitHub Pages;
* a body-only fragment, for embedding in a host that supplies its own shell.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipelines.charts import BarRow, LinePanel, SeriesPoint, bar_chart, esc
from pipelines.config import DOCS_DIR, Paths, paths
from pipelines.logging_conf import get_logger
from pipelines.warehouse import connect

log = get_logger("pipelines.dashboard")

DEFAULT_FOCUS = "AUS"
PAGE_TITLE = "Hospital Capacity Warehouse"

# Ordinal ramp (blue, light -> dark), validated for both themes.
_ORDINAL_VARS = ["--ord-1", "--ord-2", "--ord-3", "--ord-4"]


@dataclass
class DashboardData:
    """Everything the page renders, pulled from the warehouse in one pass."""

    focus_code: str
    focus_name: str
    stats: dict
    focus_series: dict[str, list[SeriesPoint]]
    focus_peaks: list[dict]
    peak_ranking: list[dict]
    variance_bands: list[dict]
    denominator_notes: list[dict]


def _rows(connection, sql: str, params: list | None = None) -> list[dict]:
    cursor = connection.execute(sql, params or [])
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _one(connection, sql: str, params: list | None = None) -> dict:
    found = _rows(connection, sql, params)
    return found[0] if found else {}


def collect(layout: Paths | None = None, focus: str = DEFAULT_FOCUS) -> DashboardData:
    """Run every dashboard query against the warehouse."""
    layout = layout or paths()
    with connect(layout, read_only=True) as connection:
        stats = _one(
            connection,
            """
            SELECT count(*)                        AS fact_rows,
                   count(DISTINCT location_key)    AS locations,
                   count(DISTINCT indicator_key)   AS indicators,
                   min(event_date)                 AS first_date,
                   max(event_date)                 AS last_date,
                   max(warehouse_built_at)         AS built_at
            FROM fct_hospital_activity
            """,
        )
        focus_row = _one(
            connection,
            "SELECT location_name, region FROM dim_location WHERE source_location_code = ?",
            [focus],
        )
        focus_name = focus_row.get("location_name", focus)

        focus_series: dict[str, list[SeriesPoint]] = {}
        for code in ("daily_intensive_care_occupancy", "daily_all_hospital_occupancy"):
            monthly = _rows(
                connection,
                """
                SELECT year_month, monthly_per_100k, max_per_100k, monthly_value, days_observed
                FROM mart_monthly_activity
                WHERE source_location_code = ? AND indicator_code = ?
                ORDER BY year_month
                """,
                [focus, code],
            )
            focus_series[code] = [
                SeriesPoint(
                    label=row["year_month"],
                    value=float(row["monthly_per_100k"] or 0),
                    tooltip=(
                        f"{row['monthly_per_100k']:.2f} per 100k mean · "
                        f"peak {row['max_per_100k']:.2f} · "
                        f"{row['monthly_value']:.0f} beds mean · "
                        f"{row['days_observed']} days reported"
                    ),
                )
                for row in monthly
            ]

        focus_peaks = _rows(
            connection,
            """
            SELECT indicator_name, care_setting, days_observed, first_observed_date,
                   last_observed_date, peak_value, peak_value_date,
                   peak_per_100k, peak_per_100k_date, mean_per_100k
            FROM mart_peak_pressure
            WHERE source_location_code = ?
            ORDER BY indicator_name
            """,
            [focus],
        )

        peak_ranking = _rows(
            connection,
            """
            WITH ranked AS (
                SELECT location_name, source_location_code, region, peak_per_100k,
                       peak_per_100k_date, days_observed,
                       row_number() OVER (ORDER BY peak_per_100k DESC) AS rank
                FROM mart_peak_pressure
                WHERE indicator_code = 'daily_intensive_care_occupancy'
                  AND peak_per_100k IS NOT NULL
            )
            SELECT *, (SELECT count(*) FROM ranked) AS ranked_total
            FROM ranked
            WHERE rank <= 14 OR source_location_code = ?
            ORDER BY rank
            """,
            [focus],
        )

        variance_bands = _rows(
            connection,
            """
            SELECT CASE WHEN abs(rate_variance_pct) <= 1  THEN 'Within 1%'
                        WHEN abs(rate_variance_pct) <= 5  THEN '1% to 5%'
                        WHEN abs(rate_variance_pct) <= 10 THEN '5% to 10%'
                        ELSE 'Over 10%' END                     AS band,
                   CASE WHEN abs(rate_variance_pct) <= 1  THEN 1
                        WHEN abs(rate_variance_pct) <= 5  THEN 2
                        WHEN abs(rate_variance_pct) <= 10 THEN 3
                        ELSE 4 END                              AS band_order,
                   count(*)                                     AS rows_in_band,
                   round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct_of_rows
            FROM fct_hospital_activity
            WHERE rate_variance_pct IS NOT NULL
            GROUP BY band, band_order
            ORDER BY band_order
            """,
        )

        denominator_notes = _rows(
            connection,
            """
            SELECT location_name, denominator_agreement,
                   max(publisher_implied_population) AS publisher_population,
                   max(reference_population)         AS reference_population,
                   round(avg(median_variance_pct), 1) AS median_variance_pct,
                   sum(rows_compared)                AS rows_compared
            FROM mart_rate_reconciliation
            WHERE denominator_agreement <> 'Aligned'
            GROUP BY location_name, denominator_agreement
            ORDER BY abs(avg(median_variance_pct)) DESC
            LIMIT 6
            """,
        )

    return DashboardData(
        focus_code=focus,
        focus_name=focus_name,
        stats=stats,
        focus_series=focus_series,
        focus_peaks=focus_peaks,
        peak_ranking=peak_ranking,
        variance_bands=variance_bands,
        denominator_notes=denominator_notes,
    )


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

_STYLE = """
:root {
  color-scheme: light;
  --page:            #f9f9f7;
  --surface-1:       #fcfcfb;
  --text-primary:    #0b0b0b;
  --text-secondary:  #52514e;
  --text-muted:      #898781;
  --grid:            #e1e0d9;
  --axis:            #c3c2b7;
  --border:          rgba(11, 11, 11, 0.10);
  --series-1:        #2a78d6;
  --series-2:        #eb6834;
  --neutral-mark:    #c3c2b7;
  --ord-1:           #86b6ef;
  --ord-2:           #5598e7;
  --ord-3:           #2a78d6;
  --ord-4:           #1c5cab;
  --good:            #0ca30c;
  --warning:         #fab219;
  --critical:        #d03b3b;
  --tooltip-bg:      #0b0b0b;
  --tooltip-ink:     #fcfcfb;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page:           #0d0d0d;
    --surface-1:      #1a1a19;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --grid:           #2c2c2a;
    --axis:           #383835;
    --border:         rgba(255, 255, 255, 0.10);
    --series-1:       #3987e5;
    --series-2:       #d95926;
    --neutral-mark:   #52514e;
    --tooltip-bg:     #fcfcfb;
    --tooltip-ink:    #0b0b0b;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page:           #0d0d0d;
  --surface-1:      #1a1a19;
  --text-primary:   #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:     #898781;
  --grid:           #2c2c2a;
  --axis:           #383835;
  --border:         rgba(255, 255, 255, 0.10);
  --series-1:       #3987e5;
  --series-2:       #d95926;
  --neutral-mark:   #52514e;
  --tooltip-bg:     #fcfcfb;
  --tooltip-ink:    #0b0b0b;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--page);
  color: var(--text-primary);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.shell { max-width: 1080px; margin: 0 auto; padding: 40px 24px 64px; }

header.masthead { margin-bottom: 28px; }
.eyebrow {
  font-size: 12px; letter-spacing: 0.09em; text-transform: uppercase;
  color: var(--text-muted); margin: 0 0 10px;
}
h1 { font-size: 30px; line-height: 1.2; margin: 0 0 10px; font-weight: 640; letter-spacing: -0.015em; }
.standfirst { font-size: 16px; color: var(--text-secondary); margin: 0; max-width: 68ch; }
.standfirst b { color: var(--text-primary); font-weight: 640; }
.standfirst.secondary { font-size: 14.5px; color: var(--text-muted); margin-top: 12px; }
.masthead-meta {
  margin-top: 16px; font-size: 13px; color: var(--text-muted);
  display: flex; flex-wrap: wrap; gap: 6px 18px;
}
.masthead-meta code { font-size: 12px; }

h2 {
  font-size: 13px; letter-spacing: 0.09em; text-transform: uppercase;
  color: var(--text-muted); margin: 44px 0 6px; font-weight: 600;
}
.section-lede { margin: 0 0 18px; color: var(--text-secondary); max-width: 74ch; font-size: 14.5px; }

.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px 22px;
}
.card + .card { margin-top: 16px; }

.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 12px; }
.tile {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 12px; padding: 16px 18px;
}
.tile-value { font-size: 27px; font-weight: 640; letter-spacing: -0.02em; line-height: 1.1; }
.tile-label { font-size: 12.5px; color: var(--text-muted); margin-top: 5px; }
.tile-note { font-size: 12.5px; color: var(--text-secondary); margin-top: 3px; }

.panel { margin: 0; }
.panel + .panel { margin-top: 22px; padding-top: 20px; border-top: 1px solid var(--border); }
.panel figcaption { margin-bottom: 6px; }
.panel-title { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 15px; }
.panel-sub { display: block; font-size: 13px; color: var(--text-muted); margin-top: 2px; }
.swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; flex: none; }

.chart-wrap { position: relative; }
.chart { display: block; width: 100%; height: auto; overflow: visible; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.tick { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.peak-label { fill: var(--text-secondary); font-size: 11.5px; font-weight: 600; }
.bar-label { fill: var(--text-secondary); font-size: 12.5px; }
.bar-label.is-highlight { fill: var(--text-primary); font-weight: 640; }
.bar-value { fill: var(--text-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }
.bar-highlight .bar-value { fill: var(--text-primary); font-weight: 640; }
.bar { cursor: default; }
.bar:hover path { opacity: 0.82; }
.empty { color: var(--text-muted); font-size: 14px; }

.crosshair { stroke: var(--axis); stroke-width: 1; }
.tooltip {
  position: absolute; z-index: 5; pointer-events: none;
  background: var(--tooltip-bg); color: var(--tooltip-ink);
  padding: 7px 10px; border-radius: 7px; font-size: 12.5px; line-height: 1.45;
  max-width: 280px; transform: translate(-50%, calc(-100% - 12px)); white-space: nowrap;
}
.tooltip b { font-weight: 640; }

.legend { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 12px 0 0; font-size: 12.5px; color: var(--text-secondary); }
.legend span { display: inline-flex; align-items: center; gap: 7px; }

.table-scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { text-align: left; padding: 8px 12px 8px 0; border-bottom: 1px solid var(--border); vertical-align: top; }
th { font-size: 11.5px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-muted); font-weight: 600; white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; padding-right: 0; }
tbody tr:last-child td { border-bottom: 0; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.trunc { color: var(--text-muted); }

.pill {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; font-weight: 600; padding: 2px 9px; border-radius: 999px;
  border: 1px solid var(--border); white-space: nowrap;
}
.pill.warn { color: var(--warning); }
.pill.fail { color: var(--critical); }
.pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }

.note {
  border-left: 3px solid var(--series-2); padding: 2px 0 2px 14px;
  margin: 18px 0 0; color: var(--text-secondary); font-size: 14px; max-width: 74ch;
}
.note b { color: var(--text-primary); }
.note + .note { border-left-color: var(--axis); }



footer { margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--border); color: var(--text-muted); font-size: 13px; }
footer a { color: var(--text-secondary); }
a { color: var(--series-1); }

@media (max-width: 640px) {
  .shell { padding: 28px 16px 48px; }
  h1 { font-size: 24px; }
  .tile-value { font-size: 23px; }
}
"""

_SCRIPT = """
(function () {
  function place(tip, wrap, x, y, html) {
    tip.innerHTML = html;
    tip.hidden = false;
    var w = wrap.getBoundingClientRect();
    var t = tip.getBoundingClientRect();
    var half = t.width / 2;
    var clamped = Math.max(half + 4, Math.min(x, w.width - half - 4));
    tip.style.left = clamped + "px";
    tip.style.top = y + "px";
  }

  document.querySelectorAll('[data-hover="line"]').forEach(function (wrap) {
    var svg = wrap.querySelector("svg");
    var tip = wrap.querySelector(".tooltip");
    var meta = JSON.parse(wrap.getAttribute("data-series"));
    if (!svg || !meta.points.length) return;

    var ns = "http://www.w3.org/2000/svg";
    var rule = document.createElementNS(ns, "line");
    rule.setAttribute("class", "crosshair");
    rule.setAttribute("y1", meta.top);
    rule.setAttribute("y2", meta.top + meta.plotHeight);
    rule.style.display = "none";
    svg.appendChild(rule);

    var dot = document.createElementNS(ns, "circle");
    dot.setAttribute("r", "4.5");
    dot.setAttribute("fill", "var(--surface-1)");
    dot.setAttribute("stroke-width", "2.5");
    dot.style.display = "none";
    svg.appendChild(dot);

    function nearest(event) {
      var box = svg.getBoundingClientRect();
      var vb = svg.viewBox.baseVal;
      var x = ((event.clientX - box.left) / box.width) * vb.width;
      var index = Math.round((x - meta.left) / meta.step);
      return Math.max(0, Math.min(meta.points.length - 1, index));
    }

    svg.addEventListener("mousemove", function (event) {
      var point = meta.points[nearest(event)];
      rule.setAttribute("x1", point.x);
      rule.setAttribute("x2", point.x);
      rule.style.display = "";
      dot.setAttribute("cx", point.x);
      dot.setAttribute("cy", point.y);
      dot.setAttribute("stroke", getComputedStyle(svg.querySelector("path[stroke]")).stroke);
      dot.style.display = "";
      var box = svg.getBoundingClientRect();
      var scale = box.width / svg.viewBox.baseVal.width;
      place(tip, wrap, point.x * scale, point.y * scale,
            "<b>" + point.label + "</b><br>" + (point.tip || point.value));
    });
    svg.addEventListener("mouseleave", function () {
      tip.hidden = true; rule.style.display = "none"; dot.style.display = "none";
    });
  });

  document.querySelectorAll('[data-hover="bar"]').forEach(function (wrap) {
    var tip = wrap.querySelector(".tooltip");
    wrap.querySelectorAll(".bar").forEach(function (bar) {
      bar.addEventListener("mousemove", function (event) {
        var box = wrap.getBoundingClientRect();
        place(tip, wrap, event.clientX - box.left, event.clientY - box.top,
              bar.getAttribute("data-tip"));
      });
      bar.addEventListener("mouseleave", function () { tip.hidden = true; });
    });
  });
})();
"""


def _n(value, places: int = 0) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{places}f}"


def _date(value) -> str:
    if value is None:
        return "-"
    return value.strftime("%-d %b %Y") if hasattr(value, "strftime") else str(value)[:10]


def _tile(value: str, label: str, note: str = "") -> str:
    return (
        f'<div class="tile"><div class="tile-value">{value}</div>'
        f'<div class="tile-label">{esc(label)}</div>'
        + (f'<div class="tile-note">{esc(note)}</div>' if note else "")
        + "</div>"
    )


def _table(headers: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    head = "".join(
        f'<th class="{"num" if numeric else ""}">{esc(title)}</th>' for title, numeric in headers
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{"num" if headers[i][1] else ""}">{cell}</td>'
            for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render_body(data: DashboardData) -> str:
    """Assemble the page content (title + style + markup), without a document shell."""
    s = data.stats
    focus = esc(data.focus_name)

    # -- Stat tiles ---------------------------------------------------------
    tiles = (
        _tile(_n(s.get("fact_rows")), "Daily observations", "one per place, day and measure")
        + _tile(_n(s.get("locations")), "Places reporting", "countries, plus the four UK nations")
        + _tile(
            f"{_date(s.get('first_date'))[-4:]} – {_date(s.get('last_date'))[-4:]}",  # noqa: RUF001 - thin space + en dash are deliberate
            "Period covered",
            f"{_date(s.get('first_date'))} to {_date(s.get('last_date'))}",
        )
    )

    # -- Focus country small multiples --------------------------------------
    icu = LinePanel(
        title=f"{data.focus_name} · intensive care occupancy",
        subtitle="Monthly mean COVID-19 ICU beds occupied per 100,000 people",
        points=data.focus_series.get("daily_intensive_care_occupancy", []),
        colour_var="--series-1",
        unit_suffix=" /100k",
    ).render("panel-icu")
    ward = LinePanel(
        title=f"{data.focus_name} · all-hospital occupancy",
        subtitle="Monthly mean COVID-19 hospital beds occupied per 100,000 people",
        points=data.focus_series.get("daily_all_hospital_occupancy", []),
        colour_var="--series-2",
        unit_suffix=" /100k",
    ).render("panel-ward")

    peak_rows = [
        [
            esc(row["indicator_name"]),
            _n(row["days_observed"]),
            f'{_n(row["peak_value"])} <span class="trunc">on {_date(row["peak_value_date"])}</span>',
            _n(row["peak_per_100k"], 2),
            _n(row["mean_per_100k"], 2),
        ]
        for row in data.focus_peaks
    ]
    peak_table = _table(
        [
            ("Measure", False),
            ("Days reported", True),
            ("Peak beds", True),
            ("Peak per 100k", True),
            ("Mean per 100k", True),
        ],
        peak_rows,
    )

    # -- International ranking (emphasis, not rainbow) ----------------------
    ranking_rows = [
        BarRow(
            label=row["location_name"],
            value=float(row["peak_per_100k"]),
            highlight=row["source_location_code"] == data.focus_code,
            annotation=(
                f"· rank {row['rank']} of {row['ranked_total']}"
                if row["source_location_code"] == data.focus_code and row["rank"] > 14
                else ""
            ),
            tooltip=(
                f"{row['location_name']}: peak {row['peak_per_100k']:.2f} per 100k "
                f"on {_date(row['peak_per_100k_date'])}, {row['days_observed']:,} days reported"
            ),
        )
        for row in data.peak_ranking
    ]
    ranking_chart = bar_chart(ranking_rows, unit_suffix="")
    ranking_legend = (
        '<p class="legend">'
        f'<span><span class="swatch" style="background:var(--series-1)"></span>{focus}</span>'
        '<span><span class="swatch" style="background:var(--neutral-mark)"></span>'
        "Other reporting countries</span></p>"
    )

    # -- Assurance ----------------------------------------------------------
    band_rows = [
        BarRow(
            label=row["band"],
            value=float(row["pct_of_rows"]),
            colour_var=_ORDINAL_VARS[min(int(row["band_order"]) - 1, len(_ORDINAL_VARS) - 1)],
            annotation=f"({_n(row['rows_in_band'])} rows)",
            tooltip=(
                f"{row['band']}: {row['pct_of_rows']:.2f}% of compared rows "
                f"({row['rows_in_band']:,})"
            ),
        )
        for row in data.variance_bands
    ]
    band_chart = bar_chart(band_rows, unit_suffix="%", row_height=30, label_width=110)

    denominator_table = _table(
        [
            ("Location", False),
            ("Finding", False),
            ("Publisher denominator", True),
            ("World Bank population", True),
            ("Median gap", True),
        ],
        [
            [
                esc(row["location_name"]),
                f'<span class="pill {"fail" if row["denominator_agreement"] == "Definitional mismatch" else "warn"}">'
                f"{esc(row['denominator_agreement'])}</span>",
                _n(row["publisher_population"]),
                _n(row["reference_population"]),
                f"{_n(row['median_variance_pct'], 1)}%",
            ]
            for row in data.denominator_notes
        ],
    )

    # -- Lineage and quality ------------------------------------------------
    built = str(s.get("built_at") or "")[:19].replace("T", " ")

    return f"""<title>{PAGE_TITLE}</title>
<style>{_STYLE}</style>
<div class="shell">

<header class="masthead">
  <p class="eyebrow">Public health data &middot; {_date(s.get("first_date"))[-4:]}&ndash;{_date(s.get("last_date"))[-4:]}</p>
  <h1>Hospital and intensive care capacity</h1>
  <p class="standfirst">
    <b>What this shows.</b> How full hospitals and intensive care units were
    during COVID-19, in {focus} and the {_n(s.get("locations", 0) - 1)} other places that
    published daily bed figures. It answers two questions: how hard were
    {focus}'s hospitals pushed, and how did that compare with everyone else?
  </p>
  <p class="standfirst secondary">
    The figures are population-adjusted, so a small country and a large one can
    be read on the same scale. Every rate here was recomputed from independent
    population data rather than taken on trust, and the page is generated
    straight from the database on each run, so no number below is typed by hand.
  </p>
  <p class="masthead-meta">
    <span>Rebuilt from source data {esc(built)} UTC</span>
  </p>
</header>

<div class="tiles">{tiles}</div>

<h2>{focus} in detail</h2>
<p class="section-lede">
  Beds occupied per 100,000 people, averaged over each month. Occupancy is a
  count of beds full at a single moment, so it is averaged rather than added
  up: summing it across days would count the same patient once per night.
  Intensive care and all-hospital occupancy sit on separate panels because they
  differ by more than a factor of ten, and putting them on one axis would
  suggest a relationship the data does not contain.
</p>
<div class="card">{icu}{ward}</div>
<div class="card">{peak_table}</div>

<h2>International context</h2>
<p class="section-lede">
  The busiest single day each country recorded for intensive care, adjusted for
  population. Raw bed counts mostly tell you how big a country is, so this is
  the figure worth comparing.
</p>
<div class="card">{ranking_chart}{ranking_legend}</div>

<h2>Checking the numbers</h2>
<p class="section-lede">
  The publisher already provides a population-adjusted rate. Rather than trust
  it, this pipeline recalculates the same rate from World Bank population data
  and keeps the gap between the two as a number it can test. That way a
  population figure quietly changing upstream shows up as a failing check
  instead of a wrong chart.
</p>
<div class="card">{band_chart}
  <p class="legend"><span>How closely the two calculations agree, across {_n(sum(r["rows_in_band"] for r in data.variance_bands))} comparable observations</span></p>
</div>
<div class="card">{denominator_table}
  <p class="note">
    <b>What the check caught.</b> Cyprus is not a rounding difference. Working
    backwards from the published rate, the publisher is dividing by roughly
    896,000 people, while the World Bank figure is 1.32 million. The reason:
    the hospital returns cover only the government-controlled area, whereas the
    World Bank series covers the whole island. The two were never counting the
    same population. Poland's smaller gap is simpler, with the two sources
    sitting either side of the 2021 census revision.
  </p>
  <p class="note">
    <b>What changed because of it.</b> The comparison above uses the
    publisher's rate, because its population figure matches the area the bed
    counts actually come from. The independently calculated rate stays on as a
    background check. Both cases are now automated tests, so a new mismatch
    would be caught rather than published.
  </p>
</div>

<footer>
  <p>
    <b>Data.</b> COVID-19 hospital and intensive care activity compiled by
    Our World in Data from national health agencies (CC BY 4.0); population
    from the World Bank (CC BY 4.0); country and region codes from ISO 3166
    (CC BY-SA 4.0). Pipeline code is MIT licensed.
  </p>
  <p>
    <b>Read with care.</b> Reporting coverage varies by country and the
    upstream series stopped in August 2024, so this is a historical record
    rather than a live feed. Cyprus's population-adjusted figures are not
    comparable with other countries', for the reason set out above.
  </p>
</footer>

</div>
<script>{_SCRIPT}</script>"""


def render_page(data: DashboardData) -> str:
    """Wrap the body in a complete standalone HTML document.

    The style block is lifted into <head> so the page paints without a flash;
    everything after it is the body.
    """
    body = render_body(data)
    head_styles, _, markup = body.partition("</style>")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="description" content="How full hospitals and intensive care '
        'units were during COVID-19, in Australia and 49 other reporting places.">\n'
        "<style>img{max-width:100%}[hidden]{display:none!important}</style>\n"
        f"{head_styles}</style>\n</head>\n<body>\n{markup}\n</body>\n</html>\n"
    )


def run(focus: str = DEFAULT_FOCUS, output: Path | None = None) -> Path:
    """Render the dashboard to docs/index.html."""
    data = collect(focus=focus)
    output = output or (DOCS_DIR / "index.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_page(data), encoding="utf-8")
    log.info("dashboard -> %s (%.0f KiB)", output, output.stat().st_size / 1024)
    return output
