"""Build public evidence from the exact sources persisted during scoring.

Public responses intentionally omit component scores and internal weighting
metadata. The source IDs in score_component_evidence are used only to resolve
back to the filing rows that actually produced the current score.
"""


def _rows(result):
    return getattr(result, "results", None) or []


async def _provenance(env, cik: str, score_date: str | None) -> dict[str, dict]:
    if not score_date:
        return {}
    result = await env.DB.prepare(
        """
        SELECT component, source_type, source_id, event_date, filing_date
        FROM score_component_evidence
        WHERE cik=? AND score_date=?
        """
    ).bind(cik, score_date).run()
    return {str(row.get("component")): row for row in _rows(result)}


async def build_public_evidence(
    env,
    cik: str,
    score_date: str | None,
    signal_keys: set[str],
) -> list[dict]:
    """Return SEC evidence tied to the current materialized score."""
    try:
        provenance = await _provenance(env, cik, score_date)
    except Exception:
        provenance = {}

    evidence: list[dict] = []

    insider = provenance.get("insider") or {}
    if "insider" in signal_keys and insider.get("source_id"):
        result = await env.DB.prepare(
            """
            SELECT it.transaction_date, it.insider_name, it.insider_role,
                   it.shares, it.price, it.transaction_value,
                   f.form_type, f.filed_at, f.accepted_at, f.filing_url, f.accession_number
            FROM insider_transactions it
            JOIN filings f ON f.id=it.filing_id
            WHERE it.id=? AND it.cik=?
            LIMIT 1
            """
        ).bind(insider.get("source_id"), cik).run()
        for item in _rows(result):
            evidence.append({
                "family": "Insider Buying",
                "evidence_role": "primary",
                "event_date": item.get("transaction_date"),
                "form_type": item.get("form_type"),
                "filed_at": item.get("filed_at"),
                "accepted_at": item.get("accepted_at"),
                "filing_url": item.get("filing_url"),
                "accession_number": item.get("accession_number"),
                "insider_name": item.get("insider_name"),
                "insider_role": item.get("insider_role"),
                "shares": item.get("shares"),
                "price": item.get("price"),
                "transaction_value": item.get("transaction_value"),
            })

    if "cluster" in signal_keys:
        result = await env.DB.prepare(
            """
            WITH ranked AS (
                SELECT it.transaction_date, it.insider_name, it.insider_role,
                       it.shares, it.price, it.transaction_value,
                       f.form_type, f.filed_at, f.accepted_at, f.filing_url, f.accession_number,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(it.insider_name, CAST(it.id AS TEXT))
                           ORDER BY it.transaction_date DESC, it.id DESC
                       ) AS rn
                FROM insider_transactions it
                JOIN filings f ON f.id=it.filing_id
                WHERE it.cik=?
                  AND it.is_open_market_purchase=1
                  AND it.transaction_date >= DATE('now','-30 day')
            )
            SELECT * FROM ranked WHERE rn=1
            ORDER BY transaction_date DESC, insider_name
            LIMIT 10
            """
        ).bind(cik).run()
        for item in _rows(result):
            evidence.append({
                "family": "Insider Cluster",
                "evidence_role": "cluster_member",
                "event_date": item.get("transaction_date"),
                "form_type": item.get("form_type"),
                "filed_at": item.get("filed_at"),
                "accepted_at": item.get("accepted_at"),
                "filing_url": item.get("filing_url"),
                "accession_number": item.get("accession_number"),
                "insider_name": item.get("insider_name"),
                "insider_role": item.get("insider_role"),
                "shares": item.get("shares"),
                "price": item.get("price"),
                "transaction_value": item.get("transaction_value"),
            })

    ownership = provenance.get("ownership") or {}
    if "ownership" in signal_keys and ownership.get("source_id"):
        result = await env.DB.prepare(
            """
            SELECT og.event_date, og.schedule_type,
                   og.representative_investor_name AS investor_name,
                   og.group_ownership_pct AS ownership_pct,
                   og.previous_ownership_pct, og.ownership_change_pp,
                   f.form_type, f.filed_at, f.accepted_at, f.filing_url, f.accession_number
            FROM ownership_groups og
            JOIN filings f ON f.id=og.filing_id
            WHERE og.id=? AND og.issuer_cik=?
            LIMIT 1
            """
        ).bind(ownership.get("source_id"), cik).run()
        for item in _rows(result):
            evidence.append({
                "family": "Beneficial Ownership",
                "evidence_role": "primary",
                "event_date": item.get("event_date"),
                "form_type": item.get("form_type"),
                "filed_at": item.get("filed_at"),
                "accepted_at": item.get("accepted_at"),
                "filing_url": item.get("filing_url"),
                "accession_number": item.get("accession_number"),
                "investor_name": item.get("investor_name"),
                "schedule_type": item.get("schedule_type"),
                "ownership_pct": item.get("ownership_pct"),
                "previous_ownership_pct": item.get("previous_ownership_pct"),
                "ownership_change_pp": item.get("ownership_change_pp"),
            })

    institutional = provenance.get("institutional") or {}
    if "institutional" in signal_keys and institutional.get("source_id"):
        result = await env.DB.prepare(
            """
            SELECT ip.period_of_report, ip.manager_name, ip.shares,
                   ip.previous_shares, ip.share_change_pct, ip.position_weight_pct,
                   f.form_type, f.filed_at, f.accepted_at, f.filing_url, f.accession_number
            FROM institutional_positions ip
            JOIN filings f ON f.id=ip.filing_id
            WHERE ip.id=? AND ip.issuer_cik=?
              AND COALESCE(ip.issuer_mapping_status,'resolved')='resolved'
            LIMIT 1
            """
        ).bind(institutional.get("source_id"), cik).run()
        for item in _rows(result):
            evidence.append({
                "family": "Institutional Accumulation",
                "evidence_role": "primary",
                "event_date": item.get("period_of_report"),
                "form_type": item.get("form_type"),
                "filed_at": item.get("filed_at"),
                "accepted_at": item.get("accepted_at"),
                "filing_url": item.get("filing_url"),
                "accession_number": item.get("accession_number"),
                "manager_name": item.get("manager_name"),
                "shares": item.get("shares"),
                "previous_shares": item.get("previous_shares"),
                "share_change_pct": item.get("share_change_pct"),
                "position_weight_pct": item.get("position_weight_pct"),
                "scoring_reason": (
                    f"{item.get('manager_name') or 'A tracked institutional manager'} "
                    + (
                        f"increased its reported position {float(item.get('share_change_pct')):.1f}% quarter over quarter. "
                        if item.get("share_change_pct") is not None
                        else "reported a material new or increased position. "
                    )
                    + "13F data is delayed and is used only as confirmation of fresher signals."
                ),
            })

    event = provenance.get("event") or {}
    if "event" in signal_keys and event.get("source_id"):
        result = await env.DB.prepare(
            """
            SELECT e.event_date, e.item_numbers, e.event_type, e.sentiment,
                   e.primary_item, e.scoring_reason,
                   f.form_type, f.filed_at, f.accepted_at, f.filing_url, f.accession_number
            FROM eight_k_events e
            JOIN filings f ON f.id=e.filing_id
            WHERE e.id=? AND e.cik=?
            LIMIT 1
            """
        ).bind(event.get("source_id"), cik).run()
        for item in _rows(result):
            evidence.append({
                "family": "Corporate Event",
                "evidence_role": "primary",
                "event_date": item.get("event_date"),
                "form_type": item.get("form_type"),
                "filed_at": item.get("filed_at"),
                "accepted_at": item.get("accepted_at"),
                "filing_url": item.get("filing_url"),
                "accession_number": item.get("accession_number"),
                "item_numbers": item.get("item_numbers"),
                "event_type": item.get("event_type"),
                "sentiment": item.get("sentiment"),
                "primary_item": item.get("primary_item"),
                "scoring_reason": item.get("scoring_reason"),
            })

    # Keep source evidence deterministic and compact.
    evidence.sort(
        key=lambda item: (
            item.get("event_date") or item.get("filed_at") or "",
            item.get("family") or "",
            item.get("accession_number") or "",
        ),
        reverse=True,
    )
    return evidence[:24]
