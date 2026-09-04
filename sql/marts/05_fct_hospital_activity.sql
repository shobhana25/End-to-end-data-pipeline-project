-- ---------------------------------------------------------------------------
-- fct_hospital_activity  (transaction-grain fact - the centre of the star)
-- ---------------------------------------------------------------------------
-- Grain: one row per location, date and indicator. Declared here and enforced
--        by a uniqueness assertion in config/quality_tests.yml, because an
--        unenforced grain is only a hope.
--
-- Measures
--   reported_value        the publisher's absolute count (beds, admissions)
--   reported_per_million  the publisher's own population-adjusted rate
--   reported_per_100k     the same rate at the scale health reporting uses;
--                         this is the measure to compare across locations,
--                         because the publisher aligns its denominator with
--                         the geography its numerator actually covers
--   population            this location's population in that calendar year,
--                         carried onto the fact so every rate below is
--                         reproducible from the row itself
--   derived_per_million   recomputed here from reported_value and population
--   derived_per_100k      the same, per 100k - an independent control, not
--                         the headline number
--   rate_variance_pct     signed % difference between the publisher's rate and
--                         ours
--
-- On rate_variance_pct: recomputing a rate the publisher already provides,
-- and keeping the difference as a measure, is what turns "the numbers look
-- fine" into something a test can assert. 97% of rows agree within 5%, and the
-- residual is explainable rather than random:
--
--   * a fraction of a percent almost everywhere, because Our World in Data
--     holds one population baseline for a whole series while this model uses
--     the World Bank estimate for the year of the observation;
--   * -7% for Poland, where the two sources straddle the 2021 census revision;
--   * -32% for Cyprus, which is not a vintage difference at all. The
--     publisher's implied denominator is ~896k against the World Bank's
--     ~1.32M, because the hospital returns cover the government-controlled
--     area while the World Bank series covers the whole island.
--
-- That last case is why reported_per_100k, not derived_per_100k, is the
-- measure used for cross-location comparison: the publisher's denominator
-- matches the geography its numerator came from. The derived rate stays as an
-- independent control, so the day a denominator is silently rebased upstream,
-- the quality suite says so.
--
-- The unit variant is pivoted here rather than carried as a dimension member:
-- count and rate are two expressions of one measurement, so they belong on one
-- fact row.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE fct_hospital_activity AS
WITH pivoted AS (
    SELECT
        source_location_code,
        event_date,
        base_indicator_code,
        max(CASE WHEN unit = 'Count'       THEN metric_value END) AS reported_value,
        max(CASE WHEN unit = 'Per million' THEN metric_value END) AS reported_per_million,
        bool_or(is_statistical_outlier)                           AS is_statistical_outlier
    FROM stg_hospital_activity
    GROUP BY source_location_code, event_date, base_indicator_code
),
keyed AS (
    SELECT
        COALESCE(d.date_key, -1)      AS date_key,
        COALESCE(l.location_key, -1)  AS location_key,
        COALESCE(i.indicator_key, -1) AS indicator_key,
        p.event_date,
        p.reported_value,
        p.reported_per_million,
        p.is_statistical_outlier,
        pop.population
    FROM pivoted AS p
    LEFT JOIN dim_date      AS d ON d.full_date            = p.event_date
    LEFT JOIN dim_location  AS l ON l.source_location_code = p.source_location_code
    LEFT JOIN dim_indicator AS i ON i.indicator_code       = p.base_indicator_code
    LEFT JOIN fct_population_annual AS pop
           ON pop.location_key  = COALESCE(l.location_key, -1)
          AND pop.calendar_year = year(p.event_date)
)
SELECT
    date_key,
    location_key,
    indicator_key,
    event_date,
    reported_value,
    reported_per_million,
    reported_per_million / 10.0                      AS reported_per_100k,
    population,
    CASE WHEN population > 0
         THEN reported_value / (population / 1e6) END AS derived_per_million,
    CASE WHEN population > 0
         THEN reported_value / (population / 1e5) END AS derived_per_100k,
    CASE WHEN population > 0 AND reported_per_million > 0
         THEN 100.0 * (reported_value / (population / 1e6) - reported_per_million)
              / reported_per_million END             AS rate_variance_pct,
    is_statistical_outlier,
    (population IS NULL)                             AS is_population_unknown,
    CAST(now() AS TIMESTAMP)                         AS warehouse_built_at
FROM keyed;
