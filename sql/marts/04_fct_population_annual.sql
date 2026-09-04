-- ---------------------------------------------------------------------------
-- fct_population_annual  (periodic snapshot fact)
-- ---------------------------------------------------------------------------
-- Grain: one row per location and calendar year.
-- Measure: population (semi-additive - additive across locations, never
--          across years).
--
-- Modelled as a fact rather than an attribute on dim_location because
-- population is a measured quantity that changes every year. Storing it on the
-- dimension would either freeze it at one value or force a slowly-changing
-- dimension for something that is simply a yearly measurement.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE fct_population_annual AS
SELECT
    COALESCE(l.location_key, -1)                   AS location_key,
    p.calendar_year                                AS calendar_year,
    p.population                                   AS population,
    CAST(p.population / 1e6 AS DOUBLE)             AS population_millions,
    p.iso_alpha_3                                  AS source_location_code
FROM stg_population AS p
LEFT JOIN dim_location AS l
       ON l.source_location_code = p.iso_alpha_3;
