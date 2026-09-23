from pathlib import Path
import sqlite3

from src.security_master import (
    HCS_VERSION,
    INSTITUTIONAL_VERSION,
    SECURITY_MASTER_VERSION,
    make_candidate,
    normalize_security_name,
    resolve_candidates,
    temporal_compatibility_sql,
)
from src.universe import UNIVERSE_VERSION, apply_universe_override, classify_issuer

ROOT = Path(__file__).resolve().parents[1]


def test_v081_security_name_normalization_handles_13f_noise():
    assert normalize_security_name("CLOROX CO DEL") == "CLOROX"
    assert normalize_security_name("Allison Transmission Holdings, Inc.") == "ALLISON TRANSMISSION HLDGS"


def test_v081_security_master_priority_prefers_exact_cusip_when_all_agree():
    candidates = [
        make_candidate(cik="0000012345", ticker="ABC", company_name="ABC Inc", method="normalized_name"),
        make_candidate(cik="12345", ticker="ABC", company_name="ABC Inc", method="security_master_cusip"),
    ]
    resolution = resolve_candidates([c for c in candidates if c])
    assert resolution.status == "resolved"
    assert resolution.issuer_cik == "12345"
    assert resolution.method == "security_master_cusip"
    assert resolution.confidence == 1.0


def test_v081_conflicting_identity_evidence_is_quarantined():
    candidates = [
        make_candidate(cik="123", method="security_master_cusip"),
        make_candidate(cik="456", method="normalized_name"),
    ]
    resolution = resolve_candidates([c for c in candidates if c])
    assert resolution.status == "conflict"
    assert resolution.issuer_cik is None
    assert resolution.confidence == 0


def test_v081_universe_override_is_explicit_and_auditable():
    base = classify_issuer("Example Operating Company Inc")
    result = apply_universe_override(
        base,
        {"force_eligible": 0, "force_issuer_type": "manual_exclusion", "reason": "Reviewed manually."},
    )
    assert not result.hcs_eligible
    assert result.issuer_type == "manual_exclusion"
    assert result.rule == "explicit_override"
    assert result.reason == "Reviewed manually."


def test_v081_versions_are_consistent():
    assert HCS_VERSION == "hcs-v0.8.1"
    assert INSTITUTIONAL_VERSION == "institutional-v0.8.1"
    assert SECURITY_MASTER_VERSION == "security-master-v0.8.1"
    assert UNIVERSE_VERSION == "universe-v0.8.1"
    assert 'VERSION = APP_VERSION' in (ROOT / "src/main.py").read_text()
    assert 'name = "hidden-conviction"' in (ROOT / "pyproject.toml").read_text()


def test_v081_temporal_compatibility_is_bounded_around_13f_period_and_filing():
    predicate = temporal_compatibility_sql()
    assert "-120 day" in predicate
    assert "+45 day" in predicate
    recompute = (ROOT / "src/recompute.py").read_text()
    assert "DATE(?) BETWEEN DATE(period_of_report, '-120 day') AND DATE(filing_date, '+45 day')" in recompute


def test_v081_duplicate_manager_protection_and_cross_manager_metrics():
    recompute = (ROOT / "src/recompute.py").read_text()
    assert "PARTITION BY COALESCE(manager_cik, manager_name)" in recompute
    assert "confirming_manager_count" in recompute
    assert "strongest_manager_score" in recompute
    assert "aggregate_manager_signal" in recompute
    assert "Research-only diagnostic. HCS still uses the strongest manager only." in recompute


def test_v081_component_provenance_is_versioned():
    recompute = (ROOT / "src/recompute.py").read_text()
    migration = (ROOT / "migrations/0026_institutional_security_master.sql").read_text()
    assert "score_component_evidence" in recompute
    assert "component_age_days" in migration
    for component in ("insider", "cluster", "ownership", "institutional", "event", "capital_allocation", "convergence", "penalty"):
        assert f'"component": "{component}"' in recompute


def test_v081_migration_preserves_historical_scores():
    migration = (ROOT / "migrations/0026_institutional_security_master.sql").read_text().upper()
    assert "DELETE FROM SCORES" not in migration
    assert "UPDATE SCORES\nSET" not in migration
    assert "SCORE_RECOMPUTE_QUEUE" in migration
    assert "TODAY" in migration or "TODAY'S" in migration


