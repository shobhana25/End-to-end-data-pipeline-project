-- ---------------------------------------------------------------------------
-- mart_peak_pressure  (reporting mart)
-- ---------------------------------------------------------------------------
-- The single worst day each location recorded for each measure, on both the
-- absolute and the population-adjusted scale, plus when it happened.
--
-- Peak-per-100k is the comparable number: absolute bed counts say more about
-- how big a country is than about how hard its hospitals were pushed. The rate
-- used is the publisher's (reported_per_100k), whose denominator matches the
-- geography its numerator covers - see 05_fct_hospital_activity.sql.
-- peak_derived_per_100k is retained beside it as the independent control.
--
-- Grain: one row per location and indicator.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE mart_peak_pressure AS
WITH ranked AS (
    SELECT
        f.location_key,
        f.indicator_key,
        f.event_date,
        f.reported_value,
        f.reported_per_100k,
        f.derived_per_100k,
        row_number() OVER (
            PARTITION BY f.location_key, f.indicator_key
            ORDER BY f.reported_value DESC, f.event_date
        ) AS rn_absolute,
        row_number() OVER (
            PARTITION BY f.location_key, f.indicator_key
            ORDER BY f.reported_per_100k DESC NULLS LAST, f.event_date
        ) AS rn_rate
    FROM fct_hospital_activity AS f
),
observed AS (
    SELECT
        location_key,
        indicator_key,
        count(*)                AS days_observed,
        min(event_date)         AS first_observed_date,
        max(event_date)         AS last_observed_date,
        avg(reported_value)     AS mean_value,
        avg(reported_per_100k)  AS mean_per_100k
    FROM fct_hospital_activity
    GROUP BY location_key, indicator_key
)
SELECT
    l.location_key,
    l.location_name,
    l.source_location_code,
    l.iso_alpha_3,
    l.region,
    l.sub_region,
    l.location_type,
    i.indicator_key,
    i.indicator_code,
    i.indicator_name,
    i.care_setting,
    i.metric_type,
    i.measure_semantics,
    o.days_observed,
    o.first_observed_date,
    o.last_observed_date,
    o.mean_value,
    o.mean_per_100k,
    pa.reported_value        AS peak_value,
    pa.event_date            AS peak_value_date,
    pr.reported_per_100k     AS peak_per_100k,
    pr.event_date            AS peak_per_100k_date,
    pr.derived_per_100k      AS peak_derived_per_100k
FROM observed AS o
JOIN dim_location  AS l ON l.location_key  = o.location_key
JOIN dim_indicator AS i ON i.indicator_key = o.indicator_key
LEFT JOIN ranked AS pa
       ON pa.location_key  = o.location_key
      AND pa.indicator_key = o.indicator_key
      AND pa.rn_absolute   = 1
LEFT JOIN ranked AS pr
       ON pr.location_key  = o.location_key
      AND pr.indicator_key = o.indicator_key
      AND pr.rn_rate       = 1;
