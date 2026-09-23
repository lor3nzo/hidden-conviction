from pathlib import Path
import sqlite3

from src.app_meta import APP_VERSION, API_SCHEMA_VERSION
from src.build_meta import BUILD_ID, BUILD_SHA

ROOT = Path(__file__).resolve().parents[1]


def _all_migrations(db: sqlite3.Connection) -> None:
    for migration in sorted((ROOT / "migrations").glob("*.sql")):
        db.executescript(migration.read_text())


def test_v012_version_schema_and_build_contract():
    assert APP_VERSION in {"0.12.0", "0.13.0", "0.13.1", "0.14.0"}
    assert API_SCHEMA_VERSION in {"2026-09-22.v3", "2026-09-22.v4", "2026-09-22.v5"}
    assert BUILD_ID
    assert BUILD_SHA
    assert f'version = "{APP_VERSION}"' in (ROOT / "pyproject.toml").read_text()


def test_v012_critical_endpoints_are_no_store_and_cache_generation_is_explicit():
    main = (ROOT / "src/main.py").read_text()
    assert 'critical_no_store = {"/api/health", "/api/version", "/api/build", "/api/system", "/api/freshness"}' in main
    assert 'X-Cache-Generation' in main
    assert 'Cache-Control"] = "no-store"' in main


def test_v012_build_endpoint_and_head_contract_exist():
    main = (ROOT / "src/main.py").read_text()
    assert '@app.get("/api/build")' in main
    assert '@app.head("/api/version")' in main
    assert '@app.head("/api/build")' in main
    assert '@app.head("/api/health")' in main
    assert '@app.head("/api/system")' in main
    assert '@app.head("/api/freshness")' in main
    assert 'commit_sha' in main and 'deployed_at' in main and 'build_id' in main


def test_v012_route_specific_ssr_and_metadata_helpers():
    edge = (ROOT / "src/public_worker.js").read_text()
    assert "function withMetadata" in edge
    assert 'rel="canonical"' in edge
    assert 'property="og:title"' in edge
    assert 'property="og:description"' in edge
    assert (ROOT / "public/company.html").exists()
    assert (ROOT / "public/system.html").exists()
    assert (ROOT / "public/methodology.html").exists()


def test_v012_company_and_system_ssr_include_live_content_not_loading_shells():
    edge = (ROOT / "src/public_worker.js").read_text()
    assert "renderCompanyPayload" in edge
    assert "renderSystem" in edge
    assert 'data-ssr-rendered="true"' in edge
    assert "Source evidence" in edge
    assert "Score history" in edge
    assert "canonical" in edge


def test_v012_cron_queue_sec_and_scoring_telemetry_exist():
    main = (ROOT / "src/main.py").read_text()
    health = (ROOT / "src/system_health.py").read_text()
    migration = (ROOT / "migrations/0031_v012_consistency_ssr.sql").read_text()
    for token in (
        "records_discovered", "records_processed", "records_failed", "scores_recomputed",
        "queue_pending_at_finish", "oldest_pending_age_minutes", "minutes_since_latest_score",
        "sec_request_metrics", "latest_latency_seconds",
    ):
        assert token in main or token in health or token in migration
    assert "_record_sec_request_metric" in main
    assert '"scoring", processed=1, latency_seconds=' in main


def test_v012_backfill_error_is_cleared_after_success():
    main = (ROOT / "src/main.py").read_text()
    health = (ROOT / "src/system_health.py").read_text()
    assert '_set_runtime_status(env, "last_daily_index_backfill_error", "")' in main
    assert 'daily_index_error' in health


def test_v012_migration_applies_on_full_schema():
    db = sqlite3.connect(":memory:")
    _all_migrations(db)
    cron_cols = {row[1] for row in db.execute("PRAGMA table_info(cron_runs)")}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"records_discovered", "records_processed", "records_failed", "scores_recomputed", "queue_pending_at_finish"}.issubset(cron_cols)
    assert "sec_request_metrics" in tables
    schema = db.execute("SELECT value FROM runtime_status WHERE key='schema_version'").fetchone()
    assert schema in {("0031",), ("0032",), ("0033",)}


def test_v012_deployment_pipeline_stamps_build_purges_optional_cache_and_checks_performance():
    deploy = (ROOT / "scripts/deploy.sh").read_text()
    verify = (ROOT / "scripts/verify-production.sh").read_text()
    assert "stamp-build.py" in deploy
    assert "purge-cache.sh" in deploy
    assert "perf-budget.sh" in deploy
    assert "/api/build" in verify
    assert "versions == {expected}" in verify
    assert "schemas" in verify


def test_v012_browser_and_accessibility_harness_is_packaged():
    package = (ROOT / "package.json").read_text()
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "test:e2e" in package
    assert "@playwright/test" in package
    assert "@axe-core/playwright" in package
    assert (ROOT / "playwright.config.mjs").exists()
    assert (ROOT / "tests_e2e/live.spec.mjs").exists()
    assert "playwright install" in ci
