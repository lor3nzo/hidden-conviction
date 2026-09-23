-- v0.5: 8-K event intelligence + 13F institutional accumulation.

CREATE TABLE IF NOT EXISTS eight_k_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id INTEGER NOT NULL UNIQUE,
  cik TEXT,
  company_name TEXT,
  event_date TEXT,
  item_numbers TEXT,
  event_type TEXT,
  sentiment TEXT NOT NULL DEFAULT 'neutral',
  event_score REAL NOT NULL DEFAULT 0,
  matched_keywords TEXT,
  excerpt TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (filing_id) REFERENCES filings(id)
);

CREATE INDEX IF NOT EXISTS idx_eight_k_cik_date
  ON eight_k_events(cik, event_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_eight_k_score
  ON eight_k_events(event_score DESC, event_date DESC);

CREATE TABLE IF NOT EXISTS institutional_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id INTEGER NOT NULL,
  manager_cik TEXT,
  manager_name TEXT,
  filing_date TEXT,
  period_of_report TEXT,
  issuer_cik TEXT,
  ticker TEXT,
  issuer_name TEXT NOT NULL,
  cusip TEXT NOT NULL,
  title_of_class TEXT,
  shares REAL NOT NULL DEFAULT 0,
  value_thousands REAL NOT NULL DEFAULT 0,
  position_weight_pct REAL,
  previous_shares REAL,
  share_change_pct REAL,
  is_new_position INTEGER NOT NULL DEFAULT 0,
  position_score REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (filing_id) REFERENCES filings(id),
  UNIQUE(filing_id, cusip)
);

CREATE INDEX IF NOT EXISTS idx_13f_manager_period
  ON institutional_positions(manager_cik, period_of_report DESC, cusip);
CREATE INDEX IF NOT EXISTS idx_13f_issuer_date
  ON institutional_positions(issuer_cik, period_of_report DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_13f_cusip
  ON institutional_positions(manager_cik, cusip, period_of_report DESC);
CREATE INDEX IF NOT EXISTS idx_13f_score
  ON institutional_positions(position_score DESC, period_of_report DESC);
