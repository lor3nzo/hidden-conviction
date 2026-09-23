from pathlib import Path

from src.scoring.hcs import confirmed_institutional_score
from src.universe import UNIVERSE_VERSION, classify_issuer

ROOT = Path(__file__).resolve().parents[1]


def test_universe_excludes_obvious_funds_seen_in_calibration():
    for name in (
        "Lincoln Bain Capital Total Credit Fund",
        "RIVERNORTH OPPORTUNITIES FUND, INC.",
        "AMG BBH Asset-Backed Credit Fund, LLC",
        "iShares Core S&P 500 ETF",
    ):
        result = classify_issuer(name)
        assert not result.hcs_eligible
        assert result.issuer_type == "fund_or_investment_vehicle"


def test_universe_excludes_obvious_acquisition_vehicle():
    result = classify_issuer("Silicon Valley Acquisition Corp.")
    assert not result.hcs_eligible
    assert result.issuer_type == "acquisition_vehicle"


def test_universe_does_not_broadly_exclude_trust_operating_companies():
    result = classify_issuer("REDWOOD TRUST INC")
    assert result.hcs_eligible
    assert result.issuer_type == "operating_company"


def test_universe_does_not_match_fundamental_as_fund_token():
    result = classify_issuer("Fundamental Global Inc.")
    assert result.hcs_eligible


def test_13f_cannot_create_conviction_by_itself():
    assert confirmed_institutional_score(15) == 0


def test_13f_confirms_fresher_positive_family():
    assert confirmed_institutional_score(12, insider_score=20) == 12
    assert confirmed_institutional_score(8, ownership_score=10) == 8
    assert confirmed_institutional_score(6, event_score=3) == 6


def test_13f_negative_event_alone_is_not_positive_anchor():
    assert confirmed_institutional_score(10, event_score=-6) == 0


def test_13f_excluded_issuer_is_hard_gated_and_candidate_is_capped():
    assert confirmed_institutional_score(15, insider_score=20, hcs_eligible=False) == 0
    assert confirmed_institutional_score(99, insider_score=20) == 15


def test_v080_recompute_paths_use_validated_latest_13f_and_confirmation_gate():
    for filename in ("src/main.py", "src/recompute.py"):
        text = (ROOT / filename).read_text()
        assert "confirmed_institutional_score(" in text
        assert "comparison_status IN ('existing_position', 'new_position')" in text
        assert "COALESCE(corporate_action_suspected, 0) = 0" in text
        assert "period_of_report >= DATE('now', '-220 day')" in text


def test_v080_13f_chunks_recompute_only_bounded_affected_issuers():
    text = (ROOT / "src" / "pipeline_13f.py").read_text()
    assert "affected_ciks: set[str] = set()" in text
    assert "if issuer_cik and not is_historical" in text
    assert "await recompute_today_score(env, issuer_cik)" in text
    assert "CHUNK_SIZE = 5" in text


def test_v080_migration_adds_universe_audit_fields_and_controlled_13f_activation():
    text = (ROOT / "migrations" / "0025_universe_and_controlled_13f.sql").read_text()
    for column in ("issuer_type", "hcs_eligible", "hcs_exclusion_reason", "universe_version"):
        assert f"ADD COLUMN {column}" in text
    assert "fund_or_investment_vehicle" in text
    assert "acquisition_vehicle" in text
    assert "comparison_status IN ('existing_position', 'new_position')" in text
    assert "corporate_action_suspected" in text
    assert "insider_score > 0" in text
    assert "ownership_score > 0" in text
    assert "event_score > 0" in text
    assert "WHERE score_date = DATE('now')" in text


def test_v080_public_ranking_has_runtime_universe_filter():
    text = (ROOT / "src" / "main.py").read_text()
    assert "def _in_public_hcs_universe" in text
    assert "_public_universe_rows(result, limit)" in text
    assert 'VERSION = APP_VERSION' in text
    assert UNIVERSE_VERSION == "universe-v0.8.1"


def test_v080_package_version():
    assert 'name = "hidden-conviction"' in (ROOT / "pyproject.toml").read_text()
