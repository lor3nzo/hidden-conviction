-- v0.6.6: remove 13F prepare fan-out and recover into serial chunk chaining.
-- This migration preserves parsed report metadata and institutional_positions.
-- It does not rebuild 13F history from scratch.

-- Recover the work item that was killed by the v0.6.5 prepare fan-out, plus
-- any other hard-killed 13F stage. Reset attempts so the new implementation
-- gets a clean execution budget.
UPDATE thirteen_f_work_items
SET status='pending',
    attempts=0,
    started_at=NULL,
    enqueued_at=NULL,
    processed_at=NULL,
    last_error=COALESCE(last_error, 'Recovered for v0.6.6 serial chunk chaining')
WHERE status='processing';

-- Old v0.6.5 prepare jobs may already have published many chunk messages.
-- Clear D1 enqueue claims for pending chunks. The v0.6.6 consumer safely ACKs
-- out-of-order legacy messages and the serial chain republishes each chunk only
-- when its predecessor is complete.
UPDATE thirteen_f_work_items
SET enqueued_at=NULL
WHERE stage='chunk' AND status='pending';

-- Reconcile progress from durable completed chunk work rather than trusting an
-- increment counter that may have been interrupted by a hard Worker kill.
UPDATE thirteen_f_reports
SET chunks_done=(
    SELECT COUNT(*)
    FROM thirteen_f_work_items w
    WHERE w.report_id=thirteen_f_reports.id
      AND w.stage='chunk'
      AND w.status='done'
)
WHERE status='chunking';

-- Keep the institutional signal disabled until the serial pipeline completes
-- validation in production.
UPDATE scores
SET whale_score = 0,
    convergence_bonus = MIN(15, MAX(0,
        ((CASE WHEN insider_score > 0 THEN 1 ELSE 0 END) +
         (CASE WHEN ownership_score > 0 THEN 1 ELSE 0 END) +
         (CASE WHEN event_score > 0 THEN 1 ELSE 0 END) - 1) * 4
    ));

UPDATE scores
SET raw_score = insider_score + cluster_score + ownership_score + whale_score + event_score + convergence_bonus,
    hcs_score = MIN(100, MAX(0, ROUND(insider_score + cluster_score + ownership_score + whale_score + event_score + convergence_bonus, 1)));

UPDATE scores
SET classification = CASE
    WHEN hcs_score >= 90 THEN 'Exceptional Conviction'
    WHEN hcs_score >= 80 THEN 'High Conviction'
    WHEN hcs_score >= 70 THEN 'Watch'
    WHEN hcs_score >= 60 THEN 'Emerging'
    ELSE 'No Signal'
END;
