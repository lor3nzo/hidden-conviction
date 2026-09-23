from pathlib import Path
import json
import sqlite3

from src.app_meta import APP_VERSION, API_SCHEMA_VERSION
from src.sec.discovery import daily_index_directory_url, parse_daily_index_directory

ROOT = Path(__file__).resolve().parents[1]


def _all_migrations(db: sqlite3.Connection) -> None:
    for migration in sorted((ROOT / "migrations").glob("*.sql")):
        db.executescript(migration.read_text())


def test_v011_version_and_schema_contract():
    # v0.11 established centralized version/schema metadata. Later releases may advance it.
    assert tuple(map(int, APP_VERSION.split("."))) >= (0, 11, 0)
    assert API_SCHEMA_VERSION.startswith("2026-09-22.v")
    assert f'version = "{APP_VERSION}"' in (ROOT / "pyproject.toml").read_text()


def test_v011_sec_directory_metadata_is_authority_for_daily_indexes():
    sample = json.dumps({
        "directory": {
            "item": [
                {"name": "master.20260917.idx", "last-modified": "2026-09-17 22:00:00", "size": 100},
                {"name": "company.20260917.idx", "size": 90},
                {"name": "master.20260918.idx", "last-modified": "2026-09-18 22:00:00", "size": 110},
                {"name": "master.bad.idx", "size": 1},
            ]
        }
    })
    rows = parse_daily_index_directory(sample)
    assert [row["date"] for row in rows] == ["2026-09-17", "2026-09-18"]
    assert rows[-1]["name"] == "master.20260918.idx"
    assert daily_index_directory_url("2026-09-22").endswith("/2026/QTR3/index.json")


def test_v011_discovery_reconciles_published_indexes_and_balances_receipts():
    main = (ROOT / "src/main.py").read_text()
    assert "discover_available_daily_indexes" in main
    assert "published_indexes" in main
    assert "unreconciled[0]" in main
    assert "timedelta(days=1)" in main
    assert "Never guess a same-day" in main
    assert 'reconciliation_status = "success" if balanced else "error"' in main
    assert "discovered_count == processed_count + pending_count + failed_count" in main
    assert "last_reconciled_date" in main


def test_v011_cron_receipts_retry_and_dead_letter_behavior_exist():
    main = (ROOT / "src/main.py").read_text()
    migration = (ROOT / "migrations/0030_v011_reliability_console.sql").read_text()
    assert "cron_runs" in main and "cron_runs" in migration
    assert "next_retry_at" in main and "dead_letter_at" in main
    assert "delaySeconds" in main
    assert "attempts >= 4" in main or "attempts >= 3" in main
    assert "run_id" in main and "duration_ms" in main


def test_v011_admin_replay_and_deterministic_score_replay_exist():
    main = (ROOT / "src/main.py").read_text()
    assert '@app.post("/api/admin/replay/filing/{queue_id}")' in main
    assert '@app.post("/api/admin/replay/score/{cik}")' in main
    assert '@app.get("/api/admin/replay-score/{identifier}")' in main
    assert "replay_receipts" in main
    assert "evidence_fingerprint" in main
    assert "reason_codes_json" in main
    assert "scoring_engine_version" in main
    assert "score_delta" in main
    assert '"matches_stored_score"' in main


def test_v011_system_console_request_tracing_and_conditional_cache_exist():
    main = (ROOT / "src/main.py").read_text()
    html = (ROOT / "public/system.html").read_text()
    assert '@app.get("/api/system")' in main
    assert 'id="view-system"' in html
    assert "X-Request-ID" in main
    assert "Server-Timing" in main
    assert "If-None-Match" in main or "if-none-match" in main
    assert 'canonical["meta"].pop("generated_at", None)' in main
    assert "MODULE_IMPORT_DURATION_MS" in main


def test_v011_server_rendered_home_and_head_support():
    edge = (ROOT / "src/public_worker.js").read_text()
    html = (ROOT / "public/index.html").read_text()
    assert "renderHome" in edge
    assert "SSR_SIGNALS" in html and "SSR_CANDIDATES" in html
    assert "request.method === 'HEAD'" in edge
    assert "path === '/system'" in edge


def test_v011_frontend_shareable_filters_svg_history_accessibility_and_console():
    js = (ROOT / "public/app.js").read_text()
    html = (ROOT / "public/system.html").read_text()
    for token in (
        "syncFiltersToUrl", "applyFiltersFromUrl", "popstate", "aria-activedescendant",
        "ArrowDown", "ArrowUp", "renderHistorySvg", "loadSystemConsole", "setupAdminDiagnostics",
    ):
        assert token in js
    assert "Why did the score change?" in js
    assert "Operator diagnostics" in html
    assert "Pipeline freshness" in (ROOT / "public/index.html").read_text()


def test_v011_full_migration_and_seeded_database_integration():
    db = sqlite3.connect(":memory:")
    _all_migrations(db)
    db.executescript((ROOT / "tests/fixtures/v011_seed.sql").read_text())

    filing_cols = {row[1] for row in db.execute("PRAGMA table_info(filings)")}
    score_cols = {row[1] for row in db.execute("PRAGMA table_info(scores)")}
    queue_cols = {row[1] for row in db.execute("PRAGMA table_info(processing_queue)")}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert {"is_amendment", "amends_accession"}.issubset(filing_cols)
    assert {"scoring_engine_version", "evidence_fingerprint", "reason_codes_json", "score_delta"}.issubset(score_cols)
    assert {"next_retry_at", "dead_letter_at", "last_attempt_at"}.issubset(queue_cols)
    assert {"cron_runs", "reconciliation_runs", "replay_receipts"}.issubset(tables)

    score = db.execute("SELECT hcs_score, score_delta, evidence_fingerprint FROM scores WHERE cik='0000000001'").fetchone()
    assert score == (42.0, 7.0, "abc123")
    reconciliation = db.execute("SELECT discovered_count, processed_count, pending_count, failed_count FROM reconciliation_runs").fetchone()
    assert reconciliation[0] == sum(reconciliation[1:])


def test_v011_engineering_tooling_and_git_ci_are_packaged():
    assert (ROOT / ".github/workflows/ci.yml").exists()
    assert (ROOT / "scripts/bootstrap-git.sh").exists()
    assert (ROOT / "scripts/tag-release.sh").exists()
    assert (ROOT / "pyrightconfig.json").exists()
    assert (ROOT / "package.json").exists()
