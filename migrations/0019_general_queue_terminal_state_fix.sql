-- v0.7.2: repair the Hycroft 8-K that was incorrectly marked processed when
-- the general queue exhausted its retry limit after a hard Worker failure.
-- No 8-K event was persisted for this accession, so it is safe to retry once
-- the terminal-state bookkeeping bug is fixed in src/main.py.

UPDATE filings
SET processed = 0,
    enqueued_at = NULL
WHERE accession_number = '0001493152-26-043593'
  AND form_type LIKE '8-K%'
  AND NOT EXISTS (
      SELECT 1
      FROM eight_k_events e
      WHERE e.filing_id = filings.id
  );

UPDATE processing_queue
SET status = 'pending',
    attempts = 0,
    started_at = NULL,
    processed_at = NULL,
    last_error = 'Retrying after queue terminal-state bookkeeping fix'
WHERE filing_id IN (
    SELECT id
    FROM filings
    WHERE accession_number = '0001493152-26-043593'
      AND form_type LIKE '8-K%'
)
  AND status = 'error';
