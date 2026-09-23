-- v0.12.0: deployment/build consistency and richer cron receipts.

ALTER TABLE cron_runs ADD COLUMN records_discovered INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cron_runs ADD COLUMN records_processed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cron_runs ADD COLUMN records_failed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cron_runs ADD COLUMN scores_recomputed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cron_runs ADD COLUMN queue_pending_at_finish INTEGER;

CREATE INDEX IF NOT EXISTS idx_cron_runs_finished ON cron_runs(finished_at DESC, status);

CREATE TABLE IF NOT EXISTS sec_request_metrics (
  source TEXT NOT NULL,
  metric_date TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  latest_latency_seconds REAL,
  max_latency_seconds REAL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (source, metric_date)
);
CREATE INDEX IF NOT EXISTS idx_sec_request_metrics_date ON sec_request_metrics(metric_date DESC, source);

INSERT OR REPLACE INTO runtime_status(key, value, updated_at)
VALUES ('schema_version', '0031', CURRENT_TIMESTAMP);
