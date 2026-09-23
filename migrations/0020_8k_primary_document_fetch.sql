-- v0.7.3: retry the known oversized Hycroft 8-K after switching
-- general 8-K ingestion from the complete submission .txt to the SEC primary document.
UPDATE processing_queue
SET status = 'pending',
    attempts = 0,
    started_at = NULL,
    processed_at = NULL,
    last_error = NULL
WHERE filing_id IN (
    SELECT id FROM filings WHERE accession_number = '0001493152-26-043593'
)
AND NOT EXISTS (
    SELECT 1 FROM eight_k_events e
    WHERE e.filing_id = processing_queue.filing_id
);

UPDATE filings
SET processed = 0,
    enqueued_at = NULL
WHERE accession_number = '0001493152-26-043593'
AND NOT EXISTS (
    SELECT 1 FROM eight_k_events e WHERE e.filing_id = filings.id
);
