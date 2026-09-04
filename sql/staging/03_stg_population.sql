-- ---------------------------------------------------------------------------
-- stg_population
-- ---------------------------------------------------------------------------
-- Annual population, real countries only.
--
-- The World Bank series interleaves ~50 aggregates ("World", "OECD members",
-- "Upper middle income", "IDA total") with countries under an identical
-- schema. The staging layer in Python flags them against the ISO 3166
-- reference list; this view is where they are excluded, so nothing downstream
-- can double-count a population.
--
-- Grain: one row per country and calendar year.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW stg_population AS
SELECT
    CAST(iso_alpha_3   AS VARCHAR) AS iso_alpha_3,
    CAST(location_name AS VARCHAR) AS location_name,
    CAST(calendar_year AS INTEGER) AS calendar_year,
    CAST(population    AS BIGINT)  AS population
FROM raw_population
WHERE is_aggregate = FALSE
  AND population > 0;
