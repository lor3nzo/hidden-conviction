from pathlib import Path

from src.sec.eight_k import PARSER_VERSION, SCORING_VERSION, parse_8k_submission

ROOT = Path(__file__).resolve().parents[1]


def test_la_rosa_style_negations_and_historical_resignation_score_only_material_weakness():
    event = parse_8k_submission("""
    Item 4.01 Changes in Registrant's Certifying Accountant.
    On September 16, 2026, the Audit Committee dismissed CBIZ CPAs P.C. and
    appointed RRBB as the Company's independent registered public accounting firm.
    As previously disclosed in a Current Report on Form 8-K filed on April 30, 2025,
    Marcum LLP resigned, and CBIZ CPAs was appointed effective April 29, 2025.
    From April 29, 2025 through the date of CBIZ CPAs' dismissal, there were
    (i) no disagreements with CBIZ CPAs on any matters of accounting principles or
    practices and (ii) no reportable events, except for the following material
    weaknesses in the Company's internal control over financial reporting.
    """)
    assert event.event_score == -3
    assert event.event_type == "Auditor change with material weakness"
    assert event.scoring_rule == "4.01.material_weakness"
    assert event.matched_keywords == ["material weakness"]


def test_historical_resignation_alone_does_not_score_current_401():
    event = parse_8k_submission("""
    Item 4.01 Changes in Registrant's Certifying Accountant.
    The Audit Committee dismissed Old CPA and appointed New CPA on September 20, 2026.
    As previously disclosed in a Current Report on Form 8-K filed in 2025, Prior CPA
    resigned from the engagement. There were no disagreements and no reportable events.
    """)
    assert event.event_score == 0
    assert event.scoring_rule == "4.01.routine_change"
    assert "auditor resignation" not in event.matched_keywords


def test_current_resignation_still_scores_after_historical_reference():
    event = parse_8k_submission("""
    Item 4.01 Changes in Registrant's Certifying Accountant.
    As previously disclosed in 2025, Prior CPA resigned from the engagement.
    On September 21, 2026, the Company's independent registered public accounting
    firm notified the Audit Committee of its resignation, effective immediately.
    There were no disagreements and no reportable events.
    """)
    assert event.event_score == -2
    assert event.scoring_rule == "4.01.auditor_resignation"
    assert event.matched_keywords == ["auditor resignation"]


def test_affirmative_current_disagreement_remains_stronger_negative():
    event = parse_8k_submission("""
    Item 4.01 Changes in Registrant's Certifying Accountant.
    The former auditor reported a disagreement with management regarding revenue
    recognition. The Company also identified a material weakness in internal control.
    """)
    assert event.event_score == -7
    assert event.scoring_rule == "4.01.accounting_concern"
    assert "auditor disagreement" in event.matched_keywords
    assert "material weakness" in event.matched_keywords


def test_negated_legal_terms_do_not_score_as_accounting_concerns():
    event = parse_8k_submission("""
    Item 4.01 Changes in Registrant's Certifying Accountant.
    The Company dismissed Old CPA and appointed New CPA. There were no disagreements
    with the former accountant and without any reportable events during the period.
    """)
    assert event.event_score == 0
    assert event.scoring_rule == "4.01.routine_change"
    assert event.matched_keywords == []


def test_v077_versions_and_selective_401_migration():
    assert PARSER_VERSION == "8k-parser-v0.7.6"
    assert SCORING_VERSION == "8k-score-v0.7.7"
    migration = (ROOT / "migrations" / "0024_8k_401_semantic_context.sql").read_text()
    assert "item_numbers LIKE '%\"4.01\"%'" in migration
    assert "8k-score-v0.7.7" in migration
    assert "status = 'pending'" in migration
    assert "form_type LIKE '8-K%'" not in migration


def test_main_and_package_versions_are_077():
    main = (ROOT / "src" / "main.py").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'VERSION = APP_VERSION' in main
    assert 'name = "hidden-conviction"' in pyproject
