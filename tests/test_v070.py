from pathlib import Path

from src.public_api import company_lookup_mode, public_signal_families


def test_v070_company_lookup_supports_ticker_and_cik():
    assert company_lookup_mode('rwt') == ('ticker', 'RWT')
    assert company_lookup_mode('0002054992') == ('cik', '2054992')


def test_v070_public_signal_families_hide_component_weights():
    signals = public_signal_families({
        'insider_score': 25,
        'cluster_score': 5,
        'ownership_score': 15,
        'whale_score': 0,
        'event_score': 9,
        'capital_allocation_score': 0,
        'convergence_bonus': 8,
        'penalty': 0,
    })
    keys = {row['key'] for row in signals}
    assert keys == {'insider', 'cluster', 'ownership', 'event', 'convergence'}
    assert all('score' not in row for row in signals)


def test_v070_negative_event_is_explained_as_risk_event():
    signals = public_signal_families({'event_score': -7})
    assert signals == [{
        'key': 'event',
        'label': 'Corporate Risk Event',
        'description': 'A recent 8-K event is reducing the current HCS.',
        'direction': 'negative',
    }]


def test_v070_frontend_contains_phase_one_views_and_source_links():
    root = Path(__file__).parents[1]
    html = (root / 'public' / 'company.html').read_text()
    js = (root / 'public' / 'app.js').read_text()
    assert 'view-company' in html
    assert 'view-methodology' in (root / 'public' / 'methodology.html').read_text()
    assert 'Why this score?' in js
    assert 'View SEC filing' in js
    assert '/api/freshness' in js


def test_v070_backend_supports_spa_routes_and_latest_public_signals():
    root = Path(__file__).parents[1]
    source = (root / 'src' / 'main.py').read_text()
    edge = (root / 'src' / 'public_worker.js').read_text()
    assert '@app.get("/api/freshness")' in source
    assert '@app.get("/api/company/{identifier}")' in source
    assert "path.startsWith('/company/')" in edge and "path === '/system'" in edge
    assert 'PARTITION BY s.cik' in source
