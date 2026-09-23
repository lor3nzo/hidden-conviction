import hashlib
import json

from scoring.hcs import (
    ScoreBreakdown,
    score_beneficial_ownership,
    score_form4_purchase,
    confirmed_institutional_score,
)
from security_master import HCS_VERSION, INSTITUTIONAL_VERSION, SECURITY_MASTER_VERSION
from universe import UNIVERSE_VERSION


COMPONENTS = (
    "insider", "cluster", "ownership", "institutional", "event",
    "capital_allocation", "convergence", "penalty",
)


def _rows(result):
    return getattr(result, "results", None) or []


def _iso_max(values):
    clean = [str(v) for v in values if v]
    return max(clean) if clean else None


def _score_age_days_sql(event_date: str | None) -> int | None:
    # Stored evidence uses SQL-computed age in production. This helper exists
    # only as a semantic placeholder for pure-Python callers/tests.
    del event_date
    return None


async def _effective_universe(env, cik: str) -> dict:
    try:
        result = await env.DB.prepare(
            """
            SELECT c.company_name, c.ticker, c.exchange, c.issuer_type,
                   c.hcs_eligible, c.hcs_exclusion_reason, c.universe_rule,
                   c.universe_reason, c.universe_version,
                   o.force_eligible, o.force_issuer_type, o.reason AS override_reason
            FROM companies c
            LEFT JOIN issuer_universe_overrides o ON o.cik=c.cik
            WHERE c.cik=? LIMIT 1
            """
        ).bind(cik).run()
        row = (_rows(result) or [{}])[0]
        if row.get("force_eligible") is not None:
            row["hcs_eligible"] = int(row.get("force_eligible") or 0)
            row["issuer_type"] = row.get("force_issuer_type") or row.get("issuer_type")
            row["universe_rule"] = "explicit_override"
            row["universe_reason"] = row.get("override_reason") or "Explicit issuer-universe override."
            row["hcs_exclusion_reason"] = None if row["hcs_eligible"] else row["universe_reason"]
        return row
    except Exception:
        result = await env.DB.prepare(
            "SELECT company_name, ticker, exchange, issuer_type, hcs_eligible, hcs_exclusion_reason, universe_version FROM companies WHERE cik=? LIMIT 1"
        ).bind(cik).run()
        return (_rows(result) or [{}])[0]


