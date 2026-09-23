from pathlib import Path
import sqlite3

from src.sec.client import validate_sec_user_agent

ROOT = Path(__file__).resolve().parents[1]


def test_v090_application_version_and_scoring_version_are_separate():
    assert 'VERSION = APP_VERSION' in (ROOT / "src/main.py").read_text()
    assert 'name = "hidden-conviction"' in (ROOT / "pyproject.toml").read_text()
    security = (ROOT / "src/security_master.py").read_text()
    assert 'HCS_VERSION = "hcs-v0.8.1"' in security


def test_v090_public_rankings_only_serve_today_scores():
    main = (ROOT / "src/main.py").read_text()
    assert main.count("s.score_date = DATE('now')") >= 2
    assert "stale_active_scores" in main


def test_v090_score_queue_is_requeue_safe():
    queue = (ROOT / "src/score_queue.py").read_text()
    pipeline = (ROOT / "src/pipeline_13f.py").read_text()
    assert "ON CONFLICT(cik) DO UPDATE SET" in queue
    assert "score_recompute_queue.status <> 'processing'" in queue
    assert "revive_errors=False" in queue
    assert "enqueue_stale_active_scores" in queue
    assert "enqueue_score_recompute" in pipeline
    assert "INSERT OR IGNORE INTO score_recompute_queue" not in pipeline


def test_v090_public_evidence_uses_persisted_source_ids_without_scores():
    evidence = (ROOT / "src/public_evidence.py").read_text()
    main = (ROOT / "src/main.py").read_text()
    assert "score_component_evidence" in evidence
    assert 'insider.get("source_id")' in evidence
    assert 'ownership.get("source_id")' in evidence
    assert 'institutional.get("source_id")' in evidence
    assert 'event.get("source_id")' in evidence
    company_block = main[main.index('@app.get("/api/company/{identifier}")'):main.index('@app.get("/api/admin/8k-health")')]
    assert '"component_provenance"' not in company_block


def test_v090_discovery_health_tracks_independent_sources():
    main = (ROOT / "src/main.py").read_text()
    migration = (ROOT / "migrations/0028_integrity_observability.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS discovery_health" in migration
    for source in ('"form4"', '"ownership"', '"8k"', '"13f"'):
        assert source in main
    assert '"event": "discovery_error"' in main


def test_v090_sec_user_agent_requires_real_contact():
    for value in (None, "", "HiddenConviction/0.11.0 replace-with-your-email@example.com"):
        try:
            validate_sec_user_agent(value)
        except RuntimeError:
            pass
        else:
            raise AssertionError("placeholder SEC identity must fail")
    assert validate_sec_user_agent("HiddenConviction/0.11.0 ops@mydomain.test") == "HiddenConviction/0.11.0 ops@mydomain.test"


def test_v090_security_headers_and_cache_policy_exist():
    main = (ROOT / "src/main.py").read_text()
    for token in (
        "Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy",
        "Permissions-Policy", "stale-while-revalidate", '"no-store"',
    ):
        assert token in main


def test_v090_research_ui_adds_search_history_and_pipeline_status():
    html = (ROOT / "public/index.html").read_text()
    js = (ROOT / "public/app.js").read_text()
    css = (ROOT / "public/style.css").read_text()
    assert 'id="company-search"' in html
    assert 'id="pipeline-status"' in html
    assert "/api/search" in js
    assert "score_history" in js
    assert ".history-grid" in css


def test_v090_full_migration_smoke_test():
    db = sqlite3.connect(":memory:")
    for migration in sorted((ROOT / "migrations").glob("*.sql")):
        db.executescript(migration.read_text())
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "discovery_health" in tables
    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_scores_date_hcs" in indexes
