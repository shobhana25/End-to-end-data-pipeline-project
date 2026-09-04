-- ---------------------------------------------------------------------------
-- dim_date  (conformed date dimension)
-- ---------------------------------------------------------------------------
-- Generated as a dense, gapless calendar spanning the observed fact range,
-- padded to whole years so that year-level rollups are never truncated
-- mid-period.
--
-- Grain: one row per calendar day.
-- Key:   date_key, a YYYYMMDD integer. Readable in a query result, sorts
--        chronologically, and survives a database reload - unlike a sequence.
--        date_key = -1 is the "Unknown" member every conformed dimension needs
--        so that unmatched facts can be kept and counted instead of dropped.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE dim_date AS
WITH bounds AS (
    SELECT
        date_trunc('year', min(event_date))                          AS start_date,
        date_trunc('year', max(event_date)) + INTERVAL 1 YEAR
                                            - INTERVAL 1 DAY         AS end_date
    FROM stg_hospital_activity
),
calendar AS (
    SELECT CAST(UNNEST(generate_series(start_date, end_date, INTERVAL 1 DAY)) AS DATE) AS full_date
    FROM bounds
)
SELECT
    CAST(strftime(full_date, '%Y%m%d') AS INTEGER)      AS date_key,
    full_date                                           AS full_date,
    year(full_date)                                     AS calendar_year,
    quarter(full_date)                                  AS calendar_quarter,
    year(full_date) || '-Q' || quarter(full_date)       AS quarter_label,
    month(full_date)                                    AS calendar_month,
    monthname(full_date)                                AS month_name,
    strftime(full_date, '%Y-%m')                        AS year_month,
    CAST(date_trunc('month', full_date) AS DATE)        AS month_start_date,
    isoyear(full_date)                                  AS iso_year,
    week(full_date)                                     AS iso_week,
    isoyear(full_date) || '-W' || lpad(CAST(week(full_date) AS VARCHAR), 2, '0') AS iso_week_label,
    CAST(date_trunc('week', full_date) AS DATE)         AS week_start_date,
    dayofmonth(full_date)                               AS day_of_month,
    isodow(full_date)                                   AS iso_day_of_week,   -- 1 = Monday
    dayname(full_date)                                  AS day_name,
    isodow(full_date) >= 6                              AS is_weekend,
    full_date = last_day(full_date)                     AS is_month_end
FROM calendar

UNION ALL

SELECT -1, NULL, NULL, NULL, 'Unknown', NULL, 'Unknown', 'Unknown', NULL,
       NULL, NULL, 'Unknown', NULL, NULL, NULL, 'Unknown', NULL, NULL;