async def _institutional_confirmation(env, cik: str, anchor_date: str | None) -> dict:
    empty = {
        "candidate_score": 0.0,
        "confirming_manager_count": 0,
        "strongest_manager_score": 0.0,
        "aggregate_manager_signal": 0.0,
        "period_of_report": None,
        "filing_date": None,
        "manager_name": None,
        "share_change_pct": None,
        "position_weight_pct": None,
        "source_id": None,
        "mapping_method": None,
    }
    if not anchor_date:
        return empty

    # Latest security observation per manager/CUSIP, then strongest row per
    # manager. The second ranking prevents one manager with multiple classes or
    # duplicate security rows from masquerading as cross-manager confirmation.
    new_sql = """
        WITH ranked_security AS (
            SELECT ip.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(ip.manager_cik, ip.manager_name), ip.cusip
                       ORDER BY ip.period_of_report DESC, ip.filing_id DESC
                   ) AS security_rn
            FROM institutional_positions ip
            WHERE ip.issuer_cik = ?
              AND ip.period_of_report >= DATE('now', '-220 day')
              AND COALESCE(ip.issuer_mapping_status, 'resolved') = 'resolved'
        ), valid_security AS (
            SELECT *
            FROM ranked_security
            WHERE security_rn = 1
              AND position_score > 0
              AND comparison_status IN ('existing_position', 'new_position')
              AND COALESCE(corporate_action_suspected, 0) = 0
              AND DATE(?) BETWEEN DATE(period_of_report, '-120 day') AND DATE(filing_date, '+45 day')
        ), manager_ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(manager_cik, manager_name)
                       ORDER BY position_score DESC, value_dollars DESC, filing_id DESC
                   ) AS manager_rn
            FROM valid_security
        )
        SELECT id, filing_id, manager_cik, manager_name, period_of_report, filing_date,
               position_score, value_dollars, share_change_pct, position_weight_pct,
               issuer_mapping_method
        FROM manager_ranked
        WHERE manager_rn=1
        ORDER BY position_score DESC, value_dollars DESC, filing_id DESC
        LIMIT 50
    """
    try:
        result = await env.DB.prepare(new_sql).bind(cik, anchor_date).run()
    except Exception:
        # Deployment-order fallback before migration 0026 adds mapping fields.
        result = await env.DB.prepare(
            """
            WITH ranked_security AS (
                SELECT ip.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(ip.manager_cik, ip.manager_name), ip.cusip
                           ORDER BY ip.period_of_report DESC, ip.filing_id DESC
                       ) AS security_rn
                FROM institutional_positions ip
                WHERE ip.issuer_cik = ?
                  AND ip.period_of_report >= DATE('now', '-220 day')
            ), valid_security AS (
                SELECT * FROM ranked_security
                WHERE security_rn=1
                  AND position_score > 0
                  AND comparison_status IN ('existing_position', 'new_position')
                  AND COALESCE(corporate_action_suspected, 0) = 0
                  AND DATE(?) BETWEEN DATE(period_of_report, '-120 day') AND DATE(filing_date, '+45 day')
            ), manager_ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(manager_cik, manager_name)
                    ORDER BY position_score DESC, value_dollars DESC, filing_id DESC
                ) AS manager_rn
                FROM valid_security
            )
            SELECT id, filing_id, manager_cik, manager_name, period_of_report, filing_date,
                   position_score, value_dollars, share_change_pct, position_weight_pct,
                   'legacy_existing' AS issuer_mapping_method
            FROM manager_ranked WHERE manager_rn=1
            ORDER BY position_score DESC, value_dollars DESC, filing_id DESC
            LIMIT 50
            """
        ).bind(cik, anchor_date).run()

    rows = _rows(result)
    if not rows:
        return empty
    strongest = rows[0]
    scores = [float(r.get("position_score") or 0) for r in rows]
    return {
        "candidate_score": max(scores),
        "confirming_manager_count": len(rows),
        "strongest_manager_score": max(scores),
        # Research-only diagnostic. HCS still uses the strongest manager only.
        "aggregate_manager_signal": round(min(15.0, sum(scores)), 1),
        "period_of_report": strongest.get("period_of_report"),
        "filing_date": strongest.get("filing_date"),
        "manager_name": strongest.get("manager_name"),
        "share_change_pct": strongest.get("share_change_pct"),
        "position_weight_pct": strongest.get("position_weight_pct"),
        "source_id": strongest.get("id"),
        "mapping_method": strongest.get("issuer_mapping_method"),
    }


async def _store_component_evidence(env, cik: str, evidence: list[dict]):
    try:
        for item in evidence:
            await env.DB.prepare(
                """
                INSERT INTO score_component_evidence
                    (cik, score_date, component, component_score, source_type, source_id,
                     event_date, filing_date, component_age_days, detail_json,
                     hcs_version, institutional_version, universe_version, updated_at)
                VALUES (?, DATE('now'), ?, ?, ?, ?, ?, ?,
                        CASE WHEN ? IS NULL THEN NULL ELSE CAST(julianday('now') - julianday(?) AS INTEGER) END,
                        ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(cik, score_date, component) DO UPDATE SET
                    component_score=excluded.component_score,
                    source_type=excluded.source_type,
                    source_id=excluded.source_id,
                    event_date=excluded.event_date,
                    filing_date=excluded.filing_date,
                    component_age_days=excluded.component_age_days,
                    detail_json=excluded.detail_json,
                    hcs_version=excluded.hcs_version,
                    institutional_version=excluded.institutional_version,
                    universe_version=excluded.universe_version,
                    updated_at=CURRENT_TIMESTAMP
                """
            ).bind(
                cik, item["component"], float(item.get("score") or 0),
                item.get("source_type"), item.get("source_id"), item.get("event_date"),
                item.get("filing_date"), item.get("event_date"), item.get("event_date"),
                json.dumps(item.get("detail") or {}, separators=(",", ":")),
                HCS_VERSION, INSTITUTIONAL_VERSION, UNIVERSE_VERSION,
            ).run()
    except Exception:
        # Schema 0025 remains safe during code-first deployment.
        return


