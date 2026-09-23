-- v0.14.0: adaptive ingestion, backlog history, alert state, retry receipts,
-- deployment performance history, and identity-refresh telemetry.

-- Normalize legacy compact YYYYMMDD filing dates so MAX()/ordering are chronological.
UPDATE filings
SET filed_at=SUBSTR(filed_at,1,4)||'-'||SUBSTR(filed_at,5,2)||'-'||SUBSTR(filed_at,7,2)
WHERE LENGTH(filed_at)=8 AND filed_at NOT LIKE '%-%';

ALTER TABLE processing_queue ADD COLUMN auto_replay_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE processing_queue ADD COLUMN last_auto_replay_at TEXT;

CREATE TABLE IF NOT EXISTS backlog_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  pending INTEGER NOT NULL DEFAULT 0,
  processing INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  oldest_pending_age_minutes REAL,
  discovered_1h INTEGER NOT NULL DEFAULT 0,
  processed_1h INTEGER NOT NULL DEFAULT 0,
  net_backlog_per_hour INTEGER NOT NULL DEFAULT 0,
  estimated_clear_minutes REAL,
  target_concurrency INTEGER NOT NULL DEFAULT 1,
  publish_limit INTEGER NOT NULL DEFAULT 50,
  identity_refresh_limit INTEGER NOT NULL DEFAULT 3,
  control_mode TEXT NOT NULL DEFAULT 'steady',
  app_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backlog_samples_captured
  ON backlog_samples(captured_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS ingestion_controller_state (
  id INTEGER PRIMARY KEY CHECK (id=1),
  mode TEXT NOT NULL DEFAULT 'steady',
  target_concurrency INTEGER NOT NULL DEFAULT 1,
  publish_limit INTEGER NOT NULL DEFAULT 50,
  identity_refresh_limit INTEGER NOT NULL DEFAULT 3,
  backlog INTEGER NOT NULL DEFAULT 0,
  sec_failed_today INTEGER NOT NULL DEFAULT 0,
  sec_latest_latency_seconds REAL,
  reason TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO ingestion_controller_state(id) VALUES (1);

CREATE TABLE IF NOT EXISTS ingestion_leases (
  slot INTEGER PRIMARY KEY,
  owner TEXT,
  lease_until TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (slot BETWEEN 1 AND 5)
);
INSERT OR IGNORE INTO ingestion_leases(slot) VALUES (1);
INSERT OR IGNORE INTO ingestion_leases(slot) VALUES (2);
INSERT OR IGNORE INTO ingestion_leases(slot) VALUES (3);
INSERT OR IGNORE INTO ingestion_leases(slot) VALUES (4);
INSERT OR IGNORE INTO ingestion_leases(slot) VALUES (5);

CREATE TABLE IF NOT EXISTS system_alerts (
  code TEXT PRIMARY KEY,
  severity TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  message TEXT NOT NULL,
  first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT,
  CHECK (severity IN ('info','warning','error')),
  CHECK (active IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_system_alerts_active
  ON system_alerts(active, severity, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS deployment_metrics (
  build_id TEXT PRIMARY KEY,
  app_version TEXT NOT NULL,
  deployed_at TEXT NOT NULL,
  health_ttfb_ms REAL,
  home_ttfb_ms REAL,
  system_ttfb_ms REAL,
  queue_pending INTEGER,
  queue_errors INTEGER,
  worker_architecture TEXT NOT NULL DEFAULT 'public-js-v1',
  captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_deployment_metrics_captured
  ON deployment_metrics(captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_processing_error_replay
  ON processing_queue(status, auto_replay_count, last_auto_replay_at, id);
CREATE INDEX IF NOT EXISTS idx_companies_identity_refresh
  ON companies(active, identity_checked_at, cik);

INSERT OR REPLACE INTO runtime_status(key, value, updated_at)
VALUES ('schema_version', '0033', CURRENT_TIMESTAMP);
