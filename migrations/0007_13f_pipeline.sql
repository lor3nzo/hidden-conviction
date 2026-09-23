-- v0.6: isolate and chunk 13F processing in a dedicated queue/worker.

CREATE TABLE IF NOT EXISTS thirteen_f_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id INTEGER NOT NULL UNIQUE,
  parent_report_id INTEGER,
  predecessor_report_id INTEGER,
  predecessor_accession TEXT,
  is_historical INTEGER NOT NULL DEFAULT 0,
  manager_cik TEXT,
  manager_name TEXT,
  filing_date TEXT,
  period_of_report TEXT,
  total_positions INTEGER NOT NULL DEFAULT 0,
  selected_positions INTEGER NOT NULL DEFAULT 0,
  chunks_expected INTEGER NOT NULL DEFAULT 0,
  chunks_done INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  processed_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (filing_id) REFERENCES filings(id),
  FOREIGN KEY (parent_report_id) REFERENCES thirteen_f_reports(id),
  FOREIGN KEY (predecessor_report_id) REFERENCES thirteen_f_reports(id)
);

CREATE INDEX IF NOT EXISTS idx_13f_reports_status
  ON thirteen_f_reports(status, id);
CREATE INDEX IF NOT EXISTS idx_13f_reports_manager_period
  ON thirteen_f_reports(manager_cik, period_of_report DESC, id DESC);

CREATE TABLE IF NOT EXISTS thirteen_f_work_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id INTEGER NOT NULL,
  work_key TEXT NOT NULL,
  stage TEXT NOT NULL,
  payload_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  enqueued_at TEXT,
  processed_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (report_id) REFERENCES thirteen_f_reports(id),
  UNIQUE(report_id, work_key)
);

CREATE INDEX IF NOT EXISTS idx_13f_work_status
  ON thirteen_f_work_items(status, enqueued_at, id);
CREATE INDEX IF NOT EXISTS idx_13f_work_started
  ON thirteen_f_work_items(status, started_at);

-- Backfill every known 13F filing into the new pipeline.
INSERT OR IGNORE INTO thirteen_f_reports (filing_id)
SELECT id
FROM filings
WHERE form_type LIKE '13F-HR%'
  AND source <> 'historical-13f-predecessor';

INSERT OR IGNORE INTO thirteen_f_work_items (report_id, work_key, stage)
SELECT id, 'prepare', 'prepare'
FROM thirteen_f_reports
WHERE status NOT IN ('done', 'ignored', 'error');

-- Retire 13F jobs from the general queue. Their filings remain unprocessed until
-- the dedicated 13F worker completes them.
UPDATE processing_queue
SET status = 'ignored',
    processed_at = CURRENT_TIMESTAMP,
    started_at = NULL,
    last_error = 'Migrated to dedicated v0.6 13F pipeline'
WHERE filing_id IN (
  SELECT id FROM filings WHERE form_type LIKE '13F-HR%'
)
AND status NOT IN ('done', 'ignored', 'error');

-- Recover stale non-13F work left behind by earlier hard Worker terminations.
UPDATE processing_queue
SET status = 'pending',
    started_at = NULL,
    last_error = COALESCE(last_error, 'Recovered during v0.6 migration')
WHERE status = 'processing'
  AND filing_id NOT IN (
    SELECT id FROM filings WHERE form_type LIKE '13F-HR%'
  );

UPDATE filings
SET enqueued_at = NULL
WHERE processed = 0
  AND id IN (
    SELECT filing_id FROM processing_queue WHERE status = 'pending'
  );
