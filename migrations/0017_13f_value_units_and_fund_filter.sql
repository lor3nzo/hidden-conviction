-- v0.6.10: final 13F signal-integrity pass before whale-score activation.
--
-- Fixes:
--   * infer and normalize report-level legacy-thousands <value> filings
--   * persist value scale/status for auditability
--   * rebuild positions so predecessor dollar values and split detection use
--     normalized dollars
--   * strengthen fund/ETF exclusion in code (ISHARES TR, SCHWAB STRATEGIC TR,
--     and other fund-family token matches)
--
-- whale_score remains hard gated at zero pending post-rebuild validation.

ALTER TABLE thirteen_f_reports ADD COLUMN value_scale_factor REAL NOT NULL DEFAULT 1;
ALTER TABLE thirteen_f_reports ADD COLUMN value_unit_status TEXT NOT NULL DEFAULT 'unclassified';

CREATE INDEX IF NOT EXISTS idx_13f_value_unit_status
  ON thirteen_f_reports(value_unit_status, period_of_report DESC);

-- Rebuild the 13F materialization from preserved filings.  The staged scanner
-- and serial 5-position chunk architecture remain unchanged.
DELETE FROM thirteen_f_work_items;
DELETE FROM institutional_positions;
DELETE FROM thirteen_f_raw_positions_stage;
UPDATE thirteen_f_reports SET parent_report_id=NULL, predecessor_report_id=NULL;
DELETE FROM thirteen_f_reports;

INSERT INTO thirteen_f_reports (filing_id)
SELECT id
FROM filings
WHERE form_type LIKE '13F-HR%'
  AND COALESCE(source, '') <> 'historical-13f-predecessor';

INSERT INTO thirteen_f_work_items (report_id, work_key, stage)
SELECT id, 'prepare', 'prepare'
FROM thirteen_f_reports;

UPDATE filings
SET processed=0
WHERE form_type LIKE '13F-HR%'
  AND COALESCE(source, '') <> 'historical-13f-predecessor';

-- Keep institutional scoring outside HCS until normalized values and fund
-- exclusions have been validated in production.
UPDATE scores
SET whale_score = 0,
    convergence_bonus = MIN(15, MAX(0,
        ((CASE WHEN insider_score > 0 THEN 1 ELSE 0 END) +
         (CASE WHEN ownership_score > 0 THEN 1 ELSE 0 END) +
         (CASE WHEN event_score > 0 THEN 1 ELSE 0 END) - 1) * 4
    ));

UPDATE scores
SET raw_score = insider_score + cluster_score + ownership_score + whale_score + event_score + convergence_bonus,
    hcs_score = MIN(100, MAX(0, ROUND(insider_score + cluster_score + ownership_score + whale_score + event_score + convergence_bonus, 1)));

UPDATE scores
SET classification = CASE
    WHEN hcs_score >= 90 THEN 'Exceptional Conviction'
    WHEN hcs_score >= 80 THEN 'High Conviction'
    WHEN hcs_score >= 70 THEN 'Watch'
    WHEN hcs_score >= 60 THEN 'Emerging'
    ELSE 'No Signal'
END;
