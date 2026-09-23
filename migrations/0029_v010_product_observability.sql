-- v0.10.0: product metadata, pipeline telemetry, durable watermarks, and issuer classification metadata.
-- Existing historical scores and filings are preserved.

ALTER TABLE filings ADD COLUMN accepted_at TEXT;

ALTER TABLE companies ADD COLUMN sic TEXT;
ALTER TABLE companies ADD COLUMN industry TEXT;
ALTER TABLE companies ADD COLUMN identity_checked_at TEXT;

CREATE INDEX IF NOT EXISTS idx_companies_sic ON companies(sic, hcs_eligible, ticker);
CREATE INDEX IF NOT EXISTS idx_companies_industry ON companies(industry, hcs_eligible, ticker);
CREATE INDEX IF NOT EXISTS idx_filings_accession_form ON filings(accession_number, form_type);
CREATE INDEX IF NOT EXISTS idx_filings_unprocessed_age ON filings(processed, created_at, form_type);

CREATE TABLE IF NOT EXISTS runtime_status (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS discovery_watermarks (
  source TEXT PRIMARY KEY,
  latest_accession TEXT,
  latest_filed_at TEXT,
  oldest_feed_filed_at TEXT,
  last_feed_count INTEGER NOT NULL DEFAULT 0,
  feed_saturated INTEGER NOT NULL DEFAULT 0,
  needs_backfill INTEGER NOT NULL DEFAULT 0,
  last_checked_at TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pipeline TEXT NOT NULL,
  metric_date TEXT NOT NULL,
  discovered INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  retried INTEGER NOT NULL DEFAULT 0,
  latest_latency_seconds REAL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(pipeline, metric_date)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_metrics_date ON pipeline_metrics(metric_date DESC, pipeline);

CREATE TABLE IF NOT EXISTS data_quality_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_type TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'warning',
  detail_json TEXT,
  detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT,
  UNIQUE(issue_type, entity_key, resolved_at)
);
CREATE INDEX IF NOT EXISTS idx_data_quality_open ON data_quality_issues(resolved_at, severity, detected_at DESC);

INSERT OR REPLACE INTO runtime_status(key, value, updated_at)
VALUES ('schema_version', '0029', CURRENT_TIMESTAMP);
