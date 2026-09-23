-- v0.4.2: identify each Schedule 13 economic group by the SEC reporting filer.
-- This prevents one large holder from being compared with a different holder
-- merely because both own the same issuer.

ALTER TABLE ownership_groups ADD COLUMN group_key TEXT;
ALTER TABLE ownership_groups ADD COLUMN predecessor_accession TEXT;

CREATE INDEX IF NOT EXISTS idx_ownership_groups_group_history
  ON ownership_groups(issuer_cik, group_key, event_date ASC, filing_id ASC);

-- Existing rows do not yet have a reliable SEC reporting-filer key. The
-- application fills these when the filings are reprocessed under v0.4.2.
