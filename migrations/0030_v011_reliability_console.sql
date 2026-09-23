-- v0.11.0: reliability, deterministic replay metadata, cron telemetry,
-- retry/backoff receipts, reconciliation watermarks, and system console support.

ALTER TABLE filings ADD COLUMN is_amendment INTEGER NOT NULL DEFAULT 0;
ALTER TABLE filings ADD COLUMN amends_accession TEXT;
CREATE INDEX IF NOT EXISTS idx_filings_amendment ON filings(is_amendment, form_type, cik, filed_at);

ALTER TABLE processing_queue ADD COLUMN next_retry_at TEXT;
ALTER TABLE processing_queue ADD COLUMN dead_letter_at TEXT;
ALTER TABLE processing_queue ADD COLUMN last_attempt_at TEXT;

ALTER TABLE score_recompute_queue ADD COLUMN next_retry_at TEXT;
ALTER TABLE score_recompute_queue ADD COLUMN dead_letter_at TEXT;
ALTER TABLE score_recompute_queue ADD COLUMN last_attempt_at TEXT;

ALTER TABLE discovery_watermarks ADD COLUMN last_reconciled_date TEXT;
ALTER TABLE discovery_watermarks ADD COLUMN last_reconciled_at TEXT;
ALTER TABLE discovery_watermarks ADD COLUMN last_index_name TEXT;

ALTER TABLE scores ADD COLUMN scoring_engine_version TEXT;
ALTER TABLE scores ADD COLUMN evidence_fingerprint TEXT;
ALTER TABLE scores ADD COLUMN reason_codes_json TEXT;
ALTER TABLE scores ADD COLUMN score_delta REAL;

CREATE TABLE IF NOT EXISTS cron_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  duration_ms INTEGER,
  summary_json TEXT,
  error_text TEXT,
  app_version TEXT NOT NULL,
  CHECK (status IN ('running','success','error'))
);
CREATE INDEX IF NOT EXISTS idx_cron_runs_started ON cron_runs(started_at DESC, status);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reconciliation_date TEXT NOT NULL,
  source TEXT NOT NULL,
  index_name TEXT,
  discovered_count INTEGER NOT NULL DEFAULT 0,
  inserted_count INTEGER NOT NULL DEFAULT 0,
  processed_count INTEGER NOT NULL DEFAULT 0,
  pending_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'success',
  detail_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(reconciliation_date, source)
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_date ON reconciliation_runs(reconciliation_date DESC, source);

CREATE TABLE IF NOT EXISTS replay_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  replay_type TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  result_json TEXT,
  error_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_replay_receipts_requested ON replay_receipts(requested_at DESC, status);

CREATE INDEX IF NOT EXISTS idx_processing_retry_ready
  ON processing_queue(status, next_retry_at, priority, id);

INSERT OR REPLACE INTO runtime_status(key, value, updated_at)
VALUES ('schema_version', '0030', CURRENT_TIMESTAMP);
