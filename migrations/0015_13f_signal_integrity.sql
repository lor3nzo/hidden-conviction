-- v0.6.8: rebuild 13F data after signal-integrity fixes.
-- Changes:
--   * aggregate duplicate information-table rows by CUSIP before ranking/comparison
--   * exclude PUT/CALL rows from long-equity scoring
--   * store the full aggregated long-equity portfolio denominator
--   * treat missing prior positions as unknown when predecessor retention is truncated
--   * suppress split-like corporate actions from accumulation scoring
-- whale_score remains gated at zero until post-rebuild validation.

ALTER TABLE thirteen_f_reports ADD COLUMN raw_position_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE thirteen_f_reports ADD COLUMN option_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE thirteen_f_reports ADD COLUMN portfolio_value_dollars REAL NOT NULL DEFAULT 0;

ALTER TABLE institutional_positions ADD COLUMN previous_value_dollars REAL;
ALTER TABLE institutional_positions ADD COLUMN comparison_status TEXT;
ALTER TABLE institutional_positions ADD COLUMN predecessor_complete INTEGER NOT NULL DEFAULT 0;
ALTER TABLE institutional_positions ADD COLUMN corporate_action_suspected INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_13f_comparison_status
  ON institutional_positions(comparison_status, period_of_report DESC);
CREATE INDEX IF NOT EXISTS idx_13f_corporate_action
  ON institutional_positions(corporate_action_suspected, period_of_report DESC);

-- The already materialized 13F rows were computed before CUSIP aggregation and
-- predecessor coverage protection, so they must be rebuilt. Preserve filings.
DELETE FROM thirteen_f_work_items;
DELETE FROM institutional_positions;
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

-- Keep the institutional family outside HCS until the rebuilt data is audited.
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
