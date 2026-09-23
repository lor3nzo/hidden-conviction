-- v0.8.0: public issuer-universe hygiene + controlled 13F activation.
--
-- Universe policy:
--   * obvious funds/ETFs/investment vehicles are excluded from public HCS ranking
--   * obvious blank-check/acquisition vehicles are excluded
--   * ordinary companies, including REITs with "Trust" in the name, remain eligible
--
-- 13F policy:
--   * 13F is confirmation only; it cannot create HCS conviction by itself
--   * only the latest validated position per manager/CUSIP can contribute
--   * only existing_position/new_position statuses with no corporate-action flag qualify
--   * current contribution remains capped by the existing 0-15 position score

ALTER TABLE companies ADD COLUMN issuer_type TEXT NOT NULL DEFAULT 'operating_company';
ALTER TABLE companies ADD COLUMN hcs_eligible INTEGER NOT NULL DEFAULT 1;
ALTER TABLE companies ADD COLUMN hcs_exclusion_reason TEXT;
ALTER TABLE companies ADD COLUMN universe_version TEXT NOT NULL DEFAULT 'universe-v0.8.0';

CREATE INDEX IF NOT EXISTS idx_companies_hcs_eligible
  ON companies(hcs_eligible, issuer_type, cik);

-- Start from a permissive operating-company default. Runtime code uses the same
-- conservative Python classifier for new issuers after this backfill.
UPDATE companies
SET issuer_type='operating_company',
    hcs_eligible=1,
    hcs_exclusion_reason=NULL,
    universe_version='universe-v0.8.0';

-- Explicit fund / investment-vehicle markers. Avoid a generic TRUST exclusion
-- because operating companies and REITs can legitimately contain that word.
UPDATE companies
SET issuer_type='fund_or_investment_vehicle',
    hcs_eligible=0,
    hcs_exclusion_reason='Excluded from public HCS ranking because the issuer appears to be an investment fund or exchange-traded vehicle.',
    universe_version='universe-v0.8.0'
WHERE
       UPPER(COALESCE(company_name,'')) LIKE '% FUND %'
    OR UPPER(COALESCE(company_name,'')) LIKE '% FUND,%'
    OR UPPER(COALESCE(company_name,'')) LIKE '% FUND.%'
    OR UPPER(COALESCE(company_name,'')) LIKE '% FUND'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ETF %'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ETF,%'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ETF.%'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ETF'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ETN %'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ETN,%'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ETN.%'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ETN'
    OR UPPER(COALESCE(company_name,'')) LIKE '%EXCHANGE TRADED%'
    OR UPPER(COALESCE(company_name,'')) LIKE '%SERIES TRUST%'
    OR UPPER(COALESCE(company_name,'')) LIKE '%INVESTMENT TRUST%'
    OR UPPER(COALESCE(company_name,'')) LIKE 'ISHARES %'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ISHARES %'
    OR UPPER(COALESCE(company_name,'')) LIKE 'SPDR %'
    OR UPPER(COALESCE(company_name,'')) LIKE '% SPDR %'
    OR UPPER(COALESCE(company_name,'')) LIKE 'PROSHARES %'
    OR UPPER(COALESCE(company_name,'')) LIKE 'DIREXION %'
    OR UPPER(COALESCE(company_name,'')) LIKE 'WISDOMTREE %'
    OR UPPER(COALESCE(company_name,'')) LIKE 'VANECK %'
    OR UPPER(COALESCE(company_name,'')) LIKE 'GLOBAL X %'
    OR UPPER(COALESCE(company_name,'')) LIKE 'VANGUARD INDEX %'
    OR UPPER(COALESCE(company_name,'')) LIKE 'SCHWAB STRATEGIC TR%';

-- Obvious SPAC / blank-check / acquisition vehicles. These are kept in the
-- database for source evidence but removed from public operating-company ranks.
UPDATE companies
SET issuer_type='acquisition_vehicle',
    hcs_eligible=0,
    hcs_exclusion_reason='Excluded from public HCS ranking because the issuer appears to be a blank-check or acquisition vehicle.',
    universe_version='universe-v0.8.0'
WHERE hcs_eligible=1
  AND (
       UPPER(COALESCE(company_name,'')) LIKE '%SPECIAL PURPOSE ACQUISITION%'
    OR UPPER(COALESCE(company_name,'')) LIKE '%BLANK CHECK%'
    OR UPPER(COALESCE(company_name,'')) LIKE '% SPAC %'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ACQUISITION CORP'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ACQUISITION CORP.%'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ACQUISITION CORP %'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ACQUISITION CORPORATION%'
    OR UPPER(COALESCE(company_name,'')) LIKE '% ACQUISITION COMPANY%'
  );

-- Activate delayed institutional confirmation on today's materialized scores.
-- A 13F candidate contributes only if a fresher positive family is already
-- active. Excluded issuers and 13F-only issuers remain at whale_score = 0.
UPDATE scores
SET whale_score = CASE
    WHEN EXISTS (
        SELECT 1 FROM companies c
        WHERE c.cik = scores.cik AND c.hcs_eligible = 1
    )
    AND (
           insider_score > 0
        OR ownership_score > 0
        OR event_score > 0
        OR capital_allocation_score > 0
    )
    THEN COALESCE((
        WITH ranked AS (
            SELECT
                ip.position_score,
                ip.value_dollars,
                ip.manager_cik,
                ip.cusip,
                ip.filing_id,
                ip.period_of_report,
                ip.comparison_status,
                ip.corporate_action_suspected,
                ROW_NUMBER() OVER (
                    PARTITION BY ip.manager_cik, ip.cusip
                    ORDER BY ip.period_of_report DESC, ip.filing_id DESC
                ) AS rn
            FROM institutional_positions ip
            WHERE ip.issuer_cik = scores.cik
              AND ip.period_of_report >= DATE('now', '-220 day')
        )
        SELECT position_score
        FROM ranked
        WHERE rn = 1
          AND position_score > 0
          AND comparison_status IN ('existing_position', 'new_position')
          AND COALESCE(corporate_action_suspected, 0) = 0
        ORDER BY position_score DESC, value_dollars DESC
        LIMIT 1
    ), 0)
    ELSE 0
END
WHERE score_date = DATE('now');

-- Convergence continues to count independent positive families. Cluster is a
-- modifier of insider buying, not a separate family.
UPDATE scores
SET convergence_bonus = MIN(15, MAX(0,
    ((CASE WHEN insider_score > 0 THEN 1 ELSE 0 END) +
     (CASE WHEN ownership_score > 0 THEN 1 ELSE 0 END) +
     (CASE WHEN whale_score > 0 THEN 1 ELSE 0 END) +
     (CASE WHEN event_score > 0 THEN 1 ELSE 0 END) - 1) * 4
))
WHERE score_date = DATE('now');

UPDATE scores
SET raw_score = insider_score + cluster_score + ownership_score + whale_score +
                event_score + capital_allocation_score + convergence_bonus - penalty
WHERE score_date = DATE('now');

UPDATE scores
SET hcs_score = MIN(100, MAX(0, ROUND(raw_score, 1)))
WHERE score_date = DATE('now');

UPDATE scores
SET classification = CASE
    WHEN hcs_score >= 90 THEN 'Exceptional Conviction'
    WHEN hcs_score >= 80 THEN 'High Conviction'
    WHEN hcs_score >= 70 THEN 'Watch'
    WHEN hcs_score >= 60 THEN 'Emerging'
    ELSE 'No Signal'
END
WHERE score_date = DATE('now');
