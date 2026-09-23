-- v0.6.1: rebuild early 13F data with correct period, attachment fallback,
-- value units, and SEC-only issuer identity matching.

ALTER TABLE institutional_positions ADD COLUMN value_dollars REAL;

-- Existing v0.6 rows stored the post-2023 SEC XML value (nearest dollar) in
-- the legacy value_thousands column. Normalize both representations before
-- rebuilding the pipeline below.
UPDATE institutional_positions
SET value_dollars = value_thousands,
    value_thousands = value_thousands / 1000.0
WHERE value_dollars IS NULL;

CREATE INDEX IF NOT EXISTS idx_13f_value_dollars
  ON institutional_positions(value_dollars DESC, period_of_report DESC);

-- This is an early MVP database, so a clean 13F rebuild is safer than trying
-- to preserve rows that lack period metadata or were parsed with old units.
DELETE FROM thirteen_f_work_items;
DELETE FROM institutional_positions;
UPDATE thirteen_f_reports SET parent_report_id = NULL, predecessor_report_id = NULL;
DELETE FROM thirteen_f_reports;

-- Current/live reports are authoritative roots. Historical predecessors are
-- recreated on demand by the dedicated 13F worker.
INSERT INTO thirteen_f_reports (filing_id)
SELECT id
FROM filings
WHERE form_type LIKE '13F-HR%'
  AND COALESCE(source, '') <> 'historical-13f-predecessor';

INSERT INTO thirteen_f_work_items (report_id, work_key, stage)
SELECT id, 'prepare', 'prepare'
FROM thirteen_f_reports;

UPDATE filings
SET processed = 0
WHERE form_type LIKE '13F-HR%'
  AND COALESCE(source, '') <> 'historical-13f-predecessor';
