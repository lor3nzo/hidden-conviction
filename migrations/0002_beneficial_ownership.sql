CREATE TABLE IF NOT EXISTS beneficial_ownership (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id INTEGER NOT NULL,
  issuer_cik TEXT NOT NULL,
  investor_cik TEXT,
  investor_name TEXT NOT NULL,
  schedule_type TEXT NOT NULL,
  event_date TEXT,
  shares_owned REAL,
  ownership_pct REAL,
  previous_ownership_pct REAL,
  ownership_change_pp REAL,
  reporting_person_types TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (filing_id) REFERENCES filings(id),
  UNIQUE(filing_id, investor_name, shares_owned, ownership_pct)
);

CREATE INDEX IF NOT EXISTS idx_beneficial_issuer_date
  ON beneficial_ownership(issuer_cik, event_date DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_beneficial_investor
  ON beneficial_ownership(issuer_cik, investor_cik, investor_name, event_date DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_beneficial_pct
  ON beneficial_ownership(ownership_pct DESC, event_date DESC);
