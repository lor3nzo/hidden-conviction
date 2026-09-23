INSERT INTO companies(cik, ticker, company_name, hcs_eligible, universe_reason)
VALUES ('0000000001', 'TST', 'Test Corporation', 1, 'operating_company');

INSERT INTO scores(
  cik, score_date, hcs_score, classification, hcs_version,
  scoring_engine_version, evidence_fingerprint, reason_codes_json, score_delta
) VALUES (
  '0000000001', '2026-09-22', 42.0, 'candidate', 'test',
  'hcs-test', 'abc123', '["form4"]', 7.0
);

INSERT INTO cron_runs(run_id, status, finished_at, duration_ms, app_version)
VALUES ('seed-run', 'success', CURRENT_TIMESTAMP, 123, '0.11.0');

INSERT INTO reconciliation_runs(
  reconciliation_date, source, index_name, discovered_count, processed_count,
  pending_count, failed_count, status, detail_json
) VALUES (
  '2026-09-18', 'sec_master', 'master.20260918.idx', 10, 8, 1, 1,
  'success', '{"balanced":true}'
);
