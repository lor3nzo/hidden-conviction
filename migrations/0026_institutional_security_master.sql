-- v0.8.1: Institutional Security Master + provenance + universe controls.
-- Historical score rows are preserved. Only today's scores for newly mapped
-- issuers are queued for an explicit recompute by the Worker.

ALTER TABLE companies ADD COLUMN universe_rule TEXT NOT NULL DEFAULT 'default_operating_company';
ALTER TABLE companies ADD COLUMN universe_reason TEXT;

ALTER TABLE institutional_positions ADD COLUMN normalized_issuer_name TEXT;
ALTER TABLE institutional_positions ADD COLUMN issuer_mapping_method TEXT;
ALTER TABLE institutional_positions ADD COLUMN issuer_mapping_confidence REAL NOT NULL DEFAULT 0;
ALTER TABLE institutional_positions ADD COLUMN issuer_mapping_status TEXT NOT NULL DEFAULT 'unresolved';
ALTER TABLE institutional_positions ADD COLUMN issuer_mapping_version TEXT;
ALTER TABLE institutional_positions ADD COLUMN issuer_mapping_updated_at TEXT;

ALTER TABLE scores ADD COLUMN confirming_manager_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scores ADD COLUMN strongest_manager_score REAL NOT NULL DEFAULT 0;
ALTER TABLE scores ADD COLUMN aggregate_manager_signal REAL NOT NULL DEFAULT 0;
ALTER TABLE scores ADD COLUMN hcs_version TEXT NOT NULL DEFAULT 'hcs-v0.8.0';
ALTER TABLE scores ADD COLUMN institutional_version TEXT NOT NULL DEFAULT 'institutional-v0.8.0';
ALTER TABLE scores ADD COLUMN universe_version TEXT NOT NULL DEFAULT 'universe-v0.8.0';
ALTER TABLE scores ADD COLUMN scored_at TEXT;

CREATE TABLE IF NOT EXISTS issuer_universe_overrides (
  cik TEXT PRIMARY KEY,
  force_eligible INTEGER,
  force_issuer_type TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (force_eligible IS NULL OR force_eligible IN (0,1))
);

CREATE TABLE IF NOT EXISTS security_aliases (
  alias_name_norm TEXT PRIMARY KEY,
  issuer_cik TEXT NOT NULL,
  alias_label TEXT,
  approved INTEGER NOT NULL DEFAULT 1,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_security_aliases_cik ON security_aliases(issuer_cik, approved);

CREATE TABLE IF NOT EXISTS security_master (
  cusip TEXT PRIMARY KEY,
  ticker TEXT,
  issuer_cik TEXT,
  issuer_name TEXT,
  normalized_issuer_name TEXT,
  mapping_method TEXT NOT NULL,
  mapping_confidence REAL NOT NULL DEFAULT 0,
  mapping_status TEXT NOT NULL DEFAULT 'unresolved',
  mapping_version TEXT NOT NULL DEFAULT 'security-master-v0.8.1',
  first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  source_count INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (mapping_status IN ('resolved','unresolved','conflict')),
  CHECK (mapping_confidence >= 0 AND mapping_confidence <= 1)
);
CREATE INDEX IF NOT EXISTS idx_security_master_cik ON security_master(issuer_cik, mapping_status);
CREATE INDEX IF NOT EXISTS idx_security_master_name ON security_master(normalized_issuer_name, mapping_status);

CREATE TABLE IF NOT EXISTS score_component_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cik TEXT NOT NULL,
  score_date TEXT NOT NULL,
  component TEXT NOT NULL,
  component_score REAL NOT NULL DEFAULT 0,
  source_type TEXT,
  source_id TEXT,
  event_date TEXT,
  filing_date TEXT,
  component_age_days INTEGER,
  detail_json TEXT,
  hcs_version TEXT NOT NULL,
  institutional_version TEXT NOT NULL,
  universe_version TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(cik, score_date, component)
);
CREATE INDEX IF NOT EXISTS idx_component_evidence_cik_date
  ON score_component_evidence(cik, score_date DESC, component);

