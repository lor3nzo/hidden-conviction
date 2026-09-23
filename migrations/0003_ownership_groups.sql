-- v0.4.1: normalize Schedule 13 dates and add one economic ownership group per filing.

UPDATE beneficial_ownership
SET event_date =
  substr(event_date, 7, 4) || '-' || substr(event_date, 1, 2) || '-' || substr(event_date, 4, 2)
WHERE event_date GLOB '[0-9][0-9]/[0-9][0-9]/[0-9][0-9][0-9][0-9]';

CREATE TABLE IF NOT EXISTS ownership_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id INTEGER NOT NULL UNIQUE,
  issuer_cik TEXT NOT NULL,
  schedule_type TEXT NOT NULL,
  event_date TEXT,
  representative_investor_cik TEXT,
  representative_investor_name TEXT,
  group_shares_owned REAL,
  group_ownership_pct REAL,
  previous_ownership_pct REAL,
  ownership_change_pp REAL,
  member_count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (filing_id) REFERENCES filings(id)
);

CREATE INDEX IF NOT EXISTS idx_ownership_groups_issuer_date
  ON ownership_groups(issuer_cik, event_date DESC, filing_id DESC);

CREATE INDEX IF NOT EXISTS idx_ownership_groups_pct
  ON ownership_groups(group_ownership_pct DESC, event_date DESC);

-- Backfill one representative economic group per already-parsed filing.
-- The row with the largest reported ownership percentage is used as the
-- representative because joint filers often duplicate the same aggregate stake.
WITH ranked AS (
  SELECT
    bo.*,
    ROW_NUMBER() OVER (
      PARTITION BY bo.filing_id
      ORDER BY COALESCE(bo.ownership_pct, -1) DESC,
               COALESCE(bo.shares_owned, -1) DESC,
               bo.id ASC
    ) AS rn,
    COUNT(*) OVER (PARTITION BY bo.filing_id) AS member_count
  FROM beneficial_ownership bo
)
INSERT OR IGNORE INTO ownership_groups (
  filing_id,
  issuer_cik,
  schedule_type,
  event_date,
  representative_investor_cik,
  representative_investor_name,
  group_shares_owned,
  group_ownership_pct,
  member_count
)
SELECT
  filing_id,
  issuer_cik,
  schedule_type,
  event_date,
  investor_cik,
  investor_name,
  shares_owned,
  ownership_pct,
  member_count
FROM ranked
WHERE rn = 1;

-- Backfill prior percentage and delta in chronological order. D1 supports
-- SQLite window functions. The application also refreshes this history after
-- every new filing, so out-of-order queue processing stays correct.
WITH ordered AS (
  SELECT
    id,
    LAG(group_ownership_pct) OVER (
      PARTITION BY issuer_cik
      ORDER BY COALESCE(event_date, '9999-12-31') ASC, filing_id ASC
    ) AS prev_pct
  FROM ownership_groups
)
UPDATE ownership_groups
SET
  previous_ownership_pct = (
    SELECT prev_pct FROM ordered WHERE ordered.id = ownership_groups.id
  ),
  ownership_change_pp = CASE
    WHEN group_ownership_pct IS NULL THEN NULL
    WHEN (SELECT prev_pct FROM ordered WHERE ordered.id = ownership_groups.id) IS NULL THEN NULL
    ELSE ROUND(
      group_ownership_pct - (SELECT prev_pct FROM ordered WHERE ordered.id = ownership_groups.id),
      4
    )
  END;
