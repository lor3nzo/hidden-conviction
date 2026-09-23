-- v0.7.7: Item 4.01 semantic-context hardening.
-- Re-score only existing filings that contain Item 4.01. The parser itself is
-- unchanged; this release fixes negation and historical-event interpretation in
-- the scoring layer.
UPDATE filings
SET processed = 0,
    enqueued_at = NULL
WHERE id IN (
    SELECT e.filing_id
    FROM eight_k_events e
    WHERE e.item_numbers LIKE '%"4.01"%'
      AND COALESCE(e.scoring_version, '') <> '8k-score-v0.7.7'
);

UPDATE processing_queue
SET status = 'pending',
    attempts = 0,
    started_at = NULL,
    processed_at = NULL,
    last_error = NULL
WHERE filing_id IN (
    SELECT e.filing_id
    FROM eight_k_events e
    WHERE e.item_numbers LIKE '%"4.01"%'
      AND COALESCE(e.scoring_version, '') <> '8k-score-v0.7.7'
);
