-- ---------------------------------------------------------------------------
-- dim_indicator  (conformed measure dimension)
-- ---------------------------------------------------------------------------
-- Grain: one row per base indicator - that is, per real-world measure, with
--        the unit variant pivoted out into fact columns rather than doubling
--        the dimension. "Daily ICU occupancy" and "Daily ICU occupancy per
--        million" are one measure expressed two ways, not two measures.
--
-- The attributes here are parsed out of the publisher's free-text label by
-- pipelines/stage.py::parse_indicator. The one that matters most analytically
-- is is_additive_over_time:
--
--   * Occupancy is a *stock* - beds full at a point in time. Summing it across
--     days counts the same patient once per night and produces a number with
--     no meaning. Aggregate it with avg() or max().
--   * Admissions are a *flow* - events during a period. They may be summed.
--
-- Carrying that on the dimension is what lets the monthly mart aggregate each
-- measure correctly instead of relying on whoever writes the next query to
-- remember the difference.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE dim_indicator AS
WITH base AS (
    SELECT
        base_indicator_code,
        any_value(care_setting)                                     AS care_setting,
        any_value(metric_type)                                      AS metric_type,
        any_value(reporting_frequency)                              AS reporting_frequency,
        any_value(measure_semantics)                                AS measure_semantics,
        max(CASE WHEN unit = 'Count'       THEN source_indicator_label END) AS source_count_label,
        max(CASE WHEN unit = 'Per million' THEN source_indicator_label END) AS source_rate_label
    FROM stg_hospital_activity
    GROUP BY base_indicator_code
)
SELECT
    CAST(row_number() OVER (ORDER BY base_indicator_code) AS INTEGER) AS indicator_key,
    base_indicator_code                                   AS indicator_code,
    reporting_frequency || ' ' || care_setting || ' ' || metric_type AS indicator_name,
    care_setting,
    metric_type,
    reporting_frequency,
    measure_semantics,
    (metric_type = 'New admissions')                      AS is_additive_over_time,
    CASE WHEN metric_type = 'New admissions' THEN 'sum' ELSE 'avg' END
                                                          AS default_time_aggregation,
    source_count_label,
    source_rate_label
FROM base

UNION ALL

SELECT -1, 'unknown', 'Unknown', 'Unknown', 'Unknown', 'Unknown', 'Unknown',
       FALSE, 'avg', NULL, NULL;
