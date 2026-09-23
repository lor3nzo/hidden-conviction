-- v0.6.4: rebuild 13F data after attachment-coverage and scoring fixes.
-- whale_score remains gated off until post-deploy validation passes.

DELETE FROM thirteen_f_work_items;
DELETE FROM institutional_positions;
UPDATE thirteen_f_reports SET parent_report_id = NULL, predecessor_report_id = NULL;
DELETE FROM thirteen_f_reports;

INSERT INTO thirteen_f_reports (filing_id)
SELECT id
FROM filings
WHERE form_type LIKE '13F-HR%'
  AND COALESCE(source, '') <> 'historical-13f-predecessor';

INSERT INTO thirteen_f_work_items (report_id, work_key, stage)
SELECT id, 'prepare', 'prepare'
FROM thirteen_f_reports;

UPDATE filings
SET processed = 0
WHERE form_type LIKE '13F-HR%'
  AND COALESCE(source, '') <> 'historical-13f-predecessor';

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
