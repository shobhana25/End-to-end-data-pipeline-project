-- ---------------------------------------------------------------------------
-- mart_location_coverage  (operational mart)
-- ---------------------------------------------------------------------------
-- How complete is each location's reporting? Reporting gaps are the failure
-- mode that quietly corrupts cross-country comparison: a country that stopped
-- publishing in 2022 will look like it had a calm 2023 unless the gap is
-- visible.
--
-- reporting_completeness_pct is days actually reported over calendar days
-- between the location's own first and last observation, so it measures gaps
-- inside a series rather than penalising a country for starting late.
--
-- Grain: one row per location and indicator.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE mart_location_coverage AS
WITH span AS (
    SELECT
        f.location_key,
        f.indicator_key,
        min(f.event_date)                                       AS first_observed_date,
        max(f.event_date)                                       AS last_observed_date,
        count(DISTINCT f.event_date)                            AS days_reported,
        date_diff('day', min(f.event_date), max(f.event_date)) + 1 AS days_in_span,
        sum(CASE WHEN f.is_population_unknown THEN 1 ELSE 0 END) AS rows_without_population
    FROM fct_hospital_activity AS f
    GROUP BY f.location_key, f.indicator_key
),
latest AS (
    SELECT max(event_date) AS dataset_last_date FROM fct_hospital_activity
)
SELECT
    l.location_key,
    l.location_name,
    l.source_location_code,
    l.region,
    l.location_type,
    l.has_population_data,
    i.indicator_key,
    i.indicator_code,
    i.indicator_name,
    i.reporting_frequency,
    s.first_observed_date,
    s.last_observed_date,
    s.days_reported,
    s.days_in_span,
    round(100.0 * s.days_reported / nullif(s.days_in_span, 0), 2) AS reporting_completeness_pct,
    date_diff('day', s.last_observed_date, t.dataset_last_date)   AS days_behind_dataset,
    s.rows_without_population,
    -- weekly series legitimately report ~1 day in 7, so completeness is judged
    -- against the indicator's own cadence, not against every calendar day
    CASE
        WHEN i.reporting_frequency = 'Weekly'
             AND 100.0 * s.days_reported / nullif(s.days_in_span, 0) >= 12 THEN 'Complete'
        WHEN i.reporting_frequency = 'Daily'
             AND 100.0 * s.days_reported / nullif(s.days_in_span, 0) >= 95 THEN 'Complete'
        WHEN i.reporting_frequency = 'Daily'
             AND 100.0 * s.days_reported / nullif(s.days_in_span, 0) >= 70 THEN 'Partial'
        ELSE 'Sparse'
    END                                                           AS coverage_grade
FROM span AS s
CROSS JOIN latest AS t
JOIN dim_location  AS l ON l.location_key  = s.location_key
JOIN dim_indicator AS i ON i.indicator_key = s.indicator_key;
