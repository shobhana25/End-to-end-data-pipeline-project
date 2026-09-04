# Architecture

## The shape of it

```mermaid
flowchart LR
    subgraph sources["Public sources"]
        A1["Our World in Data<br/>hospital &amp; ICU activity"]
        A2["World Bank<br/>annual population"]
        A3["ISO 3166-1<br/>country codes"]
    end

    subgraph ingest["1 · Ingest — Python"]
        B["Landing zone<br/><i>immutable, checksummed</i>"]
        B2[("meta_ingestion_runs<br/>URL · SHA-256 · bytes")]
    end

    subgraph stage["2 · Stage — Python"]
        C["Typed Parquet<br/><i>contract check, parse, dedupe, flag</i>"]
    end

    subgraph transform["3 · Transform — SQL"]
        D["Staging views"]
        E["Star schema<br/>3 dims · 2 facts"]
        F["Reporting marts"]
    end

    subgraph gate["4 · Quality"]
        G{{"29 declarative assertions"}}
        G2[("meta_quality_results")]
    end

    subgraph publish["5 · Publish"]
        H["docs/index.html<br/><i>static, no dependencies</i>"]
        I["Streamlit app"]
    end

    A1 & A2 & A3 --> B --> C --> D --> E --> F --> G
    B -.-> B2
    G -->|pass| H
    G -->|pass| I
    G -.-> G2
    G -->|"blocking failure"| X["Pipeline stops.<br/>Dashboard is not refreshed."]
```

The quality gate sits **before** publication on purpose: a dashboard built from
data that failed its own assertions is worse than a stale dashboard, because it
looks current.

## The star

```mermaid
erDiagram
    dim_date ||--o{ fct_hospital_activity : "date_key"
    dim_location ||--o{ fct_hospital_activity : "location_key"
    dim_indicator ||--o{ fct_hospital_activity : "indicator_key"
    dim_location ||--o{ fct_population_annual : "location_key"

    dim_date {
        int date_key PK
        date full_date
        int calendar_year
        string year_month
        int iso_week
        bool is_weekend
    }
    dim_location {
        int location_key PK
        string source_location_code UK
        string location_name
        string location_type
        string region
        bool has_population_data
    }
    dim_indicator {
        int indicator_key PK
        string indicator_code UK
        string care_setting
        string metric_type
        string measure_semantics
        bool is_additive_over_time
    }
    fct_hospital_activity {
        int date_key FK
        int location_key FK
        int indicator_key FK
        double reported_value
        double reported_per_100k
        bigint population
        double derived_per_100k
        double rate_variance_pct
    }
    fct_population_annual {
        int location_key FK
        int calendar_year
        bigint population
    }
```

Full column-level documentation: [`data_dictionary.md`](data_dictionary.md).

## Why the work is split the way it is

The brief for this project was "clean it in SQL *and* Python", and the split is
not arbitrary — each language does what it is better at.

**Python does the things SQL expresses badly.**

- *Contract enforcement.* Each landing file is checked against the column
  contract in `config/sources.yml` before anything else runs, so an upstream
  schema change fails loudly at the front door rather than producing a wrong
  number three layers down.
- *Parsing free text into structure.* The publisher packs four separate facts
  into one string — `"Daily ICU occupancy per million"` is a frequency, a care
  setting, a metric type and a unit. `parse_indicator` turns that into typed
  attributes, and refuses to guess: an unrecognised label raises rather than
  silently dropping rows from the model.
- *Windowed statistics.* Outliers are flagged with a median/MAD modified
  z-score computed within each series. SQL can express it; Python expresses it
  legibly.

**SQL does the modelling.** Surrogate keys, conformed dimensions, grain,
aggregation, and the reconciliation arithmetic are all plain SQL in
`sql/`. There is no templating language and no macro layer: the files are
readable on their own, which is the point of putting the model in SQL.

## Design decisions worth defending

**Sequential surrogate keys, not hashes.** The warehouse is rebuilt in full on
every run, so `row_number()` over the natural key is stable for a stable domain
and stays readable while exploring. A pipeline that loaded incrementally, or
built dimensions in parallel, would want a hash of the natural key instead so
keys never depend on load order. That is a real trade-off and this project sits
on the simple side of it deliberately.

**Every dimension has an Unknown member (`-1`).** Without one, an unmatched
fact either vanishes into an inner join or blocks the load. With one, it lands
somewhere countable — and `fct_activity_has_no_unknown_members` then asserts
that nothing actually does.

**The unit is pivoted onto the fact, not into the dimension.** "Daily ICU
occupancy" and "Daily ICU occupancy per million" are one measurement expressed
two ways. Treating them as two dimension members would double the row count and
make every rate query a self-join.

**Stock versus flow is carried on the dimension.** Occupancy is a count of beds
filled at an instant; summing it across days counts the same patient once per
night. Admissions are events in a period and may be summed. Recording that as
`is_additive_over_time` is what lets `mart_monthly_activity` aggregate each
measure correctly instead of relying on the next person to remember.

**Population is a fact, not a dimension attribute.** It is a measured quantity
that changes every year. On the dimension it would have to be either frozen at
one value or managed as a slowly-changing dimension, for what is simply an
annual measurement.

**The dimension is bigger than the fact.** `dim_location` carries all 249 ISO
countries though only ~50 report activity. A conformed dimension describes the
domain; the facts describe what was measured. `reports_hospital_activity`
records the difference.

## Failure modes this design defends against

| Failure | What catches it |
| --- | --- |
| Publisher renames or drops a column | Column contract in `config/sources.yml`, checked at staging |
| Truncated or partial download | Byte count, `min_rows` contract, and an atomic rename so a partial file never lands |
| A new upstream indicator appears | `parse_indicator` raises rather than dropping the rows |
| A publisher code that is not ISO | `config/location_overrides.csv`, plus an assertion that every reporting location has a region |
| World Bank aggregates double-counting population | Classified against ISO 3166 in staging, excluded in `stg_population`, asserted in the model tests |
| Duplicate rows fanning out a join | Grain uniqueness assertions on both facts |
| A silently rebased denominator upstream | The derived-rate reconciliation and its tolerance assertion |
| A gap in the date dimension dropping facts | `dim_date_is_gapless` |
| A republished but truncated upstream file | `series_has_not_regressed` |
| A dashboard built from bad data | The quality gate runs before publication and stops the run |

## Layout

```
config/           source registry, quality suite, model prose, location overrides
pipelines/        ingest · stage · warehouse · quality · dashboard · charts · cli
sql/staging/      typed views over the staged Parquet
sql/marts/        the star schema and the reporting marts, in build order
dashboard/        the Streamlit app
scripts/          data dictionary generator
tests/            124 tests, all runnable offline against committed fixtures
docs/             the published dashboard and this documentation
data/             landing zone, staged Parquet, DuckDB file (all gitignored)
```
