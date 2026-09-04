-- ---------------------------------------------------------------------------
-- mart_rate_reconciliation  (assurance mart)
-- ---------------------------------------------------------------------------
-- Compares the publisher's population-adjusted rate against the one this
-- warehouse derives from World Bank population, per location and indicator.
--
-- This is the mart that proves the numbers were checked rather than trusted.
-- denominator_agreement grades what the comparison found:
--
--   Aligned                 the two denominators agree within 2%
--   Vintage difference      2-10%; the two sources sit either side of a census
--                           or estimate revision (Poland, ~-7%)
--   Definitional mismatch   over 10%; the sources are not counting the same
--                           population at all (Cyprus, ~-32%: hospital returns
--                           cover the government-controlled area, the World
--                           Bank series covers the whole island)
--
-- publisher_implied_population back-solves the denominator the publisher must
-- have divided by, which is what makes the third case diagnosable rather than
-- merely visible.
--
-- Grain: one row per location and indicator.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE mart_rate_reconciliation AS
SELECT
    l.location_key,
    l.location_name,
    l.source_location_code,
    l.region,
    i.indicator_key,
    i.indicator_code,
    i.indicator_name,
    count(*)                                            AS rows_compared,
    avg(f.rate_variance_pct)                            AS mean_variance_pct,
    median(f.rate_variance_pct)                         AS median_variance_pct,
    min(f.rate_variance_pct)                            AS min_variance_pct,
    max(f.rate_variance_pct)                            AS max_variance_pct,
    max(abs(f.rate_variance_pct))                       AS max_abs_variance_pct,
    sum(CASE WHEN abs(f.rate_variance_pct) > 5 THEN 1 ELSE 0 END) AS rows_over_5pct,
    round(100.0 * sum(CASE WHEN abs(f.rate_variance_pct) <= 5 THEN 1 ELSE 0 END)
          / nullif(count(*), 0), 2)                     AS pct_within_5pct,
    round(median(f.reported_value / nullif(f.reported_per_million / 1e6, 0)))
                                                        AS publisher_implied_population,
    round(median(f.population))                         AS reference_population,
    CASE
        WHEN abs(median(f.rate_variance_pct)) <= 2  THEN 'Aligned'
        WHEN abs(median(f.rate_variance_pct)) <= 10 THEN 'Vintage difference'
        ELSE 'Definitional mismatch'
    END                                                 AS denominator_agreement
FROM fct_hospital_activity AS f
JOIN dim_location  AS l ON l.location_key  = f.location_key
JOIN dim_indicator AS i ON i.indicator_key = f.indicator_key
WHERE f.rate_variance_pct IS NOT NULL
  AND f.reported_per_million > 0
GROUP BY ALL;
