from pathlib import Path
import sqlite3

from src.app_meta import APP_VERSION, API_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _all_migrations(db):
    for migration in sorted((ROOT / 'migrations').glob('*.sql')):
        db.executescript(migration.read_text())


def test_v013_version_and_worker_split():
    assert APP_VERSION in {'0.13.0', '0.13.1', '0.14.0'}
    assert API_SCHEMA_VERSION in {'2026-09-22.v4', '2026-09-22.v5'}
    public = (ROOT / 'wrangler.jsonc').read_text()
    ingest = (ROOT / 'wrangler.ingest.jsonc').read_text()
    assert 'src/public_worker.js' in public
    assert 'hidden-conviction-ingest' in public
    assert '"services"' in public
    assert 'src/main.py' in ingest
    assert '"crons"' in ingest and 'hidden-conviction-filings' in ingest


def test_v013_pages_are_physically_separate_and_not_monolithic():
    home = (ROOT / 'public/index.html').read_text()
    assert 'id="view-home"' in home
    assert 'id="view-company"' not in home
    assert 'id="view-system"' not in home
    assert 'id="view-methodology"' not in home
    assert 'id="view-company"' in (ROOT / 'public/company.html').read_text()
    assert 'id="view-system"' in (ROOT / 'public/system.html').read_text()
    assert 'id="view-methodology"' in (ROOT / 'public/methodology.html').read_text()


def test_v013_disclaimer_is_identical_on_every_page():
    phrase = 'The information provided on this website is for informational and educational purposes only'
    loss = 'Investing involves risk, including the possible loss of principal.'
    for name in ('index.html', 'company.html', 'system.html', 'methodology.html'):
        text = (ROOT / 'public' / name).read_text()
        assert phrase in text
        assert loss in text
        assert 'class="site-disclaimer"' in text


def test_v013_public_worker_has_direct_home_d1_and_backend_service_proxy():
    edge = (ROOT / 'src/public_worker.js').read_text()
    assert 'env.DB.prepare(signalSql)' in edge
    assert 'env.BACKEND.fetch' in edge
    assert "X-Edge-Architecture', 'public-js-v1'" in edge
    assert "X-Deployment-ID" in edge
    assert 'recordQueryMetric' in edge


def test_v013_query_observability_and_indexes_migrate():
    db = sqlite3.connect(':memory:')
    _all_migrations(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert 'query_metrics' in tables
    assert 'idx_scores_cik_date_id' in indexes
    assert 'idx_processing_status_created' in indexes
    assert db.execute("SELECT value FROM runtime_status WHERE key='schema_version'").fetchone() in {('0032',), ('0033',)}


def test_v013_system_console_history_query_plans_and_replay_remain_available():
    main = (ROOT / 'src/main.py').read_text()
    edge = (ROOT / 'src/public_worker.js').read_text()
    assert 'recent_cron_runs' in main
    assert 'recent_error_summary' in main
    assert 'query_metrics_today' in main
    assert '@app.get("/api/admin/query-plans")' in main
    assert '@app.post("/api/admin/replay/filing/{queue_id}")' in main
    assert 'pipeline-flow' in edge and 'Recent cron runs' in edge and 'Recent errors' in edge


def test_v013_deploy_is_backend_first_secret_safe_and_public_cutover_last():
    deploy = (ROOT / 'scripts/deploy.sh').read_text()
    assert deploy.index('wrangler.ingest.jsonc') < deploy.index('ensure-ingest-secrets.sh')
    assert deploy.index('ensure-ingest-secrets.sh') < deploy.index('wrangler.13f.jsonc')
    assert deploy.index('wrangler.13f.jsonc') < deploy.index('npx wrangler deploy --config wrangler.jsonc')
    assert 'Ingestion backend healthy' in deploy
    assert (ROOT / 'scripts/ensure-ingest-secrets.sh').exists()


def test_v013_verifier_checks_raw_ssr_architecture_disclaimer_and_build_consistency():
    verify = (ROOT / 'scripts/verify-production.sh').read_text()
    for token in ('public-js-v1', 'view-company', 'pipeline-flow', 'data-page="company"', 'recent_cron_runs', 'query_metrics_today'):
        assert token in verify
    assert 'informational and educational purposes only' in verify


def test_v013_migration_is_transactionally_rollback_safe():
    db = sqlite3.connect(':memory:')
    for migration in sorted((ROOT / 'migrations').glob('*.sql')):
        if migration.name.startswith('0032_'):
            break
        db.executescript(migration.read_text())
    assert db.execute("SELECT value FROM runtime_status WHERE key='schema_version'").fetchone() == ('0031',)
    migration = (ROOT / 'migrations/0032_v013_architecture_performance.sql').read_text()
    db.executescript('BEGIN;\n' + migration + '\nROLLBACK;')
    assert db.execute("SELECT value FROM runtime_status WHERE key='schema_version'").fetchone() == ('0031',)
    assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='query_metrics'").fetchone() == (0,)


def test_v013_query_plan_uses_new_company_score_index():
    db = sqlite3.connect(':memory:')
    _all_migrations(db)
    plan = list(db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM scores WHERE cik=? ORDER BY score_date DESC, id DESC LIMIT 1",
        ('0000000000',),
    ))
    detail = ' '.join(str(row[-1]) for row in plan)
    assert 'SEARCH scores USING' in detail and 'INDEX' in detail


def test_v013_tooling_is_pinned_and_coverage_floor_is_enforced():
    pyproject = (ROOT / 'pyproject.toml').read_text()
    package = (ROOT / 'package.json').read_text()
    ci = (ROOT / '.github/workflows/ci.yml').read_text()
    assert 'fastapi==0.141.1' in pyproject
    assert 'workers-py==1.17.4' in pyproject
    assert 'pytest-cov==7.1.0' in pyproject
    assert any(f'fail_under = {n}' in pyproject for n in range(30, 101))
    assert '"eslint": "10.11.0"' in package
    assert '"@playwright/test": "1.63.0"' in package
    assert any(f'--cov-fail-under={n}' in ci for n in range(30, 101))


def test_v013_system_console_has_seven_day_trends_and_query_timing():
    main = (ROOT / 'src/main.py').read_text()
    edge = (ROOT / 'src/public_worker.js').read_text()
    assert 'pipeline_trends_7d' in main
    assert "metric_date>=DATE('now','-6 day')" in main
    assert '7 day pipeline activity' in edge
    assert 'Query performance today' in edge


def test_v013_telemetry_helpers_are_split_from_main_entrypoint():
    main = (ROOT / 'src/main.py').read_text()
    telemetry = (ROOT / 'src/telemetry.py').read_text()
    assert 'from telemetry import (' in main
    assert 'async def record_pipeline_metric' in telemetry
    assert 'async def record_sec_request_metric' in telemetry
    assert 'async def _record_pipeline_metric' not in main


def test_v013_release_notes_and_secret_artifact_hygiene():
    assert (ROOT / 'scripts/release-notes.sh').exists()
    ignore = (ROOT / '.gitignore').read_text()
    assert '.admin-secret-v013' in ignore
    assert '.coverage' in ignore
