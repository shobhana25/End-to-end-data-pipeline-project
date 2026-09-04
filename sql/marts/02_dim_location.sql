-- ---------------------------------------------------------------------------
-- dim_location  (conformed geography dimension)
-- ---------------------------------------------------------------------------
-- Grain: one row per reporting location.
-- Key:   location_key, a surrogate assigned by row_number() over the natural
--        key. The natural key (source_location_code) is retained alongside it
--        so a row can always be traced back to the publisher's identifier.
--
-- Why a surrogate at all: the natural keys are heterogeneous - real ISO
-- alpha-3 codes for countries, publisher-specific OWID_* codes for the UK
-- nations. A surrogate gives the facts one uniform, narrow join key and
-- insulates them from the publisher renaming a code.
--
-- Why sequential rather than hashed: this warehouse is rebuilt in full on
-- every run, so keys are stable for a stable domain and stay readable while
-- exploring. A pipeline that loaded incrementally, or built dimensions in
-- parallel, would want a hash of the natural key instead so that keys never
-- depend on load order.
--
-- The dimension deliberately covers the full ISO country list, not only the
-- ~50 locations that report hospital activity. A conformed dimension
-- describes the domain; the facts describe what was measured.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE dim_location AS
WITH reporting AS (
    SELECT DISTINCT source_location_code
    FROM stg_hospital_activity
),
enriched AS (
    SELECT
        l.source_location_code,
        l.location_name,
        l.iso_alpha_2,
        l.iso_alpha_3,
        l.location_type,
        l.parent_iso_alpha_3,
        l.region,
        l.sub_region,
        COALESCE(l.intermediate_region, 'Not applicable') AS intermediate_region,
        l.has_population_data,
        l.classification_source,
        (r.source_location_code IS NOT NULL)              AS reports_hospital_activity
    FROM stg_location AS l
    LEFT JOIN reporting AS r
           ON r.source_location_code = l.source_location_code
)
SELECT
    CAST(row_number() OVER (ORDER BY source_location_code) AS INTEGER) AS location_key,
    source_location_code,
    location_name,
    iso_alpha_2,
    iso_alpha_3,
    location_type,
    parent_iso_alpha_3,
    region,
    sub_region,
    intermediate_region,
    has_population_data,
    reports_hospital_activity,
    classification_source
FROM enriched

UNION ALL

SELECT -1, 'UNKNOWN', 'Unknown', NULL, NULL, 'Unknown', NULL,
       'Unknown', 'Unknown', 'Unknown', FALSE, FALSE, 'system';
