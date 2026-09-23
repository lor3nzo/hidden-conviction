-- v0.7.4: rebuild all 8-K / 8-K/A semantic events under the stricter
-- item-gated, section-local classifier.
--
-- Existing 8-K events are neutralized immediately so stale pre-v0.7.4 event
-- scores cannot affect public HCS while the queue rebuild is in progress.
UPDATE eight_k_events
SET event_score = 0,
    sentiment = 'neutral',
    matched_keywords = '[]'
WHERE filing_id IN (
    SELECT id FROM filings WHERE form_type LIKE '8-K%'
);

-- Remove stale event contribution from score rows before the rebuild. Preserve
-- the other signal families and keep the intentionally gated whale score at 0.
UPDATE scores
SET event_score = 0,
    whale_score = 0,
    convergence_bonus = MIN(
      15.0,
      MAX(
        0,
        ((CASE WHEN insider_score > 0 THEN 1 ELSE 0 END)
         + (CASE WHEN ownership_score > 0 THEN 1 ELSE 0 END)
         - 1) * 4.0
      )
    );

UPDATE scores
SET raw_score = insider_score
              + cluster_score
              + ownership_score
              + whale_score
              + event_score
              + capital_allocation_score
              + convergence_bonus
              - penalty;

UPDATE scores
SET hcs_score = MAX(0, MIN(100, ROUND(raw_score, 1)));

UPDATE scores
SET classification = CASE
    WHEN hcs_score >= 90 THEN 'Exceptional Conviction'
    WHEN hcs_score >= 80 THEN 'High Conviction'
    WHEN hcs_score >= 70 THEN 'Watch'
    WHEN hcs_score >= 60 THEN 'Emerging'
    ELSE 'No Signal'
END;

-- Reprocess all previously discovered 8-K filings with the new classifier.
UPDATE filings
SET processed = 0,
    enqueued_at = NULL
WHERE form_type LIKE '8-K%';

UPDATE processing_queue
SET status = 'pending',
    attempts = 0,
    started_at = NULL,
    processed_at = NULL,
    last_error = NULL
WHERE filing_id IN (
    SELECT id FROM filings WHERE form_type LIKE '8-K%'
);
