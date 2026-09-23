-- v0.13.0: public-edge split, query observability, and read-path indexes.

CREATE INDEX IF NOT EXISTS idx_scores_cik_date_id
  ON scores(cik, score_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_scores_today_eligible
  ON scores(score_date, hcs_score DESC, cik);
CREATE INDEX IF NOT EXISTS idx_companies_ticker_nocase
  ON companies(ticker COLLATE NOCASE, cik);
CREATE INDEX IF NOT EXISTS idx_company_tickers_nocase
  ON company_tickers(ticker COLLATE NOCASE, cik);
CREATE INDEX IF NOT EXISTS idx_processing_status_created
  ON processing_queue(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_score_recompute_ready
  ON score_recompute_queue(status, next_retry_at, created_at, cik);

CREATE TABLE IF NOT EXISTS query_metrics (
  query_name TEXT NOT NULL,
  metric_date TEXT NOT NULL,
  executions INTEGER NOT NULL DEFAULT 0,
  rows_returned INTEGER NOT NULL DEFAULT 0,
  total_duration_ms REAL NOT NULL DEFAULT 0,
  max_duration_ms REAL NOT NULL DEFAULT 0,
  slow_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (query_name, metric_date)
);
CREATE INDEX IF NOT EXISTS idx_query_metrics_date
  ON query_metrics(metric_date DESC, max_duration_ms DESC);

INSERT OR REPLACE INTO runtime_status(key, value, updated_at)
VALUES ('schema_version', '0032', CURRENT_TIMESTAMP);
