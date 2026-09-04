# Adding a source

Adding a dataset is a config change plus one staging function. Nothing in the
warehouse, quality or dashboard layers needs to know a new source exists until
you decide to model it.

## 1. Declare it

Add an entry to `config/sources.yml`:

```yaml
  - name: my_new_source            # used as the handler key and in the manifest
    title: Human-readable title
    publisher: Who publishes it
    licence: CC BY 4.0
    licence_url: https://…
    homepage: https://…            # where a human goes to read about it
    url: https://…/data.csv        # or file://data/raw/data.xlsx
    format: csv
    landing_file: my_new_source.csv
    enabled: true
    role: fact                     # fact | dimension
    description: >
      What it is and why it is here.
    expected_columns: [col_a, col_b]   # the contract - checked at staging
    min_rows: 1000                     # guards against a truncated download
```

`expected_columns` and `min_rows` are the contract. They are what turns "the
publisher changed their file" from a silently wrong number into a failed run,
so fill them in properly rather than leaving them empty.

## 2. Write a staging handler

Add a function to `pipelines/stage.py` and register it in `_HANDLERS`:

```python
def stage_my_new_source(source: Source, layout: Paths) -> StageResult:
    frame = _read_csv(layout.raw / source.landing_file)
    rows_in = len(frame)
    enforce_contract(frame, source)               # fail fast on a schema change

    frame = frame.rename(columns={c: snake_case(c) for c in frame.columns})
    # ... coerce types, drop structurally impossible rows, dedupe on the
    #     natural key, flag anything a human should look at ...

    output = layout.staged / "stg_my_new_source.parquet"
    frame.to_parquet(output, index=False)
    return StageResult(...)


_HANDLERS = {
    ...,
    "my_new_source": stage_my_new_source,
}
```

Handlers run in the order they appear in `_HANDLERS`, which matters when one
source is the reference list for another. ISO 3166 is staged before population
because it is what identifies the World Bank aggregate rows.

## 3. Expose it to SQL

Register the Parquet file in `pipelines/warehouse.py`:

```python
_STAGED_VIEWS = {
    ...,
    "stg_my_new_source.parquet": "raw_my_new_source",
}
```

The SQL layer then reads `raw_my_new_source` and never has to know a
filesystem path.

## 4. Model it

Add SQL files under `sql/staging/` and `sql/marts/`. They run in lexical order,
so the numeric prefix is the dependency order. Every model is
`CREATE OR REPLACE`, which is what keeps the build idempotent.

## 5. Assert it

Add entries to `config/quality_tests.yml`. At minimum: the grain of any new
fact, referential integrity to every dimension it joins, and a row-count floor.
Mark a test `requires_full_volume: true` if its threshold only makes sense
against the real feed rather than a test fixture.

## 6. Document it

Add the model to `config/model_docs.yml`. If it is a dimension or a fact, every
column needs a description. `scripts/build_data_dictionary.py` exits non-zero
and names any that are missing, so the dictionary cannot drift.

---

## Worked example: an AIHW extract

The Australian Institute of Health and Welfare is the natural source for this
kind of analysis on Australian data, and `config/sources.yml` carries a
disabled template entry for it. It is disabled rather than wired up because
AIHW publishes most collections as **XLSX data-download workbooks attached to a
specific report release**, not as a stable CSV endpoint, so there is no
durable URL to pin that would keep working across releases, and the reference
run in this repository is built only from sources that any reader can fetch and
reproduce byte for byte.

To wire one in:

**Pick a release and get the workbook.** From an AIHW report's *Data* tab
(for example MyHospitals admitted patient care or emergency department care),
download the data-download workbook.

**Point the source at it.** Either pin the release URL:

```yaml
    url: https://www.aihw.gov.au/getmedia/…/data-download.xlsx
    enabled: true
```

or, more robustly against a re-released file, commit to the local copy:

```bash
mv ~/Downloads/aihw-data-download.xlsx data/raw/aihw_hospital_activity.xlsx
```

```yaml
    url: file://data/raw/aihw_hospital_activity.xlsx
    enabled: true
```

The ingest layer handles `file://` sources through the same code path as HTTP:
copied atomically, checksummed, and recorded in the manifest. Provenance is
identical either way.

**Write the handler.** AIHW workbooks put several tables on one sheet under a
title row and footnotes, so the handler needs `skiprows`, an explicit
`usecols`, and a filter that drops the footnote rows:

```python
def stage_aihw_hospital_activity(source: Source, layout: Paths) -> StageResult:
    frame = pd.read_excel(
        layout.raw / source.landing_file,
        sheet_name="Table S2.1",
        skiprows=3,
        dtype=str,
    )
    frame = frame[frame["State/territory"].notna()]     # drop footnote rows
    ...
```

`pandas.read_excel` needs `openpyxl`; add it to `requirements.txt` when you
enable an XLSX source.

**Model it as a second fact against the same dimensions.** AIHW reports by
state or territory, financial year, and a peer-group hierarchy, so it wants:

- rows added to `config/location_overrides.csv` for the eight states and
  territories, with `parent_iso_alpha_3: AUS` and `location_type: Sub-national`
  (the same mechanism the four UK nations already use);
- a financial-year attribute on `dim_date` (`fy_label`, `fy_start_year`), since
  AIHW reports on July–June years;
- a new fact at AIHW's own grain rather than forced into
  `fct_hospital_activity`, whose grain is daily.

The conformed `dim_location` and `dim_date` are then shared across both facts,
which is exactly what conformed dimensions are for: the AIHW numbers and the
international numbers become comparable along the same axes without either
fact being reshaped to suit the other.
