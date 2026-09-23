from pathlib import Path


def test_v080_main_worker_activates_whale_only_through_confirmation_gate():
    root = Path(__file__).parents[1]
    source = (root / 'src' / 'main.py').read_text()
    block = source.split('async def _recompute_today_score', 1)[1].split('async def process_form4_message', 1)[0]
    assert 'candidate_whale_score = float' in block
    assert 'whale_score = 0.0' in block
    assert 'confirmed_institutional_score(' in block
    assert "comparison_status IN ('existing_position', 'new_position')" in block


def test_v071_cleanup_migration_resets_whale_and_recomputes_scores():
    root = Path(__file__).parents[1]
    migration = (root / 'migrations' / '0018_whale_score_gate_cleanup.sql').read_text()
    assert 'SET whale_score = 0' in migration
    assert 'convergence_bonus' in migration
    assert 'raw_score' in migration
    assert 'hcs_score' in migration
    assert 'classification' in migration
