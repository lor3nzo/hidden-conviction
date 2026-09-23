-- v0.7.5: narrow Item 3.01 severe scoring to explicit delisting,
-- suspension, or removal outcomes. A Staff Determination Letter by itself is
-- treated as listing noncompliance unless the filing states an operational
-- consequence such as trading suspension or effective delisting.
--
-- Reprocess only filings whose existing 8-K event includes Item 3.01.
UPDATE filings
SET processed = 0,
    enqueued_at = NULL
WHERE id IN (
    SELECT filing_id
    FROM eight_k_events
    WHERE item_numbers LIKE '%"3.01"%'
);

UPDATE processing_queue
SET status = 'pending',
    attempts = 0,
    started_at = NULL,
    processed_at = NULL,
    last_error = NULL
WHERE filing_id IN (
    SELECT filing_id
    FROM eight_k_events
    WHERE item_numbers LIKE '%"3.01"%'
);
