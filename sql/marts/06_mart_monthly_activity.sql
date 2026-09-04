-- ---------------------------------------------------------------------------
-- mart_monthly_activity  (reporting mart)
-- ---------------------------------------------------------------------------
-- Monthly rollup of the fact, aggregated according to each measure's own
-- semantics rather than one blanket rule:
--
--   * stock measures (occupancy) -> avg and max; summing beds across days is
--     meaningless, so no sum is offered
--   * flow measures (admissions) -> sum, plus avg for reference
--
-- monthly_value / monthly_per_100k carry whichever aggregation the indicator
-- declares, so a chart can plot every measure without knowing the difference.
--
-- Rates here are the publisher's (reported_per_100k). See
-- 05_fct_hospital_activity.sql for why that is the comparable one.
--
-- Grain: one row per location, indicator and month.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE mart_monthly_activity AS
SELECT
    d.year_month,
    d.month_start_date,
    d.calendar_year,
    l.location_key,
    l.location_name,
    l.source_location_code,
    l.region,
    l.sub_region,
    l.location_type,
    i.indicator_key,
    i.indicator_code,
    i.indicator_name,
    i.care_setting,
    i.metric_type,
    i.measure_semantics,
    i.is_additive_over_time,
    count(*)                                          AS days_observed,
    avg(f.reported_value)                             AS avg_value,
    max(f.reported_value)                             AS max_value,
    CASE WHEN i.is_additive_over_time THEN sum(f.reported_value) END AS total_value,
    avg(f.reported_per_100k)                          AS avg_per_100k,
    max(f.reported_per_100k)                          AS max_per_100k,
    CASE WHEN i.is_additive_over_time THEN sum(f.reported_per_100k) END AS total_per_100k,
    avg(f.derived_per_100k)                           AS avg_derived_per_100k,
    -- the aggregation this indicator actually wants, ready to plot
    CASE WHEN i.is_additive_over_time THEN sum(f.reported_value)
         ELSE avg(f.reported_value) END               AS monthly_value,
    CASE WHEN i.is_additive_over_time THEN sum(f.reported_per_100k)
         ELSE avg(f.reported_per_100k) END            AS monthly_per_100k,
    sum(CASE WHEN f.is_statistical_outlier THEN 1 ELSE 0 END) AS outlier_days
FROM fct_hospital_activity AS f
JOIN dim_date      AS d ON d.date_key      = f.date_key
JOIN dim_location  AS l ON l.location_key  = f.location_key
JOIN dim_indicator AS i ON i.indicator_key = f.indicator_key
WHERE f.date_key <> -1
GROUP BY
    d.year_month,
    d.month_start_date,
    d.calendar_year,
    l.location_key,
    l.location_name,
    l.source_location_code,
    l.region,
    l.sub_region,
    l.location_type,
    i.indicator_key,
    i.indicator_code,
    i.indicator_name,
    i.care_setting,
    i.metric_type,
    i.measure_semantics,
    i.is_additive_over_time;
