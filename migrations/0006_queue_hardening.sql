-- v0.5.1: poison-message protection and stale-processing recovery metadata.
ALTER TABLE processing_queue ADD COLUMN started_at TEXT;
CREATE INDEX IF NOT EXISTS idx_queue_started_at
  ON processing_queue(status, started_at);