CREATE TABLE IF NOT EXISTS score_recompute_queue (
  cik TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  processed_at TEXT,
  last_error TEXT,
  CHECK (status IN ('pending','processing','done','error'))
);
CREATE INDEX IF NOT EXISTS idx_score_recompute_status
  ON score_recompute_queue(status, created_at, cik);

CREATE TABLE IF NOT EXISTS security_resolution_queue (
  position_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  processed_at TEXT,
  last_error TEXT,
  CHECK (status IN ('pending','processing','done','unresolved','conflict','error'))
);
CREATE INDEX IF NOT EXISTS idx_security_resolution_status
  ON security_resolution_queue(status, created_at, position_id);

DROP VIEW IF EXISTS current_scores;
CREATE VIEW current_scores AS
SELECT s.*
FROM scores s
WHERE s.id = (
  SELECT s2.id FROM scores s2
  WHERE s2.cik=s.cik
  ORDER BY s2.score_date DESC, s2.id DESC
  LIMIT 1
);

-- Backfill audited universe-rule metadata from the already validated v0.8.0
-- classifications. This does not change eligibility.
UPDATE companies
SET universe_rule = CASE
      WHEN issuer_type='fund_or_investment_vehicle' THEN 'fund_name_rule'
      WHEN issuer_type='acquisition_vehicle' THEN 'acquisition_vehicle_name_rule'
      ELSE 'default_operating_company'
    END,
    universe_reason = CASE
      WHEN hcs_eligible=0 THEN hcs_exclusion_reason
      ELSE 'Eligible under the default operating-company universe rule.'
    END,
    universe_version='universe-v0.8.1';

-- Existing linked rows become explicit legacy mappings. The security master is
-- seeded only from CUSIPs that have exactly one historical CIK association.
UPDATE institutional_positions
SET normalized_issuer_name=UPPER(TRIM(issuer_name)),
    issuer_mapping_method=CASE WHEN issuer_cik IS NOT NULL THEN 'legacy_existing' ELSE 'unresolved' END,
    issuer_mapping_confidence=CASE WHEN issuer_cik IS NOT NULL THEN 0.90 ELSE 0 END,
    issuer_mapping_status=CASE WHEN issuer_cik IS NOT NULL THEN 'resolved' ELSE 'unresolved' END,
    issuer_mapping_version='security-master-v0.8.1',
    issuer_mapping_updated_at=CURRENT_TIMESTAMP;

INSERT OR REPLACE INTO security_master
(cusip, ticker, issuer_cik, issuer_name, normalized_issuer_name,
 mapping_method, mapping_confidence, mapping_status, mapping_version,
 first_seen, last_seen, source_count, updated_at)
SELECT
  UPPER(cusip), MAX(ticker), MIN(issuer_cik), MAX(issuer_name),
  UPPER(TRIM(MAX(issuer_name))), 'legacy_existing', 0.90, 'resolved',
  'security-master-v0.8.1', MIN(created_at), MAX(created_at), COUNT(*), CURRENT_TIMESTAMP
FROM institutional_positions
WHERE issuer_cik IS NOT NULL AND COALESCE(cusip,'') <> ''
GROUP BY UPPER(cusip)
HAVING COUNT(DISTINCT issuer_cik)=1;

-- Explicitly quarantine historically conflicting CUSIP mappings.
INSERT OR REPLACE INTO security_master
(cusip, ticker, issuer_cik, issuer_name, normalized_issuer_name,
 mapping_method, mapping_confidence, mapping_status, mapping_version,
 first_seen, last_seen, source_count, updated_at)
SELECT
  UPPER(cusip), MAX(ticker), NULL, MAX(issuer_name), UPPER(TRIM(MAX(issuer_name))),
  'conflict', 0, 'conflict', 'security-master-v0.8.1',
  MIN(created_at), MAX(created_at), COUNT(*), CURRENT_TIMESTAMP
