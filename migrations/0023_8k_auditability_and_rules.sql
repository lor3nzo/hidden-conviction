-- v0.7.6: auditable, versioned 8-K semantics.
-- Add internal rationale/version fields so future scoring changes can selectively
-- rebuild only filings produced by an older parser/scoring version.
ALTER TABLE eight_k_events ADD COLUMN primary_item TEXT;
ALTER TABLE eight_k_events ADD COLUMN scoring_rule TEXT;
ALTER TABLE eight_k_events ADD COLUMN scoring_reason TEXT;
ALTER TABLE eight_k_events ADD COLUMN source_section TEXT;
ALTER TABLE eight_k_events ADD COLUMN parser_version TEXT;
ALTER TABLE eight_k_events ADD COLUMN scoring_version TEXT;

CREATE INDEX IF NOT EXISTS idx_eight_k_versions
  ON eight_k_events(parser_version, scoring_version);

-- Requeue only 8-K filings that do not already carry the v0.7.6 semantic
-- versions. On first deployment this intentionally rebuilds the existing corpus;
-- subsequent versioned migrations can use the same predicate selectively.
UPDATE filings
SET processed = 0,
    enqueued_at = NULL
WHERE form_type LIKE '8-K%'
  AND (
    NOT EXISTS (
      SELECT 1
      FROM eight_k_events e
      WHERE e.filing_id = filings.id
    )
    OR EXISTS (
      SELECT 1
      FROM eight_k_events e
      WHERE e.filing_id = filings.id
        AND (
          COALESCE(e.parser_version, '') <> '8k-parser-v0.7.6'
          OR COALESCE(e.scoring_version, '') <> '8k-score-v0.7.6'
        )
    )
  );

UPDATE processing_queue
SET status = 'pending',
    attempts = 0,
    started_at = NULL,
    processed_at = NULL,
    last_error = NULL
WHERE filing_id IN (
    SELECT f.id
    FROM filings f
    LEFT JOIN eight_k_events e ON e.filing_id = f.id
    WHERE f.form_type LIKE '8-K%'
      AND (
        e.filing_id IS NULL
        OR COALESCE(e.parser_version, '') <> '8k-parser-v0.7.6'
        OR COALESCE(e.scoring_version, '') <> '8k-score-v0.7.6'
      )
);
