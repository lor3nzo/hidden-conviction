-- v0.6.9: bound 13F prepare CPU by scanning raw information-table rows in
-- small durable queue stages, then aggregating in D1 before the existing
-- 5-position scoring chunks.

CREATE TABLE IF NOT EXISTS thirteen_f_raw_positions_stage (
  report_id INTEGER NOT NULL,
  row_ordinal INTEGER NOT NULL,
  issuer_name TEXT,
  title_of_class TEXT,
  cusip TEXT,
  shares REAL NOT NULL DEFAULT 0,
  value_dollars REAL NOT NULL DEFAULT 0,
  put_call TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (report_id, row_ordinal),
  FOREIGN KEY (report_id) REFERENCES thirteen_f_reports(id)
);

CREATE INDEX IF NOT EXISTS idx_13f_raw_stage_report_cusip
  ON thirteen_f_raw_positions_stage(report_id, cusip);

-- v0.6.8 was interrupted while rebuilding, so restart the 13F materialization
-- from known filings. Filings themselves are preserved.
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

-- Keep institutional scoring outside HCS until the staged rebuild is audited.
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