FROM institutional_positions
WHERE issuer_cik IS NOT NULL AND COALESCE(cusip,'') <> ''
GROUP BY UPPER(cusip)
HAVING COUNT(DISTINCT issuer_cik)>1;

UPDATE institutional_positions
SET issuer_cik=NULL,
    ticker=NULL,
    issuer_mapping_method='conflict',
    issuer_mapping_confidence=0,
    issuer_mapping_status='conflict',
    issuer_mapping_version='security-master-v0.8.1',
    issuer_mapping_updated_at=CURRENT_TIMESTAMP
WHERE UPPER(cusip) IN (
  SELECT cusip FROM security_master WHERE mapping_status='conflict'
);

-- Resolve previously-unmapped historical rows only through exact, conflict-free
-- CUSIP mappings learned from already-linked positions.
UPDATE institutional_positions
SET issuer_cik=(SELECT sm.issuer_cik FROM security_master sm WHERE sm.cusip=UPPER(institutional_positions.cusip)),
    ticker=COALESCE(ticker, (SELECT sm.ticker FROM security_master sm WHERE sm.cusip=UPPER(institutional_positions.cusip))),
    issuer_mapping_method='security_master_cusip',
    issuer_mapping_confidence=1.0,
    issuer_mapping_status='resolved',
    issuer_mapping_version='security-master-v0.8.1',
    issuer_mapping_updated_at=CURRENT_TIMESTAMP
WHERE issuer_cik IS NULL
  AND issuer_mapping_status='unresolved'
  AND EXISTS (
    SELECT 1 FROM security_master sm
    WHERE sm.cusip=UPPER(institutional_positions.cusip)
      AND sm.mapping_status='resolved'
      AND sm.issuer_cik IS NOT NULL
  );

-- Explicitly queue only issuers newly resolved by the security master. Historical
-- score rows remain untouched; the Worker recomputes today's materialized row.
INSERT OR IGNORE INTO score_recompute_queue(cik, reason)
SELECT DISTINCT issuer_cik, 'v0.8.1 security-master backfill'
FROM institutional_positions
WHERE issuer_mapping_method='security_master_cusip'
  AND issuer_cik IS NOT NULL;

-- Any rows still unresolved get exactly one conservative name/directory pass in
-- bounded Cron batches. Unresolved/conflicting results become terminal instead
-- of being retried forever.
INSERT OR IGNORE INTO security_resolution_queue(position_id)
SELECT id FROM institutional_positions
WHERE issuer_mapping_status='unresolved';

-- Overrides are intentionally explicit and auditable. Inserts/updates immediately
-- update the company dimension used by all public ranking endpoints.
CREATE TRIGGER IF NOT EXISTS trg_universe_override_insert
AFTER INSERT ON issuer_universe_overrides
BEGIN
  UPDATE companies
  SET hcs_eligible=COALESCE(NEW.force_eligible, hcs_eligible),
      issuer_type=COALESCE(NEW.force_issuer_type, issuer_type),
      hcs_exclusion_reason=CASE WHEN COALESCE(NEW.force_eligible, hcs_eligible)=0 THEN NEW.reason ELSE NULL END,
      universe_rule='explicit_override',
      universe_reason=NEW.reason,
      universe_version='universe-v0.8.1',
      updated_at=CURRENT_TIMESTAMP
  WHERE cik=NEW.cik;
END;

CREATE TRIGGER IF NOT EXISTS trg_universe_override_update
AFTER UPDATE ON issuer_universe_overrides
BEGIN
  UPDATE companies
  SET hcs_eligible=COALESCE(NEW.force_eligible, hcs_eligible),
      issuer_type=COALESCE(NEW.force_issuer_type, issuer_type),
      hcs_exclusion_reason=CASE WHEN COALESCE(NEW.force_eligible, hcs_eligible)=0 THEN NEW.reason ELSE NULL END,
      universe_rule='explicit_override',
      universe_reason=NEW.reason,
      universe_version='universe-v0.8.1',
      updated_at=CURRENT_TIMESTAMP
  WHERE cik=NEW.cik;
END;
