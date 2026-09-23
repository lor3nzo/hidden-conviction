from pathlib import Path

from src.sec.eight_k import parse_8k_submission

ROOT = Path(__file__).resolve().parents[1]


def test_staff_determination_letter_without_operational_delisting_is_only_noncompliance():
    event = parse_8k_submission("""
    Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard.
    The Company received a Staff Determination Letter from Nasdaq stating that the
    closing bid price had been below $1.00 for 30 consecutive business days and the
    Company is not in compliance with the minimum bid price requirement. The notice
    has no effect at this time on the listing or trading of the Company's common stock.
    Item 9.01 Financial Statements and Exhibits.
    """)
    assert event.event_score == -8
    assert event.event_type == "Listing-rule noncompliance"
    assert "listing noncompliance" in event.matched_keywords
    assert "actual delisting or suspension" not in event.matched_keywords


def test_equity_deficiency_notice_without_suspension_is_only_noncompliance():
    event = parse_8k_submission("""
    Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard.
    Nasdaq notified the Company that it is not in compliance with the minimum
    stockholders' equity requirement for continued listing. The Company intends to
    submit a compliance plan and its securities continue to trade on Nasdaq.
    """)
    assert event.event_score == -8
    assert event.event_type == "Listing-rule noncompliance"


def test_explicit_future_delisting_remains_stronger_negative():
    event = parse_8k_submission("""
    Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard.
    Nasdaq notified the Company that trading will be suspended at the opening of
    business on September 25, 2026 and the Company's securities will be delisted.
    """)
    assert event.event_score == -12
    assert event.event_type == "Delisting or trading suspension"
    assert "actual delisting or suspension" in event.matched_keywords


def test_actual_completed_suspension_remains_stronger_negative():
    event = parse_8k_submission("""
    Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard.
    Trading has been suspended and the securities were removed from listing.
    """)
    assert event.event_score == -12
    assert "actual delisting or suspension" in event.matched_keywords


def test_v075_migration_requeues_only_item_301_filings():
    text = (ROOT / "migrations" / "0022_8k_listing_severity_fix.sql").read_text()
    assert "item_numbers LIKE '%\"3.01\"%'" in text
    assert "status = 'pending'" in text
    assert "enqueued_at = NULL" in text
    assert "form_type LIKE '8-K%'" not in text
