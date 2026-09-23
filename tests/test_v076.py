from pathlib import Path

from src.sec.eight_k import PARSER_VERSION, SCORING_VERSION, parse_8k_submission

ROOT = Path(__file__).resolve().parents[1]


def test_301_heading_transfer_language_cannot_make_hyperfine_style_deficiency_neutral():
    event = parse_8k_submission("""
    Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; Transfer of Listing.
    Nasdaq notified the Company that the closing bid price of its Class A common stock
    has fallen below $1.00 per share for 30 consecutive business days and the Company
    no longer meets the minimum bid price requirement for continued listing.
    Item 9.01 Financial Statements and Exhibits.
    """)
    assert event.event_score == -8
    assert event.event_type == "Listing-rule noncompliance"
    assert event.scoring_rule == "3.01.noncompliance"


def test_301_voluntary_transfer_requires_affirmative_body_language():
    event = parse_8k_submission("""
    Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; Transfer of Listing.
    The Company notified the NYSE that it intends to voluntarily withdraw its primary
    listing and transfer the primary listing to the Texas Stock Exchange.
    """)
    assert event.event_score == 0
    assert event.event_type == "Voluntary exchange transfer"
    assert event.scoring_rule == "3.01.voluntary_transfer"


def test_401_routine_auditor_change_is_neutral():
    event = parse_8k_submission("""
    Item 4.01 Changes in Registrant's Certifying Accountant.
    The Audit Committee dismissed Old CPA and appointed New CPA. During the two most
    recent fiscal years there were no disagreements and no reportable events.
    """)
    # Negated phrases should not be treated as actual accounting concerns. The
    # parser's narrow rule recognizes the routine-change context as neutral.
    assert event.event_score == 0
    assert event.scoring_rule == "4.01.routine_change"


def test_401_auditor_resignation_is_measured_negative():
    event = parse_8k_submission("""
    Item 4.01 Changes in Registrant's Certifying Accountant.
    The Company's independent registered public accounting firm resigned effective today.
    """)
    assert event.event_score == -2
    assert event.event_type == "Auditor resignation"


def test_401_disagreement_is_stronger_negative():
    event = parse_8k_submission("""
    Item 4.01 Changes in Registrant's Certifying Accountant.
    The former auditor reported a disagreement with management regarding revenue
    recognition and identified a material weakness in internal control.
    """)
    assert event.event_score <= -7
    assert "auditor disagreement" in event.matched_keywords
    assert "material weakness" in event.matched_keywords


def test_502_planned_retirement_is_neutral():
    event = parse_8k_submission("""
    Item 5.02 Departure of Directors or Certain Officers; Election of Directors.
    As previously announced, the Chief Financial Officer will retire in accordance
    with his planned retirement effective December 31, 2026.
    """)
    assert event.event_score == 0
    assert event.event_type == "Planned executive retirement"


def test_502_cfo_resignation_is_key_departure():
    event = parse_8k_submission("""
    Item 5.02 Departure of Directors or Certain Officers; Election of Directors.
    The Chief Financial Officer resigned effective immediately.
    """)
    assert event.event_score == -4
    assert event.event_type == "CEO or CFO departure"


def test_502_termination_for_cause_is_stronger_negative():
    event = parse_8k_submission("""
    Item 5.02 Departure of Directors or Certain Officers; Election of Directors.
    The Chief Executive Officer was terminated for cause by the Board.
    """)
    assert event.event_score == -6
    assert event.scoring_rule == "5.02.termination_for_cause"


def test_502_cfo_appointment_is_modest_positive():
    event = parse_8k_submission("""
    Item 5.02 Departure of Directors or Certain Officers; Election of Directors.
    The Board appointed Jane Doe as Chief Financial Officer effective October 1, 2026.
    """)
    assert event.event_score == 2
    assert event.event_type == "CEO or CFO appointment"


def test_201_completed_acquisition_is_positive_but_asset_sale_is_neutral():
    completed = parse_8k_submission("""
    Item 2.01 Completion of Acquisition or Disposition of Assets.
    The Company completed the acquisition of Target Corp on September 20, 2026.
    """)
    sale = parse_8k_submission("""
    Item 2.01 Completion of Acquisition or Disposition of Assets.
    The Company completed a sale of assets to Buyer LLC for cash consideration.
    """)
    assert completed.event_score == 6
    assert completed.scoring_rule == "2.01.completed_transaction"
    assert sale.event_score == 0
    assert sale.scoring_rule == "2.01.disposition"


def test_201_terminated_transaction_is_negative():
    event = parse_8k_submission("""
    Item 2.01 Completion of Acquisition or Disposition of Assets.
    The parties entered into a termination agreement and the merger agreement was terminated.
    """)
    assert event.event_score == -4
    assert event.event_type == "Terminated transaction"


def test_audit_fields_capture_primary_scored_section_and_clean_excerpt():
    event = parse_8k_submission("""
    <html><body>
    <h2>Item 7.01 Regulation FD Disclosure.</h2><p>Routine investor presentation.</p>
    <h2>Item 4.02 Non-Reliance on Previously Issued Financial Statements.</h2>
    <p>The Audit Committee concluded the financial statements should no longer be relied upon
    and the Company will restate them because of a material weakness.</p>
    <h2>Item 9.01 Financial Statements and Exhibits.</h2>
    </body></html>
    """)
    assert event.primary_item == "4.02"
    assert event.scoring_rule == "4.02.nonreliance"
    assert "should not be relied upon" in event.scoring_reason
    assert event.source_section and "Item 4.02" in event.source_section
    assert event.excerpt and "<" not in event.excerpt
    assert event.parser_version == PARSER_VERSION
    assert event.scoring_version == SCORING_VERSION


def test_semantic_safeguards_for_neutral_items_and_901():
    event = parse_8k_submission("""
    Item 5.07 Submission of Matters to a Vote of Security Holders.
    Stockholders approved routine matters.
    Item 7.01 Regulation FD Disclosure.
    Routine presentation update.
    Item 9.01 Financial Statements and Exhibits.
    Exhibit text references bankruptcy, default, impairment, and restatement.
    """)
    assert event.event_score == 0
    assert all(word not in event.matched_keywords for word in ["bankruptcy", "default", "impairment", "restatement"])


def test_v076_migration_adds_audit_fields_and_selective_version_requeue():
    text = (ROOT / "migrations" / "0023_8k_auditability_and_rules.sql").read_text()
    for column in ["primary_item", "scoring_rule", "scoring_reason", "source_section", "parser_version", "scoring_version"]:
        assert f"ADD COLUMN {column}" in text
    assert "COALESCE(e.parser_version, '') <> '8k-parser-v0.7.6'" in text
    assert "COALESCE(e.scoring_version, '') <> '8k-score-v0.7.6'" in text
    assert "status = 'pending'" in text


def test_v076_has_permanent_admin_8k_health_endpoint_and_audit_insert():
    text = (ROOT / "src" / "main.py").read_text()
    assert '@app.get("/api/admin/8k-health")' in text
    assert "parser_version, scoring_version" in text
    assert "scoring_reason" in text
    assert "source_section" in text
    assert 'VERSION = APP_VERSION' in text
