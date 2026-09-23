from pathlib import Path

from src.sec.eight_k import parse_8k_submission, submission_recent_row

ROOT = Path(__file__).resolve().parents[1]


def test_submission_recent_row_resolves_primary_document():
    payload = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000000000-26-000001", "0001493152-26-043593"],
                "primaryDocument": ["other.htm", "form8-k.htm"],
                "filingDate": ["2026-09-20", "2026-09-21"],
                "form": ["8-K", "8-K"],
            }
        }
    }
    row = submission_recent_row(payload, "0001493152-26-043593")
    assert row["primary_document"] == "form8-k.htm"
    assert row["filing_date"] == "2026-09-21"


def test_primary_only_8k_uses_fallback_metadata():
    html = """
    <html><body>
    <h2>Item 8.01 Other Events.</h2>
    <p>The company announced an operational update.</p>
    </body></html>
    """
    event = parse_8k_submission(
        html,
        fallback_cik="1718405",
        fallback_company_name="HYCROFT MINING HOLDING CORPORATION",
        fallback_event_date="2026-09-21",
    )
    assert event.cik == "1718405"
    assert event.company_name == "HYCROFT MINING HOLDING CORPORATION"
    assert event.event_date == "2026-09-21"


def test_general_8k_no_longer_fetches_full_submission_directly():
    text = (ROOT / "src" / "main.py").read_text()
    start = text.index("async def process_8k_message")
    end = text.index("\ndef _is_13f_form", start)
    block = text[start:end]
    assert "_fetch_primary_8k_document" in block
    assert 'sec_fetch_text(body["filing_url"]' not in block


def test_hycroft_retry_migration_is_guarded():
    text = (ROOT / "migrations" / "0020_8k_primary_document_fetch.sql").read_text()
    assert "0001493152-26-043593" in text
    assert "NOT EXISTS" in text
    assert "eight_k_events" in text
