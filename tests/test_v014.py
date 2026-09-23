from pathlib import Path
import sqlite3

from src.app_meta import APP_VERSION, API_SCHEMA_VERSION
from src.sec.client import SEC_MIN_REQUEST_CYCLE_SECONDS
from src.ops import (
    choose_ingestion_policy,
    estimate_clear_minutes,
    is_transient_queue_error,
    normalize_sec_date,
    operational_state,
)

ROOT = Path(__file__).resolve().parents[1]


def _all_migrations(db):
    for migration in sorted((ROOT / "migrations").glob("*.sql")):
        db.executescript(migration.read_text())


def test_v014_version_and_schema():
    assert APP_VERSION == "0.14.0"
    assert API_SCHEMA_VERSION == "2026-09-22.v5"


def test_v014_normalizes_compact_sec_dates():
    assert normalize_sec_date("20260701") == "2026-07-01"
    assert normalize_sec_date("2026-09-22") == "2026-09-22"
    assert normalize_sec_date(None) is None


def test_v014_adaptive_ingestion_policy_protects_and_accelerates():
    protect = choose_ingestion_policy(backlog=1500, sec_failed_today=7, sec_latest_latency_seconds=2)
    assert protect.mode == "protect" and protect.target_concurrency == 1
    catch_up = choose_ingestion_policy(backlog=1500, sec_failed_today=0, sec_latest_latency_seconds=1.5)
    assert catch_up.mode == "catch_up" and catch_up.target_concurrency == 5 and catch_up.publish_limit == 225
    steady = choose_ingestion_policy(backlog=20, sec_failed_today=0, sec_latest_latency_seconds=2)
    assert steady.mode == "steady"


def test_v014_eta_uses_net_drain_rate():
    assert estimate_clear_minutes(pending=100, discovered_per_hour=50, processed_per_hour=150) == 60.0
    assert estimate_clear_minutes(pending=100, discovered_per_hour=150, processed_per_hour=100) is None
    assert estimate_clear_minutes(pending=0, discovered_per_hour=0, processed_per_hour=0) == 0.0


def test_v014_operational_state_distinguishes_delay_from_failure():
    assert operational_state(database="ok", queue_status="ok", queue_age_minutes=0, cron_status="ok", sec_status="ok", scoring_status="ok") == "operational"
    assert operational_state(database="ok", queue_status="ok", queue_age_minutes=35, cron_status="ok", sec_status="ok", scoring_status="ok") == "delayed"
    assert operational_state(database="ok", queue_status="degraded", queue_age_minutes=70, cron_status="ok", sec_status="ok", scoring_status="ok") == "delayed"
    assert operational_state(database="ok", queue_status="degraded", queue_age_minutes=70, queue_errors=3, cron_status="ok", sec_status="ok", scoring_status="ok") == "degraded"
    assert operational_state(database="error", queue_status="ok", queue_age_minutes=0, cron_status="ok", sec_status="ok", scoring_status="ok") == "failed"


def test_v014_transient_error_classifier_is_conservative():
    assert is_transient_queue_error("HTTP 503 Service unavailable")
    assert is_transient_queue_error("network timeout fetching SEC filing")
    assert not is_transient_queue_error("Unsupported permanent form payload")


def test_v014_migration_adds_controller_alert_history_and_replay_columns():
    db = sqlite3.connect(":memory:")
    _all_migrations(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("backlog_samples", "ingestion_controller_state", "ingestion_leases", "system_alerts", "deployment_metrics"):
        assert table in tables
    cols = {r[1] for r in db.execute("PRAGMA table_info(processing_queue)")}
    assert {"auto_replay_count", "last_auto_replay_at"} <= cols
    assert db.execute("SELECT value FROM runtime_status WHERE key='schema_version'").fetchone() == ("0033",)
    assert db.execute("SELECT COUNT(*) FROM ingestion_leases").fetchone() == (5,)


def test_v014_sec_request_pacing_leaves_fair_access_headroom():
    assert SEC_MIN_REQUEST_CYCLE_SECONDS >= 0.75


def test_v014_worker_and_ui_package_adaptive_operations_and_clean_kpis():
    main = (ROOT / "src/main.py").read_text()
    edge = (ROOT / "src/public_worker.js").read_text()
    app = (ROOT / "public/app.js").read_text()
    style = (ROOT / "public/style.css").read_text()
    ingest = (ROOT / "wrangler.ingest.jsonc").read_text()
    assert "_update_ingestion_controller" in main
    assert "_auto_replay_transient_failures" in main
    assert "_acquire_ingestion_lease" in main
    assert "cron_overlap_skipped" in main
    assert "cron_drain_only" in main
    assert "backlog_count >= 1000" in main
    assert "effective_publish_limit" in main
    assert '@app.post("/api/admin/replay/failures")' in main
    assert "backlog_history_24h" in main and "deployment_history" in main
    assert "Published signals</span><strong>" in edge
    assert "Threshold HCS ≥ 80" in edge
    assert "backlogTrendSvg" in edge and "Estimated clearance" in edge
    assert "operational_state" in app and "admin-replay-all" in app
    assert "grid-template-columns: minmax(0,1fr) auto" in style
    assert '"max_concurrency": 1' in ingest


def test_v014_response_build_header_remains_on_edge_and_backend():
    edge = (ROOT / "src/public_worker.js").read_text()
    main = (ROOT / "src/main.py").read_text()
    assert "X-Hidden-Conviction-Build" in edge
    assert 'response.headers["X-Hidden-Conviction-Build"]' in main


def test_v014_deploy_persists_performance_receipt():
    deploy = (ROOT / "scripts/deploy.sh").read_text()
    perf = (ROOT / "scripts/perf-budget.sh").read_text()
    assert "/api/admin/deployment-metrics" in deploy
    assert ".deploy-metrics.json" in deploy
    assert "health_ttfb_ms" in perf and "home_ttfb_ms" in perf and "system_ttfb_ms" in perf
