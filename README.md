# Hospital capacity data pipeline

This project answers a simple question with a lot of messy public data: **how
full did hospitals get during COVID-19, and how did Australia compare?**

Getting to that answer takes five steps, and this repository automates all of
them. It downloads three public datasets, cleans them up, loads them into a
properly structured database, checks its own numbers, and draws a dashboard.
One command, about three seconds.

```bash
make setup && make all
```

**[View the dashboard](https://shobhana25.github.io/End-to-end-data-pipeline-project/)** ·
[How it's built](docs/architecture.md) ·
[What every column means](docs/data_dictionary.md) ·
[Adding your own data source](docs/adding_a_source.md)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/dashboard-dark.png">
  <img alt="The dashboard: headline figures, Australian ICU and hospital occupancy over time, and an international comparison." src="docs/dashboard-light.png">
</picture>

## What it does

```
download  →  clean  →  organise  →  check  →  publish
 Python      Python      SQL         SQL       HTML
```

**Download.** Fetches each dataset over the internet, retrying if the network
hiccups. Files are written to a temporary name and only renamed into place once
they arrive complete, so a dropped connection can never leave a half-written
file that looks fine. Everything is fingerprinted with a checksum, so you can
prove you got the same bytes as anyone else.

**Clean.** Checks each file still has the columns it is supposed to have. If a
publisher renames a column, the run stops there with a clear message instead of
producing a wrong number three steps later. Then it fixes types, removes
duplicates, and unpacks the publisher's free-text labels into proper fields.

**Organise.** Loads everything into a star schema: three reference tables
describing dates, places and measures, and two tables of measurements that join
to them. This is the standard way analytics databases are laid out, and it is
what makes questions like "ICU occupancy by region by month" a short query
rather than a puzzle.

**Check.** Runs 29 automated tests against the finished database. Are the keys
unique? Do all the joins resolve? Are the values possible? Do the rates match
an independent calculation? If any of these fail, the run stops before the
dashboard is refreshed. A stale dashboard is better than a confidently wrong
one.

**Publish.** Renders a single self-contained HTML page from the database. No
external scripts, no chart library, no build step. It opens anywhere, including
offline.

Roughly 115,000 daily observations across 50 places, 2020 to 2024.

## What the data turned out to hide

The source already provides a population-adjusted rate. Rather than take it on
trust, this pipeline recalculates the same rate from World Bank population
figures and keeps the difference as a number it can test.

That caught something real.

| Place | Publisher is dividing by | World Bank says | Gap |
| --- | ---: | ---: | ---: |
| Cyprus | about 896,000 | 1,317,309 | **−32%** |
| Poland | about 39,857,000 | 36,981,559 | −7% |
| everyone else | | | 97% agree within 5% |

Cyprus is not a rounding error. The hospital figures cover only the
government-controlled area of the island, while the World Bank population
covers the whole island. The two numbers were never counting the same people.
Poland's smaller gap has a duller explanation: the two sources sit either side
of the 2021 census revision.

This changed a real decision. Because the publisher's population figure matches
the area its bed counts actually come from, the dashboard compares countries
using *their* rate, and keeps the independently calculated one running quietly
in the background as a check. Both cases are now automated tests, so if a
population figure changes upstream, it shows up as a failing build rather than
a wrong chart.

## Try it

```bash
git clone https://github.com/shobhana25/End-to-end-data-pipeline-project.git
cd End-to-end-data-pipeline-project

make setup     # install dependencies
make all       # download, build, check and publish
open docs/index.html
```

Useful individual commands:

```bash
make offline     # rebuild everything with no internet at all
make test        # 126 tests, also no internet needed
make lint        # style and formatting
make app         # launch the interactive Streamlit version
make dashboard   # just redraw the page

python -m pipelines.cli dashboard --focus NZL    # lead with a different country
```

Exit codes mean something, which matters if this ever runs on a schedule:
`0` fine, `1` the data failed its checks, `2` the pipeline itself broke.

## How the database is laid out

```
                    ┌──────────────┐
                    │   dim_date   │   1,828 rows, one per day
                    └──────┬───────┘
                           │
   ┌──────────────┐        │        ┌────────────────┐
   │ dim_location │────────┼────────│ dim_indicator  │
   │   254 rows   │        │        │     5 rows     │
   └──────┬───────┘        │        └───────┬────────┘
          │         ┌──────▼────────────────▼──────┐
          │         │   fct_hospital_activity      │  115,262 rows
          │         │  one row per place, day      │
          │         │  and measure                 │
          │         └──────────────────────────────┘
          │         ┌──────────────────────────────┐
          └────────▶│   fct_population_annual      │   13,945 rows
                    └──────────────────────────────┘
```

A few choices worth explaining, with the full reasoning in
[docs/architecture.md](docs/architecture.md):

**Some numbers can be added up and some cannot.** Occupancy counts beds full at
one moment. Adding it across days counts the same patient once per night, which
means nothing. Admissions count events over a period, so they *can* be added.
The database records which is which, so the monthly figures aggregate correctly
without anyone having to remember the rule.

**Population lives in its own table.** It gets measured every year and it
changes, so it belongs with the other measurements rather than pinned to a
place as though it were fixed.

**The places table is bigger than the data.** All 249 countries are listed
though only 50 report hospital figures. A reference table should describe the
world; the measurement tables describe what was actually measured.

## How you know it works

**29 checks on the data.** These are written as configuration, not code, so
they read like plain statements of intent:

```yaml
  - name: fct_activity_grain_is_unique
    description: >
      One row per location, date and indicator. An unenforced grain is only a
      hope, so it is asserted here.
    model: fct_hospital_activity
    type: unique_combination
    columns: [date_key, location_key, indicator_key]
```

**126 tests on the code**, all runnable with no internet, in about three
seconds. They run the whole pipeline against small sample files kept in the
repository. Those samples are real rows taken from the real data, so every
awkward case is still there at a size that runs instantly.

**The checks are themselves tested.** A test file deliberately breaks the
database in ten specific ways (duplicate key, broken join, negative bed count,
a missing day in the calendar) and confirms that the right check catches each
one, and that the others stay quiet. A test suite nobody has ever seen fail is
not evidence of anything.

CI runs the style checks, the test suite on two Python versions, and a full
offline pipeline run on every push.

## Where the data comes from

| Dataset | Published by | Licence |
| --- | --- | --- |
| [Hospital and ICU activity](https://github.com/owid/covid-19-data/tree/master/public/data/hospitalizations) | Our World in Data, compiled from national health agencies | CC BY 4.0 |
| [Population by country and year](https://github.com/datasets/population) | World Bank | CC BY 4.0 |
| [Country and region codes](https://github.com/lukes/ISO-3166-Countries-with-Regional-Codes) | ISO 3166 and UN M49 | CC BY-SA 4.0 |

These three were chosen because they are openly licensed, stable, and
reproducible: the pipeline records a checksum for each, so anyone can confirm
they downloaded exactly the same data.

**A note on AIHW.** The Australian Institute of Health and Welfare would be the
natural source for this analysis on Australian data, and there is a template
entry for it in `config/sources.yml`. It ships switched off because AIHW
publishes most of its data as spreadsheets attached to individual report
releases rather than at stable web addresses, so there is no durable link to
pin. [docs/adding_a_source.md](docs/adding_a_source.md) walks through wiring an
AIHW extract in, including the state and financial-year handling it needs.

### Things to keep in mind

- The hospital data stopped being published in August 2024. This is a
  historical record, not a live feed.
- Reporting completeness varies a lot by country. The database grades every
  series, and 19 of 139 come out as sparse.
- Cyprus's population-adjusted figures are not comparable with other countries',
  for the reason described above.

## What's in the repository

```
config/       which data to download, and the checks to run against it
pipelines/    the Python: download, clean, load, check, publish
sql/          the database models, in the order they are built
dashboard/    the interactive Streamlit version
tests/        126 tests plus the sample data they run against
docs/         the published dashboard and the written documentation
```

## Putting the dashboard online

`docs/index.html` is committed to the repository, so GitHub can serve it
directly with no build step:

**Settings → Pages → Source: Deploy from a branch → `main` / `/docs`**

There is also a workflow (`.github/workflows/refresh.yml`) that re-runs the
whole pipeline against live data and commits the updated dashboard. It only
runs when triggered by hand; uncomment the schedule block to make it weekly.
The checks run first, so a failed build never gets published.

## Licence

The code is [MIT](LICENSE). The datasets stay under their own licences, listed
above.
