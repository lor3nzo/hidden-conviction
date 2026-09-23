-- Hidden Conviction v0.8.2
-- Operational indexes for unique-security 13F identity-resolution backlog draining.
-- No scoring, mapping, or historical HCS data is changed by this migration.

CREATE INDEX IF NOT EXISTS idx_institutional_resolution_cusip
ON institutional_positions(cusip, issuer_mapping_status);

CREATE INDEX IF NOT EXISTS idx_institutional_resolution_priority
ON institutional_positions(issuer_mapping_status, position_score, period_of_report, filing_id);
