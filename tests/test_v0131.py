from pathlib import Path

from src.app_meta import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]

def test_v0131_version():
    assert APP_VERSION in {"0.13.1", "0.14.0"}

def test_v0131_polish_is_packaged():
    edge=(ROOT/"src/public_worker.js").read_text()
    app=(ROOT/"public/app.js").read_text()
    home=(ROOT/"public/index.html").read_text()
    system=(ROOT/"public/system.html").read_text()
    for page in ("index.html","company.html","system.html","methodology.html"):
        text=(ROOT/"public"/page).read_text()
        assert '/favicon.svg' in text
    assert 'SSR_KPIS' in home
    assert 'data-delay-badge' in home
    assert 'href="/system" id="system-status"' in home
    assert 'system-refresh' in system and 'system-webchecks' in system
    assert 'SEC filing coverage through ${esc(displayDate' in edge
    assert 'HCS means Hidden Conviction Score' in edge
    assert 'build-card' in edge
    assert 'data-copy-url' in edge and 'data-copy-url' in app
    assert 'relativeTime' in app
    assert 'filing backlog' in app
    assert 'loadWebChecks' in app

def test_v0131_no_database_migration_required():
    migrations=sorted(p.name for p in (ROOT/'migrations').glob('*.sql'))
    assert any(name.startswith('0032_') for name in migrations)
