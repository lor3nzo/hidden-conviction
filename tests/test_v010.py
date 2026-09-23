from pathlib import Path
import sqlite3

from src.app_meta import APP_VERSION, API_SCHEMA_VERSION, PUBLIC_HCS_THRESHOLD
from src.sec.discovery import parse_daily_master_index, daily_master_index_url

ROOT = Path(__file__).resolve().parents[1]


def test_v010_version_source_of_truth_and_package_version():
    assert tuple(map(int, APP_VERSION.split("."))) >= (0, 10, 0)
    assert API_SCHEMA_VERSION
    assert PUBLIC_HCS_THRESHOLD == 80
    main = (ROOT / "src/main.py").read_text()
    assert "VERSION = APP_VERSION" in main
    assert 'name = "hidden-conviction"' in (ROOT / "pyproject.toml").read_text()


def test_v010_public_version_endpoint_and_headers():
    main = (ROOT / "src/main.py").read_text()
    assert '@app.get("/api/version")' in main
    assert "X-Hidden-Conviction-Version" in main
    assert "X-API-Schema-Version" in main


def test_v010_footer_renders_version_and_methodology():
    html = (ROOT / "public/index.html").read_text()
    js = (ROOT / "public/app.js").read_text()
    assert 'id="footer-version"' in html
    assert "/api/version" in js
    assert "loadAppVersion" in js


def test_v010_signal_filters_and_sorting_are_bounded():
    main = (ROOT / "src/main.py").read_text()
    html = (ROOT / "public/index.html").read_text()
    for token in ("min_hcs", "max_hcs", "family", "industry", "sort", "offset"):
        assert token in main
    for control in ("filter-family", "filter-industry", "filter-min-hcs", "filter-sort"):
        assert f'id="{control}"' in html
    assert "MAX_PUBLIC_PAGE_SIZE" in main


def test_v010_ui_has_loading_error_retry_and_system_state():
    html = (ROOT / "public/index.html").read_text()
    js = (ROOT / "public/app.js").read_text()
    css = (ROOT / "public/style.css").read_text()
    assert "skeleton-card" in html
    assert "retry-button" in js
    assert 'id="system-status"' in html
    assert ".system-status" in css


def test_v010_company_research_has_why_now_timeline_industry_and_acceptance_time():
    main = (ROOT / "src/main.py").read_text()
    evidence = (ROOT / "src/public_evidence.py").read_text()
    js = (ROOT / "public/app.js").read_text()
    assert '"why_now"' in main
    assert '"timeline"' in main
    assert '"industry"' in main
    assert "accepted_at" in evidence
    assert "Signal timeline" in js
    assert "Why now?" in js


def test_v010_daily_master_index_parses_relevant_forms():
    sample = """Description: Master Index of EDGAR Dissemination Feed\nCIK|Company Name|Form Type|Date Filed|Filename\n1000045|ACME INC|4|2026-09-22|edgar/data/1000045/0001000045-26-000001.txt\n1000046|BETA INC|8-K|2026-09-22|edgar/data/1000046/0001000046-26-000002.txt\n1000047|GAMMA INC|10-K|2026-09-22|edgar/data/1000047/0001000047-26-000003.txt\n1000048|DELTA INC|SC 13D|2026-09-22|edgar/data/1000048/0001000048-26-000004.txt\n"""
    rows = parse_daily_master_index(sample)
    assert len(rows) == 3
    forms = {row["form_type"] for row in rows}
    assert forms == {"4", "8-K", "SCHEDULE 13D"}
    assert all(row["filing_url"].startswith("https://www.sec.gov/Archives/") for row in rows)
    assert daily_master_index_url("2026-09-22").endswith("/2026/QTR3/master.20260922.idx")


def test_v010_discovery_saturation_triggers_backfill_and_watermarks():
    main = (ROOT / "src/main.py").read_text()
    migration = (ROOT / "migrations/0029_v010_product_observability.sql").read_text()
    assert "len(rows) >= 95" in main
    assert "discover_daily_index" in main
    assert "discovery_watermarks" in main
    assert "needs_backfill" in migration


def test_v010_runtime_and_pipeline_telemetry_exist():
    main = (ROOT / "src/main.py").read_text()
    migration = (ROOT / "migrations/0029_v010_product_observability.sql").read_text()
    for table in ("runtime_status", "pipeline_metrics", "discovery_watermarks", "data_quality_issues"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration
    assert "_record_pipeline_metric" in main
    assert "latest_latency_seconds" in main
    assert "last_cron_success" in main


def test_v010_identity_enrichment_and_diagnostics_exist():
    main = (ROOT / "src/main.py").read_text()
    assert "sicDescription" in main
    assert "_refresh_company_identity_batch" in main
    assert "_audit_identity_mappings" in main
    assert '@app.get("/api/admin/diagnostics")' in main
    assert "ticker_conflicts" in main
    assert "duplicate_filing_urls" in main


def test_v010_api_error_and_metadata_contract():
    main = (ROOT / "src/main.py").read_text()
    assert "http_exception_handler" in main
    assert "validation_exception_handler" in main
    assert '"api_schema_version"' in main
    assert '"generated_at"' in main
    assert '"meta"' in main


def test_v010_deployment_scripts_cover_migrate_deploy_verify():
    deploy = (ROOT / "scripts/deploy.sh").read_text()
    verify = (ROOT / "scripts/verify-production.sh").read_text()
    smoke = (ROOT / "scripts/smoke.sh").read_text()
    assert "d1 migrations apply" in deploy
    assert "pywrangler deploy" in deploy
    assert "verify-production.sh" in deploy
    assert "/api/version" in verify
    assert "uv run pytest" in smoke


def test_v010_head_route_supports_curl_head_checks():
    edge = (ROOT / "src/public_worker.js").read_text()
    assert "request.method === 'HEAD'" in edge


def test_v010_full_migration_smoke_test():
    db = sqlite3.connect(":memory:")
    for migration in sorted((ROOT / "migrations").glob("*.sql")):
        db.executescript(migration.read_text())
    company_columns = {row[1] for row in db.execute("PRAGMA table_info(companies)")}
    filing_columns = {row[1] for row in db.execute("PRAGMA table_info(filings)")}
    assert {"sic", "industry", "identity_checked_at"}.issubset(company_columns)
    assert "accepted_at" in filing_columns
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runtime_status", "discovery_watermarks", "pipeline_metrics", "data_quality_issues"}.issubset(tables)


def test_v010_signal_and_candidate_thresholds_cannot_be_overridden():
    main = (ROOT / "src/main.py").read_text()
    assert "min_hcs = max(float(PUBLIC_HCS_THRESHOLD)" in main
    assert "min_hcs = max(0.01, min(float(min_hcs), PUBLIC_HCS_THRESHOLD - 0.01))" in main