def test_v081_security_master_schema_and_mapping_provenance():
    migration = (ROOT / "migrations/0026_institutional_security_master.sql").read_text()
    for token in (
        "CREATE TABLE IF NOT EXISTS security_master",
        "CREATE TABLE IF NOT EXISTS security_aliases",
        "issuer_mapping_method",
        "issuer_mapping_confidence",
        "issuer_mapping_status",
        "issuer_mapping_version",
        "issuer_mapping_updated_at",
    ):
        assert token in migration


def test_v081_unresolved_backfill_is_bounded_and_terminal():
    migration = (ROOT / "migrations/0026_institutional_security_master.sql").read_text()
    pipeline = (ROOT / "src/pipeline_13f.py").read_text()
    assert "security_resolution_queue" in migration
    assert "resolve_unmapped_security_positions" in pipeline
    assert "LIMIT ?" in pipeline
    assert "'unresolved'" in pipeline
    assert "'conflict'" in pipeline


def test_v081_security_master_learns_and_retroactively_resolves_exact_cusip():
    pipeline = (ROOT / "src/pipeline_13f.py").read_text()
    assert "source_count=security_master.source_count+1" in pipeline
    assert "WHERE UPPER(cusip)=UPPER(?)" in pipeline
    assert "security-master learned CUSIP mapping" in pipeline


def test_v081_public_rankings_hard_filter_database_eligibility():
    main = (ROOT / "src/main.py").read_text()
    assert main.count("c.hcs_eligible = 1") >= 5
    assert "WHERE c.hcs_eligible = 1" in main
    assert "AND c.hcs_eligible = 1" in main


def test_v081_latest_score_semantics_are_standardized():
    main = (ROOT / "src/main.py").read_text()
    migration = (ROOT / "migrations/0026_institutional_security_master.sql").read_text()
    assert "ROW_NUMBER() OVER" in main
    assert "ORDER BY s2.score_date DESC, s2.id DESC LIMIT 1" in main
    assert "CREATE VIEW current_scores" in migration
    assert "FROM current_scores" in main


def test_v081_13f_health_and_explain_endpoints_exist():
    main = (ROOT / "src/main.py").read_text()
    assert '@app.get("/api/admin/13f-health")' in main
    assert '@app.get("/api/admin/explain/{identifier}")' in main
    for invariant in (
        "institutional_only_activation",
        "fund_score_violations",
        "corporate_action_score_violations",
        "predecessor_gate_violations",
        "conflict_mapping_violations",
        "excluded_issuer_whale_violations",
        "ambiguous_mapping_activation",
        "stale_institutional_activation",
        "temporal_mismatch_activation",
    ):
        assert invariant in main


def test_v081_public_company_page_explains_13f_delay_and_freshness():
    main = (ROOT / "src/main.py").read_text()
    evidence = (ROOT / "src/public_evidence.py").read_text()
    app = (ROOT / "public/app.js").read_text()
    assert "institutional_data_through" in main
    assert "institutional_latest_filed_at" in main
    assert "13F data is delayed and is used only as confirmation of fresher signals." in evidence
    assert "13F holdings are delayed and are used only as confirmation of fresher signals." in app


def test_v081_full_migration_smoke_test():
    db = sqlite3.connect(":memory:")
    for migration in sorted((ROOT / "migrations").glob("*.sql")):
        db.executescript(migration.read_text())
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "security_master" in tables
    assert "security_aliases" in tables
    assert "issuer_universe_overrides" in tables
    assert "score_component_evidence" in tables
    assert "score_recompute_queue" in tables
    assert "security_resolution_queue" in tables
    views = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert "current_scores" in views
    score_cols = {row[1] for row in db.execute("PRAGMA table_info(scores)")}
    assert {"hcs_version", "institutional_version", "universe_version", "confirming_manager_count"} <= score_cols
    mapping_cols = {row[1] for row in db.execute("PRAGMA table_info(institutional_positions)")}
    assert {"issuer_mapping_method", "issuer_mapping_confidence", "issuer_mapping_status", "issuer_mapping_version"} <= mapping_cols
