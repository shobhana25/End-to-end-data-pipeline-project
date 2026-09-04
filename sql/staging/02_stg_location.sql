-- ---------------------------------------------------------------------------
-- stg_location
-- ---------------------------------------------------------------------------
-- The conformed geography reference list, assembled from two feeds:
--
--   1. ISO 3166-1, which supplies every country plus its UN region hierarchy.
--   2. config/location_overrides.csv, which classifies the publisher's
--      non-ISO codes. Our World in Data reports the four UK nations
--      separately under OWID_ENG / OWID_SCT / OWID_WLS / OWID_NIR; without
--      this branch they would fail the region join and disappear from every
--      geographic rollup.
--
-- Grain: one row per source_location_code.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW stg_location AS
WITH iso_countries AS (
    SELECT
        alpha_3                       AS source_location_code,
        country_name                  AS location_name,
        alpha_2                       AS iso_alpha_2,
        alpha_3                       AS iso_alpha_3,
        'Country'                     AS location_type,
        alpha_3                       AS parent_iso_alpha_3,
        region                        AS region,
        sub_region                    AS sub_region,
        intermediate_region           AS intermediate_region,
        TRUE                          AS has_population_data,
        'ISO 3166-1'                  AS classification_source
    FROM raw_iso_countries
),
overrides AS (
    SELECT
        source_code                   AS source_location_code,
        location_name                 AS location_name,
        NULL                          AS iso_alpha_2,
        NULL                          AS iso_alpha_3,
        location_type                 AS location_type,
        parent_iso_alpha_3            AS parent_iso_alpha_3,
        region                        AS region,
        sub_region                    AS sub_region,
        NULL                          AS intermediate_region,
        CAST(has_population_data AS BOOLEAN) AS has_population_data,
        'config/location_overrides.csv'      AS classification_source
    FROM raw_location_overrides
)
SELECT * FROM iso_countries
UNION ALL
SELECT * FROM overrides;
