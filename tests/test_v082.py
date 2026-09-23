from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


def test_v082_application_version_bumps_without_scoring_version_change():
    main = (ROOT / "src/main.py").read_text()
    security = (ROOT / "src/security_master.py").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'VERSION = APP_VERSION' in main
    assert 'name = "hidden-conviction"' in pyproject
    assert 'HCS_VERSION = "hcs-v0.8.1"' in security
    assert 'INSTITUTIONAL_VERSION = "institutional-v0.8.1"' in security
    assert 'SECURITY_MASTER_VERSION = "security-master-v0.8.1"' in security


def test_v082_resolution_is_unique_security_batched_and_positive_first():
    pipeline = (ROOT / "src/pipeline_13f.py").read_text()
    assert "SECURITY_RESOLUTION_BATCH = 20" in pipeline
    assert "SECURITY_RESOLUTION_HARD_CAP = 25" in pipeline
    assert "ROW_NUMBER() OVER" in pipeline
    assert "'CUSIP:' || UPPER(TRIM(ip.cusip))" in pipeline
    assert "CASE WHEN ip.position_score > 0 THEN 0 ELSE 1 END" in pipeline
    assert "ip.period_of_report DESC" in pipeline
    assert "WHERE UPPER(TRIM(cusip))=?" in pipeline
    assert "processed_securities" in pipeline
    assert "processed_positions" in pipeline


def test_v082_no_cusip_rows_are_not_grouped_by_name():
    pipeline = (ROOT / "src/pipeline_13f.py").read_text()
    assert "'POSITION:' || CAST(ip.id AS TEXT)" in pipeline
    assert "name-only evidence cannot over-group issuers" in pipeline


def test_v082_cron_batches_are_bounded_and_logged():
    main = (ROOT / "src/main.py").read_text()
    assert "SCORE_RECOMPUTE_BATCH = 15" in main
    assert "SCORE_RECOMPUTE_HARD_CAP = 25" in main
    assert "limit=SECURITY_RESOLUTION_BATCH" in main
    assert "limit=SCORE_RECOMPUTE_BATCH" in main
    assert '"event": "background_maintenance_progress"' in main


def test_v082_13f_health_has_backlog_diagnostics():
    main = (ROOT / "src/main.py").read_text()
    for token in (
        "pending_positions",
        "pending_unique_securities",
        "pending_positive_positions",
        "pending_positive_securities",
        "estimated_remaining_runs",
        "estimated_positive_runs",
        "security_resolution_backlog",
        "score_recompute_backlog",
        '"grain": "unique CUSIP; no-CUSIP rows remain position-scoped"',
    ):
        assert token in main


def test_v082_migration_is_operational_only():
    migration = (ROOT / "migrations/0027_security_resolution_throughput.sql").read_text()
    upper = migration.upper()
    assert "CREATE INDEX" in upper
    assert "UPDATE " not in upper
    assert "DELETE " not in upper
    assert "INSERT " not in upper
    assert "ALTER TABLE" not in upper


def test_v082_full_migration_smoke_test():
    db = sqlite3.connect(":memory:")
    for migration in sorted((ROOT / "migrations").glob("*.sql")):
        db.executescript(migration.read_text())
    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_institutional_resolution_cusip" in indexes
    assert "idx_institutional_resolution_priority" in indexes
