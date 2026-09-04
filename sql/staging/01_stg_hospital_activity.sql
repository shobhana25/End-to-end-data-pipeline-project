-- ---------------------------------------------------------------------------
-- stg_hospital_activity
-- ---------------------------------------------------------------------------
-- Typed, filtered view over the staged hospital/ICU activity Parquet.
--
-- Grain: one row per location, date and indicator (including the unit
-- variant), i.e. exactly the publisher's grain. Nothing is aggregated here;
-- this layer only guarantees types and excludes rows that are structurally
-- impossible (a negative bed count is not a small number, it is a broken row).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW stg_hospital_activity AS
SELECT
    CAST(source_location_code   AS VARCHAR) AS source_location_code,
    CAST(location_name          AS VARCHAR) AS location_name,
    CAST(event_date             AS DATE)    AS event_date,
    CAST(source_indicator_label AS VARCHAR) AS source_indicator_label,
    CAST(indicator_code         AS VARCHAR) AS indicator_code,
    CAST(base_indicator_code    AS VARCHAR) AS base_indicator_code,
    CAST(reporting_frequency    AS VARCHAR) AS reporting_frequency,
    CAST(care_setting           AS VARCHAR) AS care_setting,
    CAST(metric_type            AS VARCHAR) AS metric_type,
    CAST(unit                   AS VARCHAR) AS unit,
    CAST(measure_semantics      AS VARCHAR) AS measure_semantics,
    CAST(metric_value           AS DOUBLE)  AS metric_value,
    CAST(is_statistical_outlier AS BOOLEAN) AS is_statistical_outlier
FROM raw_hospital_activity
WHERE metric_value IS NOT NULL
  AND is_negative_value = FALSE;
