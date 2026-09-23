-- v0.6.7: wake ordinary 13F reports whose explicit predecessor is already done.
-- This is non-destructive: parsed reports and institutional positions are preserved.

-- Create a fresh prepare work item for each currently stranded direct successor.
INSERT OR IGNORE INTO thirteen_f_work_items (report_id, work_key, stage, status)
SELECT
    r.id,
    'prepare_after_prior:' || CAST(r.predecessor_report_id AS TEXT),
    'prepare',
    'pending'
FROM thirteen_f_reports r
JOIN thirteen_f_reports p ON p.id = r.predecessor_report_id
WHERE r.status = 'waiting_predecessor'
  AND p.status = 'done';

-- Release any stale enqueue claim on those recovery work items so Cron can publish them.
UPDATE thirteen_f_work_items
SET status='pending',
    attempts=0,
    started_at=NULL,
    enqueued_at=NULL,
    processed_at=NULL,
    last_error=NULL
WHERE stage='prepare'
  AND work_key LIKE 'prepare_after_prior:%'
  AND report_id IN (
      SELECT r.id
      FROM thirteen_f_reports r
      JOIN thirteen_f_reports p ON p.id = r.predecessor_report_id
      WHERE r.status='waiting_predecessor'
        AND p.status='done'
  );

-- Mark only those direct successors runnable. Later quarters remain waiting until
-- their own predecessor completes, preserving exact quarter sequencing.
UPDATE thirteen_f_reports
SET status='pending',
    started_at=NULL,
    last_error=NULL
WHERE status='waiting_predecessor'
  AND predecessor_report_id IN (
      SELECT id FROM thirteen_f_reports WHERE status='done'
  );

-- Institutional scoring remains disabled until production signal validation.
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
