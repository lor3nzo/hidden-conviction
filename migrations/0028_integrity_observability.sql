-- v0.9.0: correctness, score aging, and ingestion observability.
-- No historical scores are rewritten by this migration.

CREATE TABLE IF NOT EXISTS discovery_health (
  source TEXT PRIMARY KEY,
  last_attempt_at TEXT,
  last_success_at TEXT,
  latest_filed_at TEXT,
  last_discovered_count INTEGER NOT NULL DEFAULT 0,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_discovery_health_success
  ON discovery_health(last_success_at DESC, source);

CREATE INDEX IF NOT EXISTS idx_scores_date_hcs
  ON scores(score_date DESC, hcs_score DESC, cik);

CREATE INDEX IF NOT EXISTS idx_filings_source_created
  ON filings(source, created_at DESC, filed_at DESC);

-- Prime the rolling-window refresh queue for any positive score not materialized
-- today. This changes queue state only; score history remains immutable.
UPDATE score_recompute_queue
SET status='pending',
    reason='v0.9.0 initial rolling-window refresh',
    attempts=0,
    created_at=CURRENT_TIMESTAMP,
    started_at=NULL,
    processed_at=NULL,
    last_error=NULL
WHERE status IN ('done','error')
  AND cik IN (
    SELECT cik FROM current_scores
    WHERE hcs_score>0 AND score_date<DATE('now')
  );

INSERT OR IGNORE INTO score_recompute_queue(cik, reason)
SELECT cik, 'v0.9.0 initial rolling-window refresh'
FROM current_scores
WHERE hcs_score>0 AND score_date<DATE('now');
