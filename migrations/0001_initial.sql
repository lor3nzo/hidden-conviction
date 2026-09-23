PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cik TEXT NOT NULL UNIQUE,
  ticker TEXT,
  company_name TEXT,
  exchange TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);

CREATE TABLE IF NOT EXISTS company_tickers (
  cik TEXT NOT NULL,
  ticker TEXT NOT NULL,
  exchange TEXT,
  source TEXT NOT NULL DEFAULT 'sec',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (cik, ticker)
);

CREATE INDEX IF NOT EXISTS idx_company_tickers_ticker ON company_tickers(ticker);

CREATE TABLE IF NOT EXISTS filings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  accession_number TEXT NOT NULL UNIQUE,
  cik TEXT,
  company_name TEXT,
  form_type TEXT NOT NULL,
  filed_at TEXT NOT NULL,
  filing_url TEXT NOT NULL,
  primary_document TEXT,
  source TEXT,
  processed INTEGER NOT NULL DEFAULT 0,
  enqueued_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_filings_form_date ON filings(form_type, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_filings_cik_date ON filings(cik, filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_filings_processed ON filings(processed, filed_at);

CREATE TABLE IF NOT EXISTS insider_transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id INTEGER NOT NULL,
  cik TEXT NOT NULL,
  insider_name TEXT,
  insider_role TEXT,
  is_director INTEGER NOT NULL DEFAULT 0,
  is_officer INTEGER NOT NULL DEFAULT 0,
  officer_title TEXT,
  transaction_code TEXT,
  transaction_date TEXT,
  shares REAL,
  price REAL,
  transaction_value REAL,
  shares_owned_after REAL,
  ownership_type TEXT,
  is_open_market_purchase INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (filing_id) REFERENCES filings(id),
  UNIQUE(filing_id, insider_name, transaction_code, transaction_date, shares, price)
);

CREATE INDEX IF NOT EXISTS idx_insider_cik_date ON insider_transactions(cik, transaction_date DESC);
CREATE INDEX IF NOT EXISTS idx_insider_purchase ON insider_transactions(is_open_market_purchase, transaction_date DESC);

CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cik TEXT NOT NULL,
  score_date TEXT NOT NULL,
  insider_score REAL NOT NULL DEFAULT 0,
  cluster_score REAL NOT NULL DEFAULT 0,
  ownership_score REAL NOT NULL DEFAULT 0,
  whale_score REAL NOT NULL DEFAULT 0,
  event_score REAL NOT NULL DEFAULT 0,
  capital_allocation_score REAL NOT NULL DEFAULT 0,
  convergence_bonus REAL NOT NULL DEFAULT 0,
  penalty REAL NOT NULL DEFAULT 0,
  raw_score REAL NOT NULL DEFAULT 0,
  hcs_score REAL NOT NULL DEFAULT 0,
  classification TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(cik, score_date)
);

CREATE INDEX IF NOT EXISTS idx_scores_hcs ON scores(hcs_score DESC, score_date DESC);

CREATE TABLE IF NOT EXISTS processing_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filing_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 100,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_at TEXT,
  last_error TEXT,
  FOREIGN KEY (filing_id) REFERENCES filings(id)
);

CREATE INDEX IF NOT EXISTS idx_queue_status_priority ON processing_queue(status, priority, id);
