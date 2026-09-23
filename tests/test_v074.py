from pathlib import Path

from src.sec.eight_k import parse_8k_submission

ROOT = Path(__file__).resolve().parents[1]


def test_item_401_auditor_change_does_not_inherit_generic_distress_words():
    event = parse_8k_submission("""
    Item 4.01 Changes in Registrant's Certifying Accountant.
    The auditor resigned. The engagement letter contains boilerplate remedies
    concerning default and bankruptcy of counterparties.
    Item 9.01 Financial Statements and Exhibits.
    """)
    assert event.event_score == -2
    assert event.sentiment == "negative"
    assert "auditor resignation" in event.matched_keywords
    assert "default" not in event.matched_keywords
    assert "bankruptcy" not in event.matched_keywords


def test_item_502_is_neutral_without_explicit_appointment_or_departure():
    event = parse_8k_submission("""
    Item 5.02 Departure of Directors or Certain Officers; Election of Directors.
    The compensation plan contains standard provisions addressing default and bankruptcy.
    Item 5.07 Submission of Matters to a Vote of Security Holders.
    """)
    assert event.event_score == 0
    assert event.sentiment == "neutral"
    assert event.matched_keywords == []


def test_item_507_vote_is_neutral_even_with_unrelated_negative_terms():
    event = parse_8k_submission("""
    Item 5.07 Submission of Matters to a Vote of Security Holders.
    Stockholders approved the proposal. Background materials referenced prior
    restatement, default, and bankruptcy definitions.
    Item 9.01 Financial Statements and Exhibits.
    """)
    assert event.event_score == 0
    assert event.sentiment == "neutral"


def test_item_301_minimum_bid_deficiency_is_negative_but_not_max_penalty():
    event = parse_8k_submission("""
    Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard.
    Nasdaq notified the Company that it is not in compliance with the minimum bid price
    requirement for continued listing. The Company has a cure period.
    Item 9.01 Financial Statements and Exhibits.
    """)
    assert event.event_score == -8
    assert "listing noncompliance" in event.matched_keywords


def test_item_301_voluntary_exchange_transfer_is_neutral():
    event = parse_8k_submission("""
    Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; Transfer of Listing.
    The Company notified the NYSE of its intention to voluntarily withdraw its primary
    listing and transfer the primary listing to the Texas Stock Exchange (TXSE).
    Item 7.01 Regulation FD Disclosure.
    Item 9.01 Financial Statements and Exhibits.
    """)
    assert event.event_score == 0
    assert event.sentiment == "neutral"
    assert "voluntary exchange transfer" in event.matched_keywords


def test_item_301_actual_delisting_is_stronger_negative():
    event = parse_8k_submission("""
    Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard.
    The Company received a Staff Determination Letter and trading will be suspended.
    The securities will be delisted from Nasdaq.
    """)
    assert event.event_score == -12
    assert "actual delisting or suspension" in event.matched_keywords


def test_item_402_nonreliance_is_hard_negative():
    event = parse_8k_submission("""
    Item 4.02 Non-Reliance on Previously Issued Financial Statements or a Related Audit Report.
    The audit committee concluded that previously issued financial statements should no
    longer be relied upon and the Company will restate those financial statements because
    of a material weakness.
    Item 9.01 Financial Statements and Exhibits.
    """)
    assert event.event_score == -15
    assert "restatement" in event.matched_keywords
    assert "material weakness" in event.matched_keywords


def test_item_202_results_are_not_automatically_positive():
    event = parse_8k_submission("""
    Item 2.02 Results of Operations and Financial Condition.
    The Company reported quarterly financial results.
    Item 9.01 Financial Statements and Exhibits.
    """)
    assert event.event_score == 0


def test_item_202_local_guidance_can_move_score():
    event = parse_8k_submission("""
    Item 2.02 Results of Operations and Financial Condition.
    The Company reported record revenue and raised guidance for the fiscal year.
    Item 9.01 Financial Statements and Exhibits.
    """)
    assert event.event_score == 9
    assert set(event.matched_keywords) == {"record revenue", "raised guidance"}


def test_item_901_never_scores_and_excerpt_is_clean_text():
    event = parse_8k_submission("""
    <html><body>
    <h2>Item 8.01 Other Events.</h2>
    <p>The company announced an operational update.</p>
    <h2>Item 9.01 Financial Statements and Exhibits.</h2>
    <p>Exhibit references bankruptcy and default.</p>
    </body></html>
    """)
    assert event.event_score == 0
    assert event.matched_keywords == []
    assert event.excerpt is not None
    assert "<" not in event.excerpt


def test_v074_rebuild_migration_requeues_all_8ks_and_neutralizes_old_event_scores():
    text = (ROOT / "migrations" / "0021_8k_semantic_hardening.sql").read_text()
    assert "form_type LIKE '8-K%'" in text
    assert "SET event_score = 0" in text
    assert "status = 'pending'" in text
    assert "enqueued_at = NULL" in text
