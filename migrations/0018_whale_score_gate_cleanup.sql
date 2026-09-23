-- v0.7.1: enforce the intentionally disabled 13F/whale contribution in scores.
-- The main Worker had a duplicate recompute path that could write a live
-- institutional position_score into scores.whale_score even though the canonical
-- recompute module kept the gate at zero.

UPDATE scores
SET whale_score = 0,
    convergence_bonus = MIN(
      15.0,
      MAX(
        0,
        ((CASE WHEN insider_score > 0 THEN 1 ELSE 0 END)
         + (CASE WHEN ownership_score > 0 THEN 1 ELSE 0 END)
         + (CASE WHEN event_score > 0 THEN 1 ELSE 0 END)
         - 1) * 4.0
      )
    )
WHERE whale_score <> 0;

UPDATE scores
SET raw_score = insider_score
              + cluster_score
              + ownership_score
              + whale_score
              + event_score
              + capital_allocation_score
              + convergence_bonus
              - penalty
WHERE whale_score = 0;

UPDATE scores
SET hcs_score = MAX(0, MIN(100, ROUND(raw_score, 1)))
WHERE whale_score = 0;

UPDATE scores
SET classification = CASE
    WHEN hcs_score >= 90 THEN 'Exceptional Conviction'
    WHEN hcs_score >= 80 THEN 'High Conviction'
    WHEN hcs_score >= 70 THEN 'Watch'
    WHEN hcs_score >= 60 THEN 'Emerging'
    ELSE 'No Signal'
END
WHERE whale_score = 0;
