from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_retry_exhaustion_does_not_mark_filing_processed():
    text = (ROOT / "src" / "main.py").read_text()
    assert 'UPDATE filings SET processed = 0, enqueued_at = NULL WHERE id = ?' in text
    marker = 'if prior_attempts >= 4:'
    retry_block = text[text.index(marker):text.index('continue', text.index(marker))]
    assert 'processed = 1' not in retry_block


def test_hycroft_requeue_is_guarded_by_missing_event():
    text = (ROOT / "migrations" / "0019_general_queue_terminal_state_fix.sql").read_text()
    assert "0001493152-26-043593" in text
    assert "NOT EXISTS" in text
    assert "eight_k_events" in text
    assert "status = 'pending'" in text