async def recompute_today_score(env, cik: str):
    insider_result = await env.DB.prepare(
        """
        SELECT it.id, it.insider_role, it.transaction_value, it.shares,
               it.shares_owned_after, it.transaction_date, it.filing_id, f.filed_at
        FROM insider_transactions it
        LEFT JOIN filings f ON f.id=it.filing_id
        WHERE it.cik = ?
          AND it.is_open_market_purchase = 1
          AND it.transaction_date >= DATE('now', '-30 day')
        """
    ).bind(cik).run()
    insider_rows = _rows(insider_result)
    scored_insiders = []
    for r in insider_rows:
        score = score_form4_purchase(
            r.get("insider_role"), r.get("transaction_value"),
            r.get("shares"), r.get("shares_owned_after")
        )
        scored_insiders.append((float(score), r))
    scored_insiders.sort(key=lambda x: (x[0], x[1].get("transaction_date") or ""), reverse=True)
    insider_score = scored_insiders[0][0] if scored_insiders else 0.0
    insider_rep = scored_insiders[0][1] if scored_insiders else {}

    buyers = await env.DB.prepare(
        """
        SELECT COUNT(DISTINCT it.insider_name) AS buyer_count,
               MAX(it.transaction_date) AS latest_transaction_date,
               MAX(f.filed_at) AS latest_filed_at
        FROM insider_transactions it
        LEFT JOIN filings f ON f.id=it.filing_id
        WHERE it.cik = ?
          AND it.is_open_market_purchase = 1
          AND it.transaction_date >= DATE('now', '-30 day')
        """
    ).bind(cik).run()
    buyer_rows = _rows(buyers)
    buyer_row = buyer_rows[0] if buyer_rows else {}
    count = int(buyer_row.get("buyer_count") or 0)
    cluster = 15 if count >= 5 else 12 if count == 4 else 9 if count == 3 else 5 if count == 2 else 0

    ownership_result = await env.DB.prepare(
        """
        WITH ranked AS (
            SELECT og.id, og.schedule_type, og.group_ownership_pct, og.previous_ownership_pct,
                   og.group_key, og.filing_id, og.event_date, f.filed_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(og.group_key, 'filing:' || og.filing_id)
                       ORDER BY COALESCE(og.event_date, DATE(og.created_at)) DESC, og.filing_id DESC
                   ) AS rn
            FROM ownership_groups og
            LEFT JOIN filings f ON f.id=og.filing_id
            WHERE og.issuer_cik = ?
              AND COALESCE(og.event_date, DATE(og.created_at)) >= DATE('now', '-60 day')
        )
        SELECT * FROM ranked WHERE rn = 1
        """
    ).bind(cik).run()
    ownership_rows = _rows(ownership_result)
    scored_ownership = []
    for r in ownership_rows:
        score = score_beneficial_ownership(
            r.get("schedule_type"), r.get("group_ownership_pct"), r.get("previous_ownership_pct")
        )
        scored_ownership.append((float(score), r))
    scored_ownership.sort(key=lambda x: (x[0], x[1].get("event_date") or ""), reverse=True)
    ownership_score = scored_ownership[0][0] if scored_ownership else 0.0
    ownership_rep = scored_ownership[0][1] if scored_ownership else {}

    event_result = await env.DB.prepare(
        """
        SELECT e.id, e.event_score, e.event_date, e.filing_id, e.event_type,
               e.primary_item, e.scoring_reason, f.filed_at
        FROM eight_k_events e
        LEFT JOIN filings f ON f.id=e.filing_id
        WHERE e.cik = ?
          AND COALESCE(e.event_date, DATE(e.created_at)) >= DATE('now', '-30 day')
        ORDER BY ABS(e.event_score) DESC, COALESCE(e.event_date, DATE(e.created_at)) DESC
        LIMIT 1
        """
    ).bind(cik).run()
    event_rows = _rows(event_result)
    event_rep = event_rows[0] if event_rows else {}
    event_score = float(event_rep.get("event_score") or 0)

    anchor_date = _iso_max([
        insider_rep.get("transaction_date") if insider_score > 0 else None,
        ownership_rep.get("event_date") if ownership_score > 0 else None,
        event_rep.get("event_date") if event_score > 0 else None,
    ])

    institutional = await _institutional_confirmation(env, cik, anchor_date)
    candidate_whale_score = float(institutional.get("candidate_score") or 0)
    whale_score = 0.0

    universe = await _effective_universe(env, cik)
    whale_score = confirmed_institutional_score(
        candidate_whale_score,
        insider_score=insider_score,
        ownership_score=ownership_score,
        event_score=event_score,
        capital_allocation_score=0,
        hcs_eligible=bool(int(universe.get("hcs_eligible") if universe.get("hcs_eligible") is not None else 1)),
    )

    positive_families = sum(
        1 for x in (insider_score, ownership_score, whale_score, max(event_score, 0)) if x > 0
    )
    convergence = min(15.0, max(0, positive_families - 1) * 4.0)

    breakdown = ScoreBreakdown(
        insider_score=insider_score,
        cluster_score=cluster,
        ownership_score=ownership_score,
        whale_score=whale_score,
        event_score=event_score,
        convergence_bonus=convergence,
    )

    # v0.11.0 stores a deterministic receipt for each materialized score.
    provenance_payload = {
        "cik": cik,
        "score_date": "today",
        "hcs_version": HCS_VERSION,
        "institutional_version": INSTITUTIONAL_VERSION,
        "universe_version": UNIVERSE_VERSION,
        "components": {
            "insider": insider_score,
            "cluster": cluster,
            "ownership": ownership_score,
            "institutional": whale_score,
            "event": event_score,
            "capital_allocation": 0,
            "convergence": convergence,
        },
    }
    evidence_fingerprint = hashlib.sha256(
        json.dumps(provenance_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    reason_codes = [
        name for name, value in provenance_payload["components"].items() if float(value or 0) != 0
    ]
    previous_score = 0.0
    try:
        previous_result = await env.DB.prepare(
            "SELECT hcs_score FROM scores WHERE cik=? AND score_date<DATE('now') ORDER BY score_date DESC LIMIT 1"
        ).bind(cik).run()
        previous_rows = _rows(previous_result)
        if previous_rows:
            previous_score = float(previous_rows[0].get("hcs_score") or 0)
    except Exception:
        previous_score = 0.0
    score_delta = round(float(breakdown.hcs_score) - previous_score, 1)

    try:
        await env.DB.prepare(
            """
            INSERT INTO scores
            (cik, score_date, insider_score, cluster_score, ownership_score, whale_score,
             event_score, convergence_bonus, raw_score, hcs_score, classification,
             confirming_manager_count, strongest_manager_score, aggregate_manager_signal,
             hcs_version, institutional_version, universe_version, scored_at,
             scoring_engine_version, evidence_fingerprint, reason_codes_json, score_delta)
            VALUES (?, DATE('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
            ON CONFLICT(cik, score_date) DO UPDATE SET
              insider_score = excluded.insider_score,
              cluster_score = excluded.cluster_score,
              ownership_score = excluded.ownership_score,
              whale_score = excluded.whale_score,
              event_score = excluded.event_score,
              convergence_bonus = excluded.convergence_bonus,
              raw_score = excluded.raw_score,
              hcs_score = excluded.hcs_score,
              classification = excluded.classification,
              confirming_manager_count=excluded.confirming_manager_count,
              strongest_manager_score=excluded.strongest_manager_score,
              aggregate_manager_signal=excluded.aggregate_manager_signal,
              hcs_version=excluded.hcs_version,
              institutional_version=excluded.institutional_version,
              universe_version=excluded.universe_version,
              scoring_engine_version=excluded.scoring_engine_version,
              evidence_fingerprint=excluded.evidence_fingerprint,
              reason_codes_json=excluded.reason_codes_json,
              score_delta=excluded.score_delta,
              scored_at=CURRENT_TIMESTAMP
            """
        ).bind(
            cik, breakdown.insider_score, breakdown.cluster_score, breakdown.ownership_score,
            breakdown.whale_score, breakdown.event_score, breakdown.convergence_bonus,
            breakdown.raw_score, breakdown.hcs_score, breakdown.classification,
            int(institutional.get("confirming_manager_count") or 0),
            float(institutional.get("strongest_manager_score") or 0),
            float(institutional.get("aggregate_manager_signal") or 0),
            HCS_VERSION, INSTITUTIONAL_VERSION, UNIVERSE_VERSION,
            HCS_VERSION, evidence_fingerprint, json.dumps(reason_codes, separators=(",", ":")), score_delta,
        ).run()
    except Exception:
        await env.DB.prepare(
            """
            INSERT INTO scores
            (cik, score_date, insider_score, cluster_score, ownership_score, whale_score,
             event_score, convergence_bonus, raw_score, hcs_score, classification)
            VALUES (?, DATE('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cik, score_date) DO UPDATE SET
              insider_score = excluded.insider_score,
              cluster_score = excluded.cluster_score,
              ownership_score = excluded.ownership_score,
              whale_score = excluded.whale_score,
              event_score = excluded.event_score,
              convergence_bonus = excluded.convergence_bonus,
              raw_score = excluded.raw_score,
              hcs_score = excluded.hcs_score,
              classification = excluded.classification
            """
        ).bind(
            cik, breakdown.insider_score, breakdown.cluster_score, breakdown.ownership_score,
            breakdown.whale_score, breakdown.event_score, breakdown.convergence_bonus,
            breakdown.raw_score, breakdown.hcs_score, breakdown.classification,
        ).run()

    institutional_detail = {
        "manager_name": institutional.get("manager_name"),
        "period_of_report": institutional.get("period_of_report"),
        "filing_date": institutional.get("filing_date"),
        "share_change_pct": institutional.get("share_change_pct"),
        "position_weight_pct": institutional.get("position_weight_pct"),
        "confirming_manager_count": institutional.get("confirming_manager_count"),
        "strongest_manager_score": institutional.get("strongest_manager_score"),
        "aggregate_manager_signal": institutional.get("aggregate_manager_signal"),
        "mapping_method": institutional.get("mapping_method"),
        "anchor_date": anchor_date,
    }
    evidence = [
        {
            "component": "insider", "score": insider_score, "source_type": "form4",
            "source_id": insider_rep.get("id"), "event_date": insider_rep.get("transaction_date"),
            "filing_date": insider_rep.get("filed_at"),
            "detail": {"transaction_value": insider_rep.get("transaction_value"), "role": insider_rep.get("insider_role")},
        },
        {
            "component": "cluster", "score": cluster, "source_type": "form4_cluster",
            "source_id": None, "event_date": buyer_row.get("latest_transaction_date"),
            "filing_date": buyer_row.get("latest_filed_at"), "detail": {"buyer_count": count},
        },
        {
            "component": "ownership", "score": ownership_score, "source_type": "schedule13",
            "source_id": ownership_rep.get("id"), "event_date": ownership_rep.get("event_date"),
            "filing_date": ownership_rep.get("filed_at"),
            "detail": {"schedule_type": ownership_rep.get("schedule_type"), "ownership_pct": ownership_rep.get("group_ownership_pct")},
        },
        {
            "component": "institutional", "score": whale_score, "source_type": "13f",
            "source_id": institutional.get("source_id"), "event_date": institutional.get("period_of_report"),
            "filing_date": institutional.get("filing_date"), "detail": institutional_detail,
        },
        {
            "component": "event", "score": event_score, "source_type": "8k",
            "source_id": event_rep.get("id"), "event_date": event_rep.get("event_date"),
            "filing_date": event_rep.get("filed_at"),
            "detail": {"event_type": event_rep.get("event_type"), "primary_item": event_rep.get("primary_item"), "reason": event_rep.get("scoring_reason")},
        },
        {"component": "capital_allocation", "score": 0, "source_type": None, "source_id": None, "event_date": None, "filing_date": None, "detail": {}},
        {"component": "convergence", "score": convergence, "source_type": "derived", "source_id": None, "event_date": anchor_date, "filing_date": None, "detail": {"positive_families": positive_families}},
        {"component": "penalty", "score": 0, "source_type": "derived", "source_id": None, "event_date": None, "filing_date": None, "detail": {}},
    ]
    await _store_component_evidence(env, cik, evidence)

    # Recompute the receipt with source identifiers once evidence is assembled.
    evidence_receipt = [
        {
            "component": item.get("component"),
            "score": float(item.get("score") or 0),
            "source_type": item.get("source_type"),
            "source_id": item.get("source_id"),
            "event_date": item.get("event_date"),
            "filing_date": item.get("filing_date"),
        }
        for item in evidence
    ]
    evidence_fingerprint = hashlib.sha256(
        json.dumps({
            "cik": cik,
            "hcs_version": HCS_VERSION,
            "institutional_version": INSTITUTIONAL_VERSION,
            "universe_version": UNIVERSE_VERSION,
            "evidence": evidence_receipt,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    try:
        await env.DB.prepare(
            "UPDATE scores SET evidence_fingerprint=?, reason_codes_json=?, scoring_engine_version=?, score_delta=? "
            "WHERE cik=? AND score_date=DATE('now')"
        ).bind(
            evidence_fingerprint, json.dumps(reason_codes, separators=(",", ":")),
            HCS_VERSION, score_delta, cik,
        ).run()
    except Exception:
        pass
