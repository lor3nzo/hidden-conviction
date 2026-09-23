import json
import re
from dataclasses import asdict

from scoring.hcs import score_13f_position
from recompute import recompute_today_score
from score_queue import enqueue_score_recompute
from sec.client import sec_fetch_text
from sec.company_map import COMPANY_TICKERS_EXCHANGE_URL, parse_company_tickers_exchange
from sec.thirteen_f import (
    ThirteenFPosition,
    information_table_filename,
    parse_13f_information_table,
    parse_13f_information_table_batch,
    parse_13f_information_table_summary,
    parse_13f_submission_metadata,
    infer_13f_value_scale,
)
from universe import classify_issuer, apply_universe_override, UNIVERSE_VERSION
from security_master import (
    SECURITY_MASTER_VERSION,
    make_candidate,
    normalize_security_name,
    resolve_candidates,
)

CHUNK_SIZE = 5
CHUNK_INSERT_BATCH = 10
RAW_SCAN_BATCH = 50
RAW_STAGE_INSERT_BATCH = 10
MAX_POSITIONS = 250
MAX_HARD_ATTEMPTS = 3
MAX_CANONICAL_SUBMISSION_CHARS = 12_000_000
SECURITY_RESOLUTION_BATCH = 20
SECURITY_RESOLUTION_HARD_CAP = 25
_SEC_COMPANY_DIRECTORY_CACHE = None
_LOCAL_COMPANY_MAP_CACHE = None


def _rows(result):
    return getattr(result, "results", None) or []


def _env_value(env, name, default=None):
    try:
        value = getattr(env, name)
        return value if value not in (None, "") else default
    except Exception:
        return default


def _normalize_company_name(value: str | None) -> str:
    text = (value or "").upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    tokens = text.split()
    canonical = []
    aliases = {
        "SYSTEMS": "SYS",
        "SYS": "SYS",
        "LABORATORIES": "LABS",
        "LABS": "LABS",
        "MATERIALS": "MATLS",
        "MATLS": "MATLS",
    }
    for token in tokens:
        if token == "AND":
            continue
        canonical.append(aliases.get(token, token))
    text = " ".join(canonical)
    # Strip common 13F/security-name noise before legal suffixes.
    text = re.sub(r"\b(CL|CLASS)\s+[A-Z0-9]+$", "", text).strip()
    text = re.sub(r"\b(NEW|DEL)$", "", text).strip()
    suffixes = (" INCORPORATED", " CORPORATION", " LIMITED", " INC", " CORP", " LTD", " PLC", " COMPANY", " CO")
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
                break
    return " ".join(text.split())


def _sec_submission_text_url(filer_cik: str, accession: str) -> str:
    cik_int = str(int(filer_cik))
    accession_no_dash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dash}/{accession}.txt"




def _sec_index_json_url(filing_url: str) -> str:
    return filing_url.rsplit("/", 1)[0] + "/index.json"


def _canonical_submission_url(filing: dict) -> str | None:
    accession = filing.get("accession_number")
    cik = filing.get("cik")
    filing_url = filing.get("filing_url")
    if accession and cik:
        return _sec_submission_text_url(str(cik), accession)
    if accession and filing_url:
        match = re.search(r"/data/(\d+)/", filing_url)
        if match:
            return _sec_submission_text_url(match.group(1), accession)
    return filing_url


async def _fallback_information_table_url(env, filing_url: str) -> str | None:
    """Resolve one likely information-table attachment without parsing it."""
    user_agent = _env_value(env, "SEC_USER_AGENT")
    try:
        index_text = await sec_fetch_text(_sec_index_json_url(filing_url), user_agent=user_agent)
        payload = json.loads(index_text)
    except Exception:
        return None

    items = ((payload.get("directory") or {}).get("item") or [])
    candidates = []
    for item in items:
        name = (item.get("name") or "").strip()
        lower = name.lower()
        if not name or not (lower.endswith(".xml") or lower.endswith(".txt")):
            continue
        if lower == "index.json":
            continue
        if re.fullmatch(r"\d{10}-\d{2}-\d{6}\.txt", lower):
            continue
        keyword_rank = 0 if any(token in lower for token in ("13f", "info", "table", "holding", "list")) else 1
        xml_rank = 0 if lower.endswith(".xml") else 1
        primary_rank = 1 if "primary" in lower else 0
        size = int(item.get("size") or 0)
        candidates.append((keyword_rank, primary_rank, xml_rank, -size, name))

    if not candidates:
        return None
    candidates.sort()
    base = filing_url.rsplit("/", 1)[0]
    return f"{base}/{candidates[0][-1]}"


async def _fallback_information_table_summary(env, filing_url: str):
    """One-shot attachment fallback with v0.6.8 aggregate statistics."""
    user_agent = _env_value(env, "SEC_USER_AGENT")
    try:
        index_text = await sec_fetch_text(_sec_index_json_url(filing_url), user_agent=user_agent)
        payload = json.loads(index_text)
    except Exception:
        return [], 0, 0.0, 0, 0

    items = ((payload.get("directory") or {}).get("item") or [])
    candidates = []
    for item in items:
        name = (item.get("name") or "").strip()
        lower = name.lower()
        if not name or not (lower.endswith(".xml") or lower.endswith(".txt")):
            continue
        if lower == "index.json":
            continue
        if re.fullmatch(r"\d{10}-\d{2}-\d{6}\.txt", lower):
            continue
        keyword_rank = 0 if any(token in lower for token in ("13f", "info", "table", "holding", "list")) else 1
        xml_rank = 0 if lower.endswith(".xml") else 1
        primary_rank = 1 if "primary" in lower else 0
        size = int(item.get("size") or 0)
        candidates.append((keyword_rank, primary_rank, xml_rank, -size, name))

    if not candidates:
        return [], 0, 0.0, 0, 0

    candidates.sort()
    name = candidates[0][-1]
    base = filing_url.rsplit("/", 1)[0]
    try:
        body = await sec_fetch_text(f"{base}/{name}", user_agent=user_agent)
        return parse_13f_information_table_summary(body, limit=MAX_POSITIONS)
    except Exception:
        return [], 0, 0.0, 0, 0


async def _fallback_information_table(env, filing_url: str):
    """Backward-compatible two-value wrapper used by earlier tests/helpers."""
    positions, total, _, _, _ = await _fallback_information_table_summary(env, filing_url)
    return positions, total


def _name_keys(value: str | None) -> list[str]:
    base = _normalize_company_name(value)
    if not base:
        return []
    keys = [base]
    # Common 13F issuer-label noise that should not alter issuer identity.
    cleaned = re.sub(r"\b(CL|CLASS)\s+[A-Z0-9]+$", "", base).strip()
    cleaned = re.sub(r"\bDEL$", "", cleaned).strip()
    cleaned = _normalize_company_name(cleaned)
    if cleaned and cleaned not in keys:
        keys.append(cleaned)
    return keys


async def _sec_company_directory(env):
    global _SEC_COMPANY_DIRECTORY_CACHE
    if _SEC_COMPANY_DIRECTORY_CACHE is not None:
        return _SEC_COMPANY_DIRECTORY_CACHE

    user_agent = _env_value(env, "SEC_USER_AGENT")
    rows = parse_company_tickers_exchange(
        await sec_fetch_text(COMPANY_TICKERS_EXCHANGE_URL, user_agent=user_agent)
    )
    grouped = {}
    for row in rows:
        for key in _name_keys(row.get("company_name")):
            grouped.setdefault(key, []).append(row)

    resolved = {}
    for key, candidates in grouped.items():
        ciks = {str(r.get("cik")) for r in candidates if r.get("cik")}
        tickers = {str(r.get("ticker")) for r in candidates if r.get("ticker")}
        if len(ciks) == 1:
            representative = candidates[0].copy()
            representative["ticker"] = next(iter(tickers)) if len(tickers) == 1 else None
            resolved[key] = representative
    _SEC_COMPANY_DIRECTORY_CACHE = resolved
    return resolved


async def _persist_identity_matches(env, matches: list[dict]):
    """Persist SEC directory matches with audited universe classification."""
    global _LOCAL_COMPANY_MAP_CACHE
    unique = {}
    for match in matches:
        cik = str(match.get("cik") or "").strip()
        if cik:
            unique[cik] = match
    rows = list(unique.values())
    if not rows:
        return

    # One company write applies heuristics plus any explicit override. The CTE
    # keeps the historical two-write shape (companies + company_tickers) while
    # avoiding a per-chunk override lookup. Before migration 0026, the missing
    # override table causes this statement to fall back cleanly to legacy SQL.
    value_rows = ",".join("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in rows)
    params = []
    for row in rows:
        universe = classify_issuer(row.get("company_name"), row.get("ticker"), row.get("exchange"))
        params.extend([
            row.get("cik"), row.get("ticker"), row.get("company_name"), row.get("exchange"),
            universe.issuer_type, 1 if universe.hcs_eligible else 0,
            universe.exclusion_reason, universe.rule, universe.reason, UNIVERSE_VERSION,
        ])
    try:
        await env.DB.prepare(
            f"""
            WITH incoming(
                cik, ticker, company_name, exchange, issuer_type, hcs_eligible,
                hcs_exclusion_reason, universe_rule, universe_reason, universe_version
            ) AS (VALUES {value_rows})
            INSERT INTO companies
                (cik, ticker, company_name, exchange, issuer_type, hcs_eligible,
                 hcs_exclusion_reason, universe_rule, universe_reason, universe_version, updated_at)
            SELECT
                i.cik, i.ticker, i.company_name, i.exchange,
                CASE WHEN o.cik IS NOT NULL AND o.force_issuer_type IS NOT NULL
                     THEN o.force_issuer_type ELSE i.issuer_type END,
                CASE WHEN o.cik IS NOT NULL AND o.force_eligible IS NOT NULL
                     THEN o.force_eligible ELSE i.hcs_eligible END,
                CASE
                    WHEN COALESCE(o.force_eligible, i.hcs_eligible)=0
                    THEN COALESCE(o.reason, i.hcs_exclusion_reason)
                    ELSE NULL
                END,
                CASE WHEN o.cik IS NOT NULL AND (o.force_eligible IS NOT NULL OR o.force_issuer_type IS NOT NULL)
                     THEN 'explicit_override' ELSE i.universe_rule END,
                CASE WHEN o.cik IS NOT NULL AND (o.force_eligible IS NOT NULL OR o.force_issuer_type IS NOT NULL)
                     THEN o.reason ELSE i.universe_reason END,
                i.universe_version, CURRENT_TIMESTAMP
            FROM incoming i
            LEFT JOIN issuer_universe_overrides o ON o.cik=i.cik
            ON CONFLICT(cik) DO UPDATE SET
                ticker=COALESCE(excluded.ticker, companies.ticker),
                company_name=COALESCE(excluded.company_name, companies.company_name),
                exchange=COALESCE(excluded.exchange, companies.exchange),
                issuer_type=excluded.issuer_type,
                hcs_eligible=excluded.hcs_eligible,
                hcs_exclusion_reason=excluded.hcs_exclusion_reason,
                universe_rule=excluded.universe_rule,
                universe_reason=excluded.universe_reason,
                universe_version=excluded.universe_version,
                updated_at=CURRENT_TIMESTAMP
            """
        ).bind(*params).run()
    except Exception:
        legacy_values = ",".join("(?, ?, ?, ?, CURRENT_TIMESTAMP)" for _ in rows)
        legacy_params = []
        for row in rows:
            legacy_params.extend([row.get("cik"), row.get("ticker"), row.get("company_name"), row.get("exchange")])
        await env.DB.prepare(
            f"""
            INSERT INTO companies (cik, ticker, company_name, exchange, updated_at)
            VALUES {legacy_values}
            ON CONFLICT(cik) DO UPDATE SET
                ticker=COALESCE(excluded.ticker, companies.ticker),
                company_name=COALESCE(excluded.company_name, companies.company_name),
                exchange=COALESCE(excluded.exchange, companies.exchange),
                updated_at=CURRENT_TIMESTAMP
            """
        ).bind(*legacy_params).run()

    ticker_rows = [r for r in rows if r.get("ticker")]
    if ticker_rows:
        ticker_values = ",".join("(?, ?, ?, 'sec-13f-name-match')" for _ in ticker_rows)
        ticker_params = []
        for row in ticker_rows:
            ticker_params.extend([row.get("cik"), row.get("ticker"), row.get("exchange")])
        await env.DB.prepare(
            f"INSERT OR IGNORE INTO company_tickers (cik, ticker, exchange, source) VALUES {ticker_values}"
        ).bind(*ticker_params).run()

    if _LOCAL_COMPANY_MAP_CACHE is not None:
        for row in rows:
            for key in _name_keys(row.get("company_name")):
                existing = _LOCAL_COMPANY_MAP_CACHE.get(key)
                if not existing or str(existing.get("cik")) == str(row.get("cik")):
                    cached = row.copy()
                    cached["identity_method"] = "normalized_name"
                    _LOCAL_COMPANY_MAP_CACHE[key] = cached
                else:
                    _LOCAL_COMPANY_MAP_CACHE.pop(key, None)


async def _resolve_company_identities(env, positions: list[dict]) -> dict[str, dict]:
    """Resolve one small chunk without per-position network or D1 writes."""
    local = await _company_map(env)
    resolved = {}
    unresolved = []
    for pos in positions:
        name = pos.get("issuer_name") or ""
        match = None
        for key in _name_keys(name):
            match = local.get(key)
            if match:
                break
        if match:
            resolved[name] = match
        else:
            unresolved.append(name)

    if not unresolved:
        return resolved

    try:
        directory = await _sec_company_directory(env)
    except Exception:
        return resolved

    new_matches = []
    for name in unresolved:
        match = None
        for key in _name_keys(name):
            match = directory.get(key)
            if match:
                break
        if not match:
            continue
        match = match.copy()
        match["identity_method"] = "sec_directory_name"
        resolved[name] = match
        new_matches.append(match)

    # Persist all newly resolved identities in bulk instead of issuing one or
    # two D1 writes for every position in the chunk.
    await _persist_identity_matches(env, new_matches)
    return resolved


async def _security_master_lookup(env, positions: list[dict]) -> dict[str, dict]:
    cusips = sorted({str(p.get("cusip") or "").upper() for p in positions if p.get("cusip")})
    if not cusips:
        return {}
    placeholders = ",".join("?" for _ in cusips)
    try:
        result = await env.DB.prepare(
            f"""
            SELECT cusip, ticker, issuer_cik, issuer_name, mapping_method,
                   mapping_confidence, mapping_status
            FROM security_master
            WHERE cusip IN ({placeholders})
            """
        ).bind(*cusips).run()
    except Exception:
        return {}
    return {str(r.get("cusip") or "").upper(): r for r in _rows(result)}


async def _approved_alias_lookup(env, positions: list[dict]) -> dict[str, dict]:
    keys = sorted({normalize_security_name(p.get("issuer_name")) for p in positions if p.get("issuer_name")})
    keys = [key for key in keys if key]
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    try:
        result = await env.DB.prepare(
            f"""
            SELECT a.alias_name_norm, a.issuer_cik, c.ticker, c.company_name
            FROM security_aliases a
            LEFT JOIN companies c ON c.cik=a.issuer_cik
            WHERE a.approved=1 AND a.alias_name_norm IN ({placeholders})
            """
        ).bind(*keys).run()
    except Exception:
        return {}
    return {r.get("alias_name_norm"): r for r in _rows(result)}


async def _ticker_identity_lookup(env, positions: list[dict]) -> dict[str, dict]:
    tickers = sorted({str(p.get("ticker") or "").upper().strip() for p in positions if p.get("ticker")})
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    try:
        result = await env.DB.prepare(
            f"""
            WITH candidates AS (
                SELECT UPPER(ticker) AS ticker, cik FROM companies WHERE UPPER(ticker) IN ({placeholders})
                UNION ALL
                SELECT UPPER(ticker) AS ticker, cik FROM company_tickers WHERE UPPER(ticker) IN ({placeholders})
            ), unique_ticker AS (
                SELECT ticker, MIN(cik) AS cik
                FROM candidates
                GROUP BY ticker
                HAVING COUNT(DISTINCT cik)=1
            )
            SELECT u.ticker, u.cik, c.company_name
            FROM unique_ticker u LEFT JOIN companies c ON c.cik=u.cik
            """
        ).bind(*tickers, *tickers).run()
    except Exception:
        return {}
    return {str(r.get("ticker") or "").upper(): r for r in _rows(result)}


async def _resolve_security_identities(env, positions: list[dict]) -> dict[str, dict]:
    """Resolve CUSIP/ticker/name evidence conservatively and surface conflicts."""
    master = await _security_master_lookup(env, positions)
    aliases = await _approved_alias_lookup(env, positions)
    tickers = await _ticker_identity_lookup(env, positions)
    fallback = await _resolve_company_identities(env, positions)
    output: dict[str, dict] = {}

    for pos in positions:
        cusip = str(pos.get("cusip") or "").upper()
        issuer_name = pos.get("issuer_name") or ""
        candidates = []

        sm = master.get(cusip)
        if sm and sm.get("mapping_status") == "resolved" and sm.get("issuer_cik"):
            candidate = make_candidate(
                cik=sm.get("issuer_cik"), ticker=sm.get("ticker"),
                company_name=sm.get("issuer_name"), method="security_master_cusip",
            )
            if candidate:
                candidates.append(candidate)

        ticker = str(pos.get("ticker") or "").upper().strip()
        tm = tickers.get(ticker) if ticker else None
        if tm:
            candidate = make_candidate(
                cik=tm.get("cik"), ticker=ticker, company_name=tm.get("company_name"),
                method="ticker_exact",
            )
            if candidate:
                candidates.append(candidate)

        alias = aliases.get(normalize_security_name(issuer_name))
        if alias:
            candidate = make_candidate(
                cik=alias.get("issuer_cik"), ticker=alias.get("ticker"),
                company_name=alias.get("company_name"), method="approved_alias",
            )
            if candidate:
                candidates.append(candidate)

        name_match = fallback.get(issuer_name)
        if name_match:
            method = name_match.get("identity_method") or "normalized_name"
            candidate = make_candidate(
                cik=name_match.get("cik"), ticker=name_match.get("ticker"),
                company_name=name_match.get("company_name"), method=method,
            )
            if candidate:
                candidates.append(candidate)

        resolution = resolve_candidates(candidates)
        output[cusip or issuer_name] = {
            "cik": resolution.issuer_cik,
            "ticker": resolution.ticker,
            "company_name": resolution.company_name,
            "mapping_method": resolution.method,
            "mapping_confidence": resolution.confidence,
            "mapping_status": resolution.status,
            "normalized_issuer_name": normalize_security_name(issuer_name),
        }
    return output


async def _persist_security_master_updates(env, rows: list[dict]):
    """Learn only resolved high-confidence CUSIP mappings; conflicts remain explicit."""
    if not rows:
        return
    try:
        for row in rows:
            cusip = row.get("cusip")
            if not cusip:
                continue
            status = row.get("mapping_status") or "unresolved"
            cik = row.get("issuer_cik") if status == "resolved" else None
            await env.DB.prepare(
                """
                INSERT INTO security_master
                    (cusip, ticker, issuer_cik, issuer_name, normalized_issuer_name,
                     mapping_method, mapping_confidence, mapping_status, mapping_version,
                     first_seen, last_seen, source_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(cusip) DO UPDATE SET
                    ticker=CASE WHEN security_master.mapping_status='conflict' THEN security_master.ticker ELSE COALESCE(excluded.ticker, security_master.ticker) END,
                    issuer_cik=CASE
                        WHEN excluded.mapping_status='conflict' THEN NULL
                        WHEN security_master.issuer_cik IS NOT NULL AND excluded.issuer_cik IS NOT NULL
                             AND security_master.issuer_cik <> excluded.issuer_cik THEN NULL
                        WHEN security_master.mapping_status='conflict' THEN NULL
                        ELSE COALESCE(security_master.issuer_cik, excluded.issuer_cik)
                    END,
                    issuer_name=COALESCE(excluded.issuer_name, security_master.issuer_name),
                    normalized_issuer_name=COALESCE(excluded.normalized_issuer_name, security_master.normalized_issuer_name),
                    mapping_method=CASE
                        WHEN excluded.mapping_status='conflict' THEN 'conflict'
                        WHEN security_master.issuer_cik IS NOT NULL AND excluded.issuer_cik IS NOT NULL
                             AND security_master.issuer_cik <> excluded.issuer_cik THEN 'conflict'
                        WHEN security_master.mapping_status='conflict' THEN 'conflict'
                        ELSE excluded.mapping_method
                    END,
                    mapping_confidence=CASE
                        WHEN excluded.mapping_status='conflict' THEN 0
                        WHEN security_master.issuer_cik IS NOT NULL AND excluded.issuer_cik IS NOT NULL
                             AND security_master.issuer_cik <> excluded.issuer_cik THEN 0
                        WHEN security_master.mapping_status='conflict' THEN 0
                        ELSE MAX(security_master.mapping_confidence, excluded.mapping_confidence)
                    END,
                    mapping_status=CASE
                        WHEN excluded.mapping_status='conflict' THEN 'conflict'
                        WHEN security_master.issuer_cik IS NOT NULL AND excluded.issuer_cik IS NOT NULL
                             AND security_master.issuer_cik <> excluded.issuer_cik THEN 'conflict'
                        WHEN security_master.mapping_status='conflict' THEN 'conflict'
                        ELSE excluded.mapping_status
                    END,
                    mapping_version=excluded.mapping_version,
                    last_seen=CURRENT_TIMESTAMP,
                    source_count=security_master.source_count+1,
                    updated_at=CURRENT_TIMESTAMP
                """
            ).bind(
                cusip, row.get("ticker"), cik, row.get("issuer_name"),
                row.get("normalized_issuer_name"), row.get("mapping_method"),
                float(row.get("mapping_confidence") or 0), status, SECURITY_MASTER_VERSION,
            ).run()

            # High-confidence CUSIP learning is retroactive: once a CUSIP is
            # resolved, older unresolved rows inherit that mapping. This mutates
            # source identity only, never historical HCS rows. Today's affected
            # issuer is queued for an explicit versioned recompute.
            if status == "resolved" and cik:
                await env.DB.prepare(
                    """
                    UPDATE institutional_positions
                    SET issuer_cik=?, ticker=COALESCE(ticker, ?),
                        issuer_mapping_method='security_master_cusip',
                        issuer_mapping_confidence=1.0,
                        issuer_mapping_status='resolved',
                        issuer_mapping_version=?, issuer_mapping_updated_at=CURRENT_TIMESTAMP
                    WHERE UPPER(cusip)=UPPER(?)
                      AND issuer_mapping_status='unresolved'
                    """
                ).bind(cik, row.get("ticker"), SECURITY_MASTER_VERSION, cusip).run()
                await enqueue_score_recompute(
                    env, cik, "security-master learned CUSIP mapping"
                )
                await env.DB.prepare(
                    """
                    UPDATE security_resolution_queue
                    SET status='done', processed_at=CURRENT_TIMESTAMP, last_error=NULL
                    WHERE position_id IN (
                        SELECT id FROM institutional_positions
                        WHERE UPPER(cusip)=UPPER(?) AND issuer_mapping_status='resolved'
                    ) AND status IN ('pending','unresolved')
                    """
                ).bind(cusip).run()
    except Exception:
        # Worker may be deployed just before migration 0026. Keep ingestion alive.
        return


async def resolve_unmapped_security_positions(env, limit: int = SECURITY_RESOLUTION_BATCH) -> dict:
    """Resolve a bounded batch of unique securities, not individual history rows.

    v0.8.2 prioritizes securities with positive institutional scores, chooses one
    representative row per CUSIP, and applies one resolution result to every
    queued historical position sharing that CUSIP. Rows without a CUSIP remain
    deliberately position-scoped so name-only evidence cannot over-group issuers.
    """
    batch_limit = max(1, min(int(limit), SECURITY_RESOLUTION_HARD_CAP))
    try:
        result = await env.DB.prepare(
            """
            WITH pending AS (
                SELECT
                    q.position_id,
                    q.attempts,
                    q.created_at,
                    ip.cusip,
                    ip.issuer_name,
                    ip.ticker,
                    ip.title_of_class,
                    ip.position_score,
                    ip.period_of_report,
                    ip.filing_id,
                    CASE
                        WHEN COALESCE(TRIM(ip.cusip),'') <> ''
                            THEN 'CUSIP:' || UPPER(TRIM(ip.cusip))
                        ELSE 'POSITION:' || CAST(ip.id AS TEXT)
                    END AS security_key,
                    ROW_NUMBER() OVER (
                        PARTITION BY CASE
                            WHEN COALESCE(TRIM(ip.cusip),'') <> ''
                                THEN 'CUSIP:' || UPPER(TRIM(ip.cusip))
                            ELSE 'POSITION:' || CAST(ip.id AS TEXT)
                        END
                        ORDER BY
                            CASE WHEN ip.position_score > 0 THEN 0 ELSE 1 END,
                            ip.period_of_report DESC,
                            ip.filing_id DESC,
                            ip.id DESC
                    ) AS rn
                FROM security_resolution_queue q
                JOIN institutional_positions ip ON ip.id=q.position_id
                WHERE q.status='pending'
            )
            SELECT position_id, attempts, created_at, cusip, issuer_name, ticker,
                   title_of_class, position_score, period_of_report, security_key
            FROM pending
            WHERE rn=1
            ORDER BY
                CASE WHEN position_score > 0 THEN 0 ELSE 1 END,
                period_of_report DESC,
                created_at,
                position_id
            LIMIT ?
            """
        ).bind(batch_limit).run()
    except Exception:
        return {
            "processed_positions": 0,
            "processed_securities": 0,
            "resolved_securities": 0,
            "unresolved_securities": 0,
            "conflict_securities": 0,
        }

    representatives = _rows(result)
    if not representatives:
        return {
            "processed_positions": 0,
            "processed_securities": 0,
            "resolved_securities": 0,
            "unresolved_securities": 0,
            "conflict_securities": 0,
        }

    positions = [
        {
            "cusip": row.get("cusip"),
            "issuer_name": row.get("issuer_name"),
            "ticker": row.get("ticker"),
            "title_of_class": row.get("title_of_class"),
        }
        for row in representatives
    ]
    identities = await _resolve_security_identities(env, positions)
    master_updates = []
    counts = {
        "processed_positions": 0,
        "processed_securities": 0,
        "resolved_securities": 0,
        "unresolved_securities": 0,
        "conflict_securities": 0,
    }

    for row in representatives:
        position_id = int(row.get("position_id"))
        cusip = str(row.get("cusip") or "").upper().strip()

        # Claim the whole pending history for one CUSIP in a single write. A
        # position without CUSIP is claimed individually to avoid unsafe name
        # grouping.
        if cusip:
            claim = await env.DB.prepare(
                """
                UPDATE security_resolution_queue
                SET status='processing', started_at=CURRENT_TIMESTAMP, attempts=attempts+1
                WHERE status='pending'
                  AND position_id IN (
                      SELECT id FROM institutional_positions
                      WHERE UPPER(TRIM(cusip))=?
                  )
                """
            ).bind(cusip).run()
        else:
            claim = await env.DB.prepare(
                """
                UPDATE security_resolution_queue
                SET status='processing', started_at=CURRENT_TIMESTAMP, attempts=attempts+1
                WHERE position_id=? AND status='pending'
                """
            ).bind(position_id).run()

        meta = getattr(claim, "meta", None) or {}
        claimed_positions = int(meta.get("changes", 0) or 0)
        if claimed_positions == 0:
            continue

        key = cusip or (row.get("issuer_name") or "")
        mapped = identities.get(key) or {}
        status = mapped.get("mapping_status") or "unresolved"
        terminal = status if status in {"unresolved", "conflict"} else "done"
        issuer_cik = mapped.get("cik") if status == "resolved" else None
        normalized_name = (
            mapped.get("normalized_issuer_name")
            or normalize_security_name(row.get("issuer_name"))
        )

        try:
            if cusip:
                await env.DB.prepare(
                    """
                    UPDATE institutional_positions
                    SET issuer_cik=?, ticker=CASE WHEN ? IS NOT NULL THEN COALESCE(ticker, ?) ELSE ticker END,
                        normalized_issuer_name=?, issuer_mapping_method=?,
                        issuer_mapping_confidence=?, issuer_mapping_status=?,
                        issuer_mapping_version=?, issuer_mapping_updated_at=CURRENT_TIMESTAMP
                    WHERE UPPER(TRIM(cusip))=?
                      AND issuer_mapping_status='unresolved'
                    """
                ).bind(
                    issuer_cik, mapped.get("ticker"), mapped.get("ticker"),
                    normalized_name, mapped.get("mapping_method") or status,
                    float(mapped.get("mapping_confidence") or 0), status,
                    SECURITY_MASTER_VERSION, cusip,
                ).run()
                await env.DB.prepare(
                    """
                    UPDATE security_resolution_queue
                    SET status=?, processed_at=CURRENT_TIMESTAMP,
                        started_at=NULL, last_error=NULL
                    WHERE status='processing'
                      AND position_id IN (
                          SELECT id FROM institutional_positions
                          WHERE UPPER(TRIM(cusip))=?
                      )
                    """
                ).bind(terminal, cusip).run()
            else:
                await env.DB.prepare(
                    """
                    UPDATE institutional_positions
                    SET issuer_cik=?, ticker=CASE WHEN ? IS NOT NULL THEN COALESCE(ticker, ?) ELSE ticker END,
                        normalized_issuer_name=?, issuer_mapping_method=?,
                        issuer_mapping_confidence=?, issuer_mapping_status=?,
                        issuer_mapping_version=?, issuer_mapping_updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND issuer_mapping_status='unresolved'
                    """
                ).bind(
                    issuer_cik, mapped.get("ticker"), mapped.get("ticker"),
                    normalized_name, mapped.get("mapping_method") or status,
                    float(mapped.get("mapping_confidence") or 0), status,
                    SECURITY_MASTER_VERSION, position_id,
                ).run()
                await env.DB.prepare(
                    """
                    UPDATE security_resolution_queue
                    SET status=?, processed_at=CURRENT_TIMESTAMP,
                        started_at=NULL, last_error=NULL
                    WHERE position_id=? AND status='processing'
                    """
                ).bind(terminal, position_id).run()

            counts["processed_positions"] += claimed_positions
            counts["processed_securities"] += 1
            counts[f"{status}_securities" if status in {"resolved", "unresolved", "conflict"} else "unresolved_securities"] += 1
            master_updates.append({
                "cusip": cusip,
                "ticker": mapped.get("ticker"),
                "issuer_cik": issuer_cik,
                "issuer_name": row.get("issuer_name"),
                "normalized_issuer_name": normalized_name,
                "mapping_method": mapped.get("mapping_method") or status,
                "mapping_confidence": float(mapped.get("mapping_confidence") or 0),
                "mapping_status": status,
            })
            if issuer_cik:
                await enqueue_score_recompute(
                    env, issuer_cik, "unique-security 13F identity resolved"
                )
        except Exception as exc:
            # Return all claimed rows for this security to pending unless the
            # representative has reached the hard retry limit.
            attempts = int(row.get("attempts") or 0) + 1
            next_status = 'error' if attempts >= 3 else 'pending'
            if cusip:
                await env.DB.prepare(
                    """
                    UPDATE security_resolution_queue
                    SET status=?, started_at=NULL, last_error=?
                    WHERE status='processing'
                      AND position_id IN (
                          SELECT id FROM institutional_positions
                          WHERE UPPER(TRIM(cusip))=?
                      )
                    """
                ).bind(next_status, str(exc)[:1000], cusip).run()
            else:
                await env.DB.prepare(
                    """
                    UPDATE security_resolution_queue
                    SET status=?, started_at=NULL, last_error=?
                    WHERE position_id=? AND status='processing'
                    """
                ).bind(next_status, str(exc)[:1000], position_id).run()

    await _persist_security_master_updates(env, master_updates)
    return counts


def _looks_like_fund(issuer_name: str | None, title_of_class: str | None = None) -> bool:
    """Conservative fund/ETF filter for company-level HCS.

    Use token-aware matching so fund families are caught even when the issuer
    name starts with the marker (for example ``ISHARES TR``).  Avoid broad
    matches such as plain ``SCHWAB`` or ``TRUST`` that could suppress operating
    companies or non-fund securities.
    """
    text = f"{issuer_name or ''} {title_of_class or ''}".upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = " ".join(text.split())
    patterns = (
        r"\bETF\b",
        r"\bEXCHANGE TRADED\b",
        r"\bISHARES\b",
        r"\bSPDR\b",
        r"\bVANGUARD\b",
        r"\bINVESCO QQQ\b",
        r"\bSELECT SECTOR SPDR\b",
        r"\bSCHWAB STRATEGIC TR(?:UST)?\b",
        r"\bPROSHARES\b",
        r"\bDIREXION\b",
        r"\bWISDOMTREE\b",
        r"\bVANECK\b",
        r"\bGLOBAL X\b",
        r"\bARK ETF\b",
        r"\bFIRST TR(?:UST)? EXCHANGE TRADED\b",
        r"\bPORTFOLIOS\b",
        r"\bINDEX FUND\b",
        r"\bMUTUAL FUND\b",
        r"\bTREASURY BILL\b",
        r"\bBOND ETF\b",
        r"\bACTIVE ETF\b",
        r"\bSERIES TRUST\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _submission_rows(payload: dict) -> list[dict]:
    recent = ((payload.get("filings") or {}).get("recent") or {})
    accessions = recent.get("accessionNumber") or []
    rows = []
    for i, accession in enumerate(accessions):
        row = {"accessionNumber": accession}
        for key in ("filingDate", "form", "primaryDocument", "reportDate"):
            values = recent.get(key) or []
            row[key] = values[i] if i < len(values) else None
        rows.append(row)
    return rows


def _is_13f_form(value: str | None) -> bool:
    return (value or "").upper().strip() in {"13F-HR", "13F-HR/A"}


def _previous_quarter_end(period: str | None) -> str | None:
    if not period or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
        return None
    year, month, day = map(int, period.split("-"))
    if (month, day) == (3, 31):
        return f"{year - 1}-12-31"
    if (month, day) == (6, 30):
        return f"{year}-03-31"
    if (month, day) == (9, 30):
        return f"{year}-06-30"
    if (month, day) == (12, 31):
        return f"{year}-09-30"
    return None


async def _manager_prior_candidate(env, manager_cik: str, current_accession: str, expected_period: str | None):
    user_agent = _env_value(env, "SEC_USER_AGENT")
    padded = str(manager_cik).zfill(10)
    text = await sec_fetch_text(
        f"https://data.sec.gov/submissions/CIK{padded}.json",
        user_agent=user_agent,
    )
    payload = json.loads(text)
    candidates = []
    for row in _submission_rows(payload):
        accession = row.get("accessionNumber")
        filed = row.get("filingDate")
        if (
            accession
            and accession != current_accession
            and _is_13f_form(row.get("form"))
            and (not expected_period or row.get("reportDate") == expected_period)
        ):
            candidates.append(row)
    candidates.sort(key=lambda r: (r.get("filingDate") or "", r.get("accessionNumber") or ""), reverse=True)
    return candidates[0] if candidates else None


async def _company_map(env) -> dict[str, dict]:
    """Return only unambiguous normalized issuer-name matches from local companies."""
    global _LOCAL_COMPANY_MAP_CACHE
    if _LOCAL_COMPANY_MAP_CACHE is not None:
        return _LOCAL_COMPANY_MAP_CACHE
    result = await env.DB.prepare("SELECT cik, ticker, company_name FROM companies").run()
    grouped: dict[str, list[dict]] = {}
    for row in _rows(result):
        key = _normalize_company_name(row.get("company_name"))
        if key:
            grouped.setdefault(key, []).append(row)
    output = {}
    for key, candidates in grouped.items():
        ciks = {str(r.get("cik")) for r in candidates if r.get("cik")}
        if len(ciks) == 1:
            row = candidates[0].copy()
            row["identity_method"] = "normalized_name"
            output[key] = row
    _LOCAL_COMPANY_MAP_CACHE = output
    return output


def _result_changes(result) -> int:
    meta = getattr(result, "meta", None)
    return int((meta or {}).get("changes", 0)) if meta else 0


async def _publish_work_id(env, work_id: int) -> bool:
    """Publish one pending work item exactly once per enqueue attempt.

    D1 is used as the claim record before the Queue send. If the Queue send
    fails, the claim is released so Cron can recover it later.
    """
    claimed = await env.DB.prepare(
        """
        UPDATE thirteen_f_work_items
        SET enqueued_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending' AND enqueued_at IS NULL
        """
    ).bind(work_id).run()
    if _result_changes(claimed) == 0:
        return False

    try:
        await env.THIRTEEN_F_QUEUE.send({"work_id": int(work_id), "pipeline": "13f"})
    except Exception:
        await env.DB.prepare(
            "UPDATE thirteen_f_work_items SET enqueued_at=NULL WHERE id=? AND status='pending'"
        ).bind(work_id).run()
        raise
    return True


async def _create_work_only(env, report_id: int, work_key: str, stage: str, payload: dict | None = None):
    """Create/update a pending work item without publishing it yet."""
    await env.DB.prepare(
        """
        INSERT INTO thirteen_f_work_items
        (report_id, work_key, stage, payload_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(report_id, work_key) DO UPDATE SET
            stage=excluded.stage,
            payload_json=CASE
                WHEN thirteen_f_work_items.status='done' THEN thirteen_f_work_items.payload_json
                ELSE excluded.payload_json
            END,
            status=CASE
                WHEN thirteen_f_work_items.status='done' THEN 'done'
                ELSE 'pending'
            END,
            started_at=NULL,
            enqueued_at=CASE
                WHEN thirteen_f_work_items.status='done' THEN thirteen_f_work_items.enqueued_at
                ELSE NULL
            END,
            processed_at=CASE
                WHEN thirteen_f_work_items.status='done' THEN thirteen_f_work_items.processed_at
                ELSE NULL
            END,
            last_error=CASE
                WHEN thirteen_f_work_items.status='done' THEN thirteen_f_work_items.last_error
                ELSE NULL
            END
        """
    ).bind(report_id, work_key, stage, json.dumps(payload) if payload is not None else None).run()


async def _enqueue_work(env, report_id: int, work_key: str, stage: str, payload: dict | None = None):
    await env.DB.prepare(
        """
        INSERT OR IGNORE INTO thirteen_f_work_items
        (report_id, work_key, stage, payload_json)
        VALUES (?, ?, ?, ?)
        """
    ).bind(report_id, work_key, stage, json.dumps(payload) if payload is not None else None).run()

    result = await env.DB.prepare(
        """
        SELECT id, enqueued_at, status
        FROM thirteen_f_work_items
        WHERE report_id = ? AND work_key = ?
        LIMIT 1
        """
    ).bind(report_id, work_key).run()
    rows = _rows(result)
    if not rows:
        return False
    row = rows[0]
    if row.get("status") != "pending" or row.get("enqueued_at"):
        return False
    return await _publish_work_id(env, int(row["id"]))


async def _bulk_create_chunk_work(env, report_id: int, chunks: list, total_value: float) -> int:
    """Create all chunk records with a handful of D1 writes, but enqueue none.

    This is the v0.6.6 fan-out fix. A 250-position filing produces 50 rows,
    inserted in groups of ten instead of 50 INSERT/SELECT/SEND/UPDATE cycles.
    Existing completed chunks are preserved so a recovered prepare job can
    safely rebuild missing chunk rows without losing progress.
    """
    expected = len(chunks)
    await env.DB.prepare(
        """
        DELETE FROM thirteen_f_work_items
        WHERE report_id=? AND stage='chunk'
          AND CAST(SUBSTR(work_key, 7) AS INTEGER) >= ?
        """
    ).bind(report_id, expected).run()

    for batch_start in range(0, expected, CHUNK_INSERT_BATCH):
        batch = chunks[batch_start:batch_start + CHUNK_INSERT_BATCH]
        values = []
        params = []
        for offset, chunk in enumerate(batch):
            index = batch_start + offset
            payload = {
                "total_value": total_value,
                "positions": [asdict(p) for p in chunk],
            }
            values.append("(?, ?, 'chunk', ?)")
            params.extend([report_id, f"chunk:{index:03d}", json.dumps(payload)])
        if not values:
            continue
        await env.DB.prepare(
            f"""
            INSERT INTO thirteen_f_work_items
            (report_id, work_key, stage, payload_json)
            VALUES {','.join(values)}
            ON CONFLICT(report_id, work_key) DO UPDATE SET
                stage='chunk',
                payload_json=CASE
                    WHEN thirteen_f_work_items.status='done' THEN thirteen_f_work_items.payload_json
                    ELSE excluded.payload_json
                END,
                status=CASE
                    WHEN thirteen_f_work_items.status='done' THEN 'done'
                    ELSE 'pending'
                END,
                attempts=CASE
                    WHEN thirteen_f_work_items.status='done' THEN thirteen_f_work_items.attempts
                    ELSE 0
                END,
                started_at=NULL,
                enqueued_at=CASE
                    WHEN thirteen_f_work_items.status='done' THEN thirteen_f_work_items.enqueued_at
                    ELSE NULL
                END,
                processed_at=CASE
                    WHEN thirteen_f_work_items.status='done' THEN thirteen_f_work_items.processed_at
                    ELSE NULL
                END,
                last_error=CASE
                    WHEN thirteen_f_work_items.status='done' THEN thirteen_f_work_items.last_error
                    ELSE NULL
                END
            """
        ).bind(*params).run()

    done_result = await env.DB.prepare(
        """
        SELECT COUNT(*) AS done_count
        FROM thirteen_f_work_items
        WHERE report_id=? AND stage='chunk' AND status='done'
          AND CAST(SUBSTR(work_key, 7) AS INTEGER) < ?
        """
    ).bind(report_id, expected).run()
    done_rows = _rows(done_result)
    done_count = int((done_rows[0].get("done_count") if done_rows else 0) or 0)

    await env.DB.prepare(
        "UPDATE thirteen_f_reports SET status='chunking', chunks_expected=?, chunks_done=? WHERE id=?"
    ).bind(expected, done_count, report_id).run()
    return done_count


async def _enqueue_first_incomplete_chunk(env, report_id: int) -> bool:
    result = await env.DB.prepare(
        """
        SELECT c.id
        FROM thirteen_f_work_items c
        WHERE c.report_id=? AND c.stage='chunk' AND c.status='pending'
          AND NOT EXISTS (
              SELECT 1
              FROM thirteen_f_work_items p
              WHERE p.report_id=c.report_id
                AND p.stage='chunk'
                AND p.work_key < c.work_key
                AND p.status <> 'done'
          )
        ORDER BY c.work_key
        LIMIT 1
        """
    ).bind(report_id).run()
    rows = _rows(result)
    if not rows:
        return False
    return await _publish_work_id(env, int(rows[0]["id"]))


async def _enqueue_next_chunk_after(env, report_id: int, work_key: str) -> bool:
    match = re.fullmatch(r"chunk:(\d{3})", work_key or "")
    if not match:
        return False
    next_key = f"chunk:{int(match.group(1)) + 1:03d}"
    result = await env.DB.prepare(
        "SELECT id, status, enqueued_at FROM thirteen_f_work_items WHERE report_id=? AND work_key=? LIMIT 1"
    ).bind(report_id, next_key).run()
    rows = _rows(result)
    if not rows:
        return False
    row = rows[0]
    if row.get("status") != "pending" or row.get("enqueued_at"):
        return False
    return await _publish_work_id(env, int(row["id"]))


async def _chunk_has_unfinished_predecessor(env, report_id: int, work_key: str) -> bool:
    result = await env.DB.prepare(
        """
        SELECT 1 AS blocked
        FROM thirteen_f_work_items
        WHERE report_id=? AND stage='chunk' AND work_key < ? AND status <> 'done'
        LIMIT 1
        """
    ).bind(report_id, work_key).run()
    return bool(_rows(result))


async def publish_pending_13f_work(env, limit: int = 20) -> int:
    """Publish only roots and serially eligible chunks.

    Cron is a recovery path, not a fan-out mechanism. At most the first
    unfinished chunk for each report is eligible, so dormant chunk rows remain
    in D1 until their predecessor completes.
    """
    result = await env.DB.prepare(
        """
        SELECT w.id
        FROM thirteen_f_work_items w
        WHERE w.status='pending' AND w.enqueued_at IS NULL
          AND (
              w.stage='prepare'
              OR (
                  w.stage='scan'
                  AND NOT EXISTS (
                      SELECT 1 FROM thirteen_f_work_items p
                      WHERE p.report_id=w.report_id
                        AND p.status <> 'done'
                        AND (p.stage='prepare' OR (p.stage='scan' AND p.work_key < w.work_key))
                  )
              )
              OR (
                  w.stage='assemble'
                  AND NOT EXISTS (
                      SELECT 1 FROM thirteen_f_work_items p
                      WHERE p.report_id=w.report_id
                        AND p.status <> 'done'
                        AND p.stage IN ('prepare','scan')
                  )
              )
              OR (
                  w.stage='chunk'
                  AND NOT EXISTS (
                      SELECT 1 FROM thirteen_f_work_items p
                      WHERE p.report_id=w.report_id
                        AND p.status <> 'done'
                        AND (p.stage IN ('prepare','scan','assemble')
                             OR (p.stage='chunk' AND p.work_key < w.work_key))
                  )
              )
              OR (
                  w.stage='finalize'
                  AND NOT EXISTS (
                      SELECT 1 FROM thirteen_f_work_items p
                      WHERE p.report_id=w.report_id
                        AND p.status <> 'done'
                        AND p.stage IN ('prepare','scan','assemble','chunk')
                  )
              )
          )
        ORDER BY
            CASE w.stage
                WHEN 'prepare' THEN 0
                WHEN 'scan' THEN 1
                WHEN 'assemble' THEN 2
                WHEN 'chunk' THEN 3
                WHEN 'finalize' THEN 4
                ELSE 5
            END,
            w.id
        LIMIT ?
        """
    ).bind(limit).run()
    count = 0
    for row in _rows(result):
        if await _publish_work_id(env, int(row["id"])):
            count += 1
    return count


async def recover_stale_13f_work(env) -> int:
    result = await env.DB.prepare(
        """
        UPDATE thirteen_f_work_items
        SET status = 'pending', started_at = NULL, enqueued_at = NULL,
            last_error = COALESCE(last_error, 'Recovered stale 13F work item')
        WHERE status = 'processing'
          AND started_at < DATETIME('now', '-10 minutes')
        """
    ).run()
    changes = _result_changes(result)

    # If a Worker died after claiming a pending item but before Queue.send,
    # release the old claim so Cron can publish it again.
    released = await env.DB.prepare(
        """
        UPDATE thirteen_f_work_items
        SET enqueued_at=NULL,
            last_error=COALESCE(last_error, 'Recovered stale 13F enqueue claim')
        WHERE status='pending'
          AND enqueued_at < DATETIME('now', '-10 minutes')
        """
    ).run()
    return changes + _result_changes(released)


async def ensure_report_for_filing(env, filing_id: int) -> int | None:
    await env.DB.prepare(
        "INSERT OR IGNORE INTO thirteen_f_reports (filing_id) VALUES (?)"
    ).bind(filing_id).run()
    result = await env.DB.prepare(
        "SELECT id, status FROM thirteen_f_reports WHERE filing_id = ? LIMIT 1"
    ).bind(filing_id).run()
    rows = _rows(result)
    if not rows:
        return None
    report_id = int(rows[0]["id"])
    if rows[0].get("status") not in {"done", "ignored", "error"}:
        await _enqueue_work(env, report_id, "prepare", "prepare")
    return report_id


async def _prepare_report(env, work: dict, report: dict, filing: dict):
    """Lightweight metadata/dependency stage for v0.6.9.

    No information-table rows are parsed here. Large filings are scanned in
    bounded batches by separate queue invocations.
    """
    report_id = int(report["id"])
    filing_id = int(report["filing_id"])
    user_agent = _env_value(env, "SEC_USER_AGENT")
    canonical_url = _canonical_submission_url(filing)

    text = None
    canonical_error = None
    try:
        text = await sec_fetch_text(canonical_url, user_agent=user_agent)
    except Exception as exc:
        canonical_error = exc

    if text is None and filing.get("filing_url") and filing.get("filing_url") != canonical_url:
        text = await sec_fetch_text(filing["filing_url"], user_agent=user_agent)
        canonical_url = filing["filing_url"]

    if text is None:
        raise RuntimeError(f"Unable to fetch canonical 13F submission: {canonical_error}")

    if len(text) > MAX_CANONICAL_SUBMISSION_CHARS:
        await env.DB.prepare(
            "UPDATE thirteen_f_reports SET status='error', processed_at=CURRENT_TIMESTAMP, last_error=? WHERE id=?"
        ).bind(
            f"Canonical 13F submission exceeds CPU-safe size cap ({len(text)} chars)", report_id
        ).run()
        await env.DB.prepare("UPDATE filings SET processed=1 WHERE id=?").bind(filing_id).run()
        return

    parsed = parse_13f_submission_metadata(text)
    if not parsed.period_of_report:
        await env.DB.prepare(
            "UPDATE thirteen_f_reports SET status='error', processed_at=CURRENT_TIMESTAMP, last_error='Missing 13F period of report' WHERE id=?"
        ).bind(report_id).run()
        await env.DB.prepare("UPDATE filings SET processed=1 WHERE id=?").bind(filing_id).run()
        return

    manager_cik = parsed.manager_cik or filing.get("cik")
    await env.DB.prepare(
        """
        UPDATE thirteen_f_reports
        SET manager_cik=?, manager_name=?, filing_date=?, period_of_report=?,
            status='pending', last_error=NULL
        WHERE id=?
        """
    ).bind(
        manager_cik, parsed.manager_name, parsed.filing_date, parsed.period_of_report, report_id,
    ).run()
    await env.DB.prepare(
        "UPDATE filings SET cik=?, company_name=? WHERE id=?"
    ).bind(manager_cik, parsed.manager_name, filing_id).run()

    is_historical = int(report.get("is_historical") or 0) == 1
    expected_prior_period = _previous_quarter_end(parsed.period_of_report)
    if not is_historical and manager_cik and expected_prior_period:
        local = await env.DB.prepare(
            """
            SELECT r.id, f.accession_number
            FROM thirteen_f_reports r
            JOIN filings f ON f.id = r.filing_id
            WHERE r.manager_cik=? AND r.id<>? AND r.status='done'
              AND r.period_of_report = ?
            ORDER BY r.id DESC
            LIMIT 1
            """
        ).bind(manager_cik, report_id, expected_prior_period).run()
        local_rows = _rows(local)
        if local_rows:
            await env.DB.prepare(
                "UPDATE thirteen_f_reports SET predecessor_report_id=?, predecessor_accession=? WHERE id=?"
            ).bind(local_rows[0]["id"], local_rows[0]["accession_number"], report_id).run()
        else:
            candidate = await _manager_prior_candidate(
                env, manager_cik, filing["accession_number"], expected_prior_period
            )
            if candidate:
                accession = candidate.get("accessionNumber")
                url = _sec_submission_text_url(manager_cik, accession)
                await env.DB.prepare(
                    """
                    INSERT OR IGNORE INTO filings
                    (accession_number, cik, company_name, form_type, filed_at, filing_url, source, processed)
                    VALUES (?, ?, ?, ?, ?, ?, 'historical-13f-predecessor', 0)
                    """
                ).bind(
                    accession, manager_cik, parsed.manager_name,
                    candidate.get("form") or "13F-HR", candidate.get("filingDate") or "1900-01-01", url,
                ).run()
                prior_file = await env.DB.prepare(
                    "SELECT id FROM filings WHERE accession_number=? LIMIT 1"
                ).bind(accession).run()
                prior_rows = _rows(prior_file)
                if prior_rows:
                    prior_filing_id = int(prior_rows[0]["id"])
                    await env.DB.prepare(
                        """
                        INSERT OR IGNORE INTO thirteen_f_reports
                        (filing_id, parent_report_id, is_historical)
                        VALUES (?, ?, 1)
                        """
                    ).bind(prior_filing_id, report_id).run()
                    prior_report = await env.DB.prepare(
                        "SELECT id, status FROM thirteen_f_reports WHERE filing_id=? LIMIT 1"
                    ).bind(prior_filing_id).run()
                    pr = _rows(prior_report)
                    if pr:
                        prior_report_id = int(pr[0]["id"])
                        await env.DB.prepare(
                            """
                            UPDATE thirteen_f_reports
                            SET predecessor_report_id=?, predecessor_accession=?, status='waiting_predecessor'
                            WHERE id=?
                            """
                        ).bind(prior_report_id, accession, report_id).run()
                        if pr[0].get("status") != "done":
                            await _enqueue_work(env, prior_report_id, "prepare", "prepare")
                            return

    report_type = (parsed.report_type or "").upper()
    attachment_name = information_table_filename(text)
    source_url = None
    if attachment_name:
        source_url = canonical_url.rsplit("/", 1)[0] + "/" + attachment_name
    elif re.search(r"<(?:\w+:)?infoTable\b", text, re.I):
        source_url = canonical_url
    elif report_type == "13F NOTICE":
        await env.DB.prepare(
            "UPDATE thirteen_f_reports SET status='ignored', processed_at=CURRENT_TIMESTAMP, last_error='13F notice: no holdings reported by this manager' WHERE id=?"
        ).bind(report_id).run()
        await env.DB.prepare("UPDATE filings SET processed=1 WHERE id=?").bind(filing_id).run()
        return
    else:
        source_url = await _fallback_information_table_url(env, canonical_url)

    if not source_url:
        await env.DB.prepare(
            "UPDATE thirteen_f_reports SET status='error', processed_at=CURRENT_TIMESTAMP, last_error='No 13F information table source found' WHERE id=?"
        ).bind(report_id).run()
        await env.DB.prepare("UPDATE filings SET processed=1 WHERE id=?").bind(filing_id).run()
        return

    # A fresh prepare starts a new durable scan chain. Staging rows are safe to
    # clear here because a report cannot reach prepare again after scan work has
    # completed unless it is explicitly rebuilt by a migration.
    await env.DB.prepare(
        "DELETE FROM thirteen_f_raw_positions_stage WHERE report_id=?"
    ).bind(report_id).run()
    await env.DB.prepare(
        "DELETE FROM thirteen_f_work_items WHERE report_id=? AND stage IN ('scan','assemble','chunk','finalize') AND status<>'done'"
    ).bind(report_id).run()
    await env.DB.prepare(
        "UPDATE thirteen_f_reports SET status='scanning', chunks_expected=0, chunks_done=0 WHERE id=?"
    ).bind(report_id).run()
    await _create_work_only(
        env, report_id, "scan:000000", "scan",
        {"source_url": source_url, "cursor": 0, "block_ordinal": 0},
    )


async def _stage_raw_13f_rows(env, report_id: int, rows: list[dict]):
    """Persist one bounded raw-row batch idempotently."""
    if not rows:
        return
    for start in range(0, len(rows), RAW_STAGE_INSERT_BATCH):
        batch = rows[start:start + RAW_STAGE_INSERT_BATCH]
        values = []
        params = []
        for row in batch:
            values.append("(?, ?, ?, ?, ?, ?, ?, ?)")
            params.extend([
                report_id, int(row.get("row_ordinal") or 0), row.get("issuer_name"),
                row.get("title_of_class"), row.get("cusip"), float(row.get("shares") or 0),
                float(row.get("value_dollars") or 0), row.get("put_call"),
            ])
        await env.DB.prepare(
            f"""
            INSERT INTO thirteen_f_raw_positions_stage
            (report_id, row_ordinal, issuer_name, title_of_class, cusip, shares, value_dollars, put_call)
            VALUES {','.join(values)}
            ON CONFLICT(report_id, row_ordinal) DO UPDATE SET
                issuer_name=excluded.issuer_name,
                title_of_class=excluded.title_of_class,
                cusip=excluded.cusip,
                shares=excluded.shares,
                value_dollars=excluded.value_dollars,
                put_call=excluded.put_call
            """
        ).bind(*params).run()


async def _scan_report(env, work: dict, report: dict, filing: dict):
    """Scan at most RAW_SCAN_BATCH infoTable blocks in one invocation."""
    payload = json.loads(work.get("payload_json") or "{}")
    source_url = payload.get("source_url")
    cursor = int(payload.get("cursor") or 0)
    block_ordinal = int(payload.get("block_ordinal") or 0)
    if not source_url:
        raise RuntimeError("13F scan work is missing source_url")

    text = await sec_fetch_text(source_url, user_agent=_env_value(env, "SEC_USER_AGENT"))
    if len(text) > MAX_CANONICAL_SUBMISSION_CHARS:
        raise RuntimeError(f"13F information table exceeds CPU-safe size cap ({len(text)} chars)")

    rows, next_cursor, next_ordinal, exhausted = parse_13f_information_table_batch(
        text, cursor=cursor, block_ordinal=block_ordinal, limit=RAW_SCAN_BATCH
    )
    await _stage_raw_13f_rows(env, int(report["id"]), rows)

    if exhausted:
        await _create_work_only(env, int(report["id"]), "assemble", "assemble")
    else:
        await _create_work_only(
            env, int(report["id"]), f"scan:{next_ordinal:06d}", "scan",
            {"source_url": source_url, "cursor": next_cursor, "block_ordinal": next_ordinal},
        )


async def _assemble_report(env, work: dict, report: dict, filing: dict):
    """Let D1 aggregate raw rows, then create the existing 5-position chunks."""
    report_id = int(report["id"])
    filing_id = int(report["filing_id"])

    summary = await env.DB.prepare(
        """
        WITH aggregated AS (
            SELECT cusip, SUM(shares) AS shares, SUM(value_dollars) AS value_dollars
            FROM thirteen_f_raw_positions_stage
            WHERE report_id=? AND put_call IS NULL
            GROUP BY cusip
        )
        SELECT
            (SELECT COUNT(*) FROM thirteen_f_raw_positions_stage WHERE report_id=?) AS raw_rows,
            (SELECT COUNT(*) FROM thirteen_f_raw_positions_stage
             WHERE report_id=? AND put_call IN ('PUT','CALL')) AS option_rows,
            COUNT(*) AS total_positions,
            SUM(value_dollars) AS raw_portfolio_value,
            SUM(CASE WHEN shares > 0 AND value_dollars > 0 THEN 1 ELSE 0 END) AS price_samples,
            SUM(CASE WHEN shares > 0 AND value_dollars > 0
                       AND (value_dollars / shares) < 2.0 THEN 1 ELSE 0 END) AS low_price_samples,
            AVG(CASE WHEN value_dollars > 0 THEN value_dollars END) AS average_raw_position_value
        FROM aggregated
        """
    ).bind(report_id, report_id, report_id).run()
    sr = _rows(summary)
    state = sr[0] if sr else {}
    raw_rows = int(state.get("raw_rows") or 0)
    option_rows = int(state.get("option_rows") or 0)
    total_positions = int(state.get("total_positions") or 0)
    raw_portfolio_value = float(state.get("raw_portfolio_value") or 0)
    value_scale, value_unit_status = infer_13f_value_scale(
        int(state.get("price_samples") or 0),
        int(state.get("low_price_samples") or 0),
        float(state.get("average_raw_position_value") or 0),
    )
    portfolio_value = raw_portfolio_value * value_scale

    if raw_rows <= 0:
        await env.DB.prepare(
            "UPDATE thirteen_f_reports SET status='error', processed_at=CURRENT_TIMESTAMP, last_error='No parsable 13F information-table rows found' WHERE id=?"
        ).bind(report_id).run()
        await env.DB.prepare("UPDATE filings SET processed=1 WHERE id=?").bind(filing_id).run()
        await env.DB.prepare("DELETE FROM thirteen_f_raw_positions_stage WHERE report_id=?").bind(report_id).run()
        return

    if total_positions <= 0:
        await env.DB.prepare(
            """
            UPDATE thirteen_f_reports
            SET status='ignored', processed_at=CURRENT_TIMESTAMP, raw_position_rows=?, option_rows=?,
                total_positions=0, selected_positions=0, portfolio_value_dollars=0,
                value_scale_factor=?, value_unit_status=?,
                last_error='13F contains no eligible long-equity positions'
            WHERE id=?
            """
        ).bind(raw_rows, option_rows, value_scale, value_unit_status, report_id).run()
        await env.DB.prepare("UPDATE filings SET processed=1 WHERE id=?").bind(filing_id).run()
        await env.DB.prepare("DELETE FROM thirteen_f_raw_positions_stage WHERE report_id=?").bind(report_id).run()
        return

    top = await env.DB.prepare(
        """
        SELECT
            cusip,
            MAX(issuer_name) AS issuer_name,
            MAX(title_of_class) AS title_of_class,
            SUM(shares) AS shares,
            SUM(value_dollars) * ? AS value_dollars,
            COUNT(*) AS source_rows
        FROM thirteen_f_raw_positions_stage
        WHERE report_id=? AND put_call IS NULL
        GROUP BY cusip
        ORDER BY SUM(value_dollars) DESC, cusip
        LIMIT ?
        """
    ).bind(value_scale, report_id, MAX_POSITIONS).run()

    positions = []
    for row in _rows(top):
        positions.append(ThirteenFPosition(
            issuer_name=row.get("issuer_name") or "",
            cusip=(row.get("cusip") or "").upper(),
            value_dollars=float(row.get("value_dollars") or 0),
            shares=float(row.get("shares") or 0),
            title_of_class=row.get("title_of_class"),
            put_call=None,
            source_rows=int(row.get("source_rows") or 1),
        ))

    await env.DB.prepare(
        """
        UPDATE thirteen_f_reports
        SET raw_position_rows=?, option_rows=?, total_positions=?, selected_positions=?,
            portfolio_value_dollars=?, value_scale_factor=?, value_unit_status=?,
            status='assembling', last_error=NULL
        WHERE id=?
        """
    ).bind(
        raw_rows, option_rows, total_positions, len(positions), portfolio_value,
        value_scale, value_unit_status, report_id
    ).run()

    total_value = portfolio_value or 1.0
    chunks = [positions[i:i + CHUNK_SIZE] for i in range(0, len(positions), CHUNK_SIZE)]
    done_count = await _bulk_create_chunk_work(env, report_id, chunks, total_value)
    if done_count >= len(chunks):
        await _create_work_only(env, report_id, "finalize", "finalize")


async def _advance_preprocess_chain(env, report_id: int) -> bool:
    """Publish exactly one next scan/assemble/chunk root after durable completion."""
    result = await env.DB.prepare(
        """
        SELECT id, stage
        FROM thirteen_f_work_items
        WHERE report_id=? AND status='pending' AND enqueued_at IS NULL
          AND stage IN ('scan','assemble')
        ORDER BY CASE stage WHEN 'scan' THEN 0 ELSE 1 END, work_key, id
        LIMIT 1
        """
    ).bind(report_id).run()
    rows = _rows(result)
    if rows:
        return await _publish_work_id(env, int(rows[0]["id"]))
    if await _enqueue_first_incomplete_chunk(env, report_id):
        return True
    finalize = await env.DB.prepare(
        "SELECT id FROM thirteen_f_work_items WHERE report_id=? AND stage='finalize' AND status='pending' AND enqueued_at IS NULL ORDER BY id LIMIT 1"
    ).bind(report_id).run()
    final_rows = _rows(finalize)
    if final_rows:
        return await _publish_work_id(env, int(final_rows[0]["id"]))
    return False


def _suspect_stock_split(
    current_shares: float,
    previous_shares: float | None,
    current_value: float,
    previous_value: float | None,
) -> bool:
    """Conservatively identify split-like share-count jumps from 13F snapshots.

    A forward split multiplies shares while reducing the per-share price by the
    same approximate factor. We require both an integer-like share ratio and an
    inverse per-share-value relationship. This intentionally favors false
    negatives over suppressing genuine manager accumulation.
    """
    if not previous_shares or previous_shares <= 0 or current_shares <= 0:
        return False
    if not previous_value or previous_value <= 0 or current_value <= 0:
        return False

    share_ratio = current_shares / previous_shares
    current_unit_value = current_value / current_shares
    previous_unit_value = previous_value / previous_shares
    if previous_unit_value <= 0:
        return False
    unit_value_ratio = current_unit_value / previous_unit_value

    for split_ratio in (2.0, 3.0, 4.0, 5.0, 10.0, 20.0):
        if abs((share_ratio / split_ratio) - 1.0) <= 0.06:
            normalized_price_move = unit_value_ratio * split_ratio
            if 0.60 <= normalized_price_move <= 1.60:
                return True
    return False


def _institutional_comparison(
    *,
    shares: float,
    value_dollars: float,
    weight: float,
    previous_shares: float | None,
    previous_value_dollars: float | None,
    is_historical: bool,
    has_predecessor: bool,
    predecessor_complete: bool,
    is_fund: bool,
) -> dict:
    """Return a conservative, auditable 13F comparison decision."""
    change_pct = None
    if previous_shares is not None and previous_shares > 0:
        change_pct = round(((shares - previous_shares) / previous_shares) * 100.0, 4)

    if is_historical:
        return {
            "score": 0.0,
            "is_new": 0,
            "change_pct": change_pct,
            "comparison_status": "historical_baseline",
            "corporate_action_suspected": 0,
        }
    if not has_predecessor:
        return {
            "score": 0.0,
            "is_new": 0,
            "change_pct": None,
            "comparison_status": "no_predecessor",
            "corporate_action_suspected": 0,
        }
    if is_fund:
        return {
            "score": 0.0,
            "is_new": 0,
            "change_pct": change_pct,
            "comparison_status": "fund_excluded",
            "corporate_action_suspected": 0,
        }

    if previous_shares is None or previous_shares <= 0:
        if not predecessor_complete:
            return {
                "score": 0.0,
                "is_new": 0,
                "change_pct": None,
                "comparison_status": "unknown_predecessor_truncated",
                "corporate_action_suspected": 0,
            }
        return {
            "score": score_13f_position(shares, None, value_dollars, weight),
            "is_new": 1,
            "change_pct": None,
            "comparison_status": "new_position",
            "corporate_action_suspected": 0,
        }

    if _suspect_stock_split(
        shares, previous_shares, value_dollars, previous_value_dollars
    ):
        return {
            "score": 0.0,
            "is_new": 0,
            "change_pct": change_pct,
            "comparison_status": "corporate_action_suspected",
            "corporate_action_suspected": 1,
        }

    return {
        "score": score_13f_position(
            shares, previous_shares, value_dollars, weight
        ),
        "is_new": 0,
        "change_pct": change_pct,
        "comparison_status": "existing_position",
        "corporate_action_suspected": 0,
    }


async def _prior_snapshot_for_chunk(
    env, predecessor_report_id: int | None, cusips: list[str]
) -> tuple[dict[str, dict], bool]:
    """Load exact-quarter predecessor rows plus coverage completeness.

    If the predecessor contained more eligible CUSIPs than we retained, an
    absent CUSIP is unknown rather than a confirmed new position.
    """
    if not predecessor_report_id:
        return {}, False

    clean_cusips = sorted({str(c or "").upper() for c in cusips if c})
    if clean_cusips:
        placeholders = ",".join("?" for _ in clean_cusips)
        sql = f"""
            SELECT r.total_positions, r.selected_positions,
                   ip.cusip, ip.shares, ip.value_dollars
            FROM thirteen_f_reports r
            LEFT JOIN institutional_positions ip
              ON ip.filing_id = r.filing_id
             AND ip.cusip IN ({placeholders})
            WHERE r.id = ?
        """
        result = await env.DB.prepare(sql).bind(*clean_cusips, predecessor_report_id).run()
    else:
        result = await env.DB.prepare(
            "SELECT total_positions, selected_positions, NULL AS cusip, NULL AS shares, NULL AS value_dollars FROM thirteen_f_reports WHERE id=?"
        ).bind(predecessor_report_id).run()

    rows = _rows(result)
    if not rows:
        return {}, False
    first = rows[0]
    total = int(first.get("total_positions") or 0)
    selected = int(first.get("selected_positions") or 0)
    complete = total > 0 and selected >= total
    prior = {}
    for row in rows:
        cusip = str(row.get("cusip") or "").upper()
        if not cusip:
            continue
        prior[cusip] = {
            "shares": float(row.get("shares") or 0),
            "value_dollars": float(row.get("value_dollars") or 0),
        }
    return prior, complete


async def _prior_shares_for_chunk(env, predecessor_report_id: int | None, cusips: list[str]) -> dict[str, float]:
    """Backward-compatible helper retained for earlier tests/tools."""
    prior, _ = await _prior_snapshot_for_chunk(env, predecessor_report_id, cusips)
    return {cusip: values["shares"] for cusip, values in prior.items()}


async def _process_chunk(env, work: dict, report: dict, filing: dict):
    payload = json.loads(work.get("payload_json") or "{}")
    positions = payload.get("positions") or []
    total_value = float(payload.get("total_value") or 1.0)
    manager_cik = report.get("manager_cik")
    period = report.get("period_of_report")
    predecessor_report_id = report.get("predecessor_report_id")
    prior, predecessor_complete = await _prior_snapshot_for_chunk(
        env, predecessor_report_id, [p.get("cusip") for p in positions if p.get("cusip")]
    )
    identities = await _resolve_security_identities(env, positions)
    affected_ciks: set[str] = set()
    master_updates: list[dict] = []

    for pos in positions:
        cusip = (pos.get("cusip") or "").upper()
        issuer_name = pos.get("issuer_name") or ""
        matched = identities.get(cusip or issuer_name) or {}
        issuer_cik = matched.get("cik")
        ticker = matched.get("ticker")
        mapping_method = matched.get("mapping_method") or "unresolved"
        mapping_confidence = float(matched.get("mapping_confidence") or 0)
        mapping_status = matched.get("mapping_status") or "unresolved"
        normalized_issuer_name = matched.get("normalized_issuer_name") or normalize_security_name(issuer_name)
        shares = float(pos.get("shares") or 0)
        value_dollars = float(pos.get("value_dollars") or 0)
        value_thousands = value_dollars / 1000.0
        prior_row = prior.get(cusip) or {}
        previous_shares = prior_row.get("shares")
        previous_value_dollars = prior_row.get("value_dollars")
        weight = round((value_dollars / total_value) * 100.0, 4) if total_value else 0.0
        is_historical = int(report.get("is_historical") or 0) == 1
        has_predecessor = bool(predecessor_report_id)
        is_fund = _looks_like_fund(issuer_name, pos.get("title_of_class"))
        comparison = _institutional_comparison(
            shares=shares,
            value_dollars=value_dollars,
            weight=weight,
            previous_shares=previous_shares,
            previous_value_dollars=previous_value_dollars,
            is_historical=is_historical,
            has_predecessor=has_predecessor,
            predecessor_complete=predecessor_complete,
            is_fund=is_fund,
        )

        try:
            await env.DB.prepare(
                """
                INSERT INTO institutional_positions
                (filing_id, manager_cik, manager_name, filing_date, period_of_report,
                 issuer_cik, ticker, issuer_name, normalized_issuer_name, cusip, title_of_class, shares,
                 value_thousands, value_dollars, position_weight_pct, previous_shares,
                 previous_value_dollars, share_change_pct, is_new_position, position_score,
                 comparison_status, predecessor_complete, corporate_action_suspected,
                 issuer_mapping_method, issuer_mapping_confidence, issuer_mapping_status,
                 issuer_mapping_version, issuer_mapping_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(filing_id, cusip) DO UPDATE SET
                    issuer_cik=excluded.issuer_cik, ticker=excluded.ticker,
                    issuer_name=excluded.issuer_name, normalized_issuer_name=excluded.normalized_issuer_name,
                    title_of_class=excluded.title_of_class, shares=excluded.shares,
                    value_thousands=excluded.value_thousands, value_dollars=excluded.value_dollars,
                    position_weight_pct=excluded.position_weight_pct,
                    previous_shares=excluded.previous_shares, previous_value_dollars=excluded.previous_value_dollars,
                    share_change_pct=excluded.share_change_pct, is_new_position=excluded.is_new_position,
                    position_score=excluded.position_score, comparison_status=excluded.comparison_status,
                    predecessor_complete=excluded.predecessor_complete,
                    corporate_action_suspected=excluded.corporate_action_suspected,
                    issuer_mapping_method=excluded.issuer_mapping_method,
                    issuer_mapping_confidence=excluded.issuer_mapping_confidence,
                    issuer_mapping_status=excluded.issuer_mapping_status,
                    issuer_mapping_version=excluded.issuer_mapping_version,
                    issuer_mapping_updated_at=CURRENT_TIMESTAMP
                """
            ).bind(
                int(report["filing_id"]), manager_cik, report.get("manager_name"), report.get("filing_date"),
                period, issuer_cik, ticker, issuer_name, normalized_issuer_name, cusip, pos.get("title_of_class"),
                shares, value_thousands, value_dollars, weight, previous_shares,
                previous_value_dollars, comparison["change_pct"], comparison["is_new"], comparison["score"],
                comparison["comparison_status"], 1 if predecessor_complete else 0,
                comparison["corporate_action_suspected"], mapping_method, mapping_confidence,
                mapping_status, SECURITY_MASTER_VERSION,
            ).run()
        except Exception:
            # Safe deployment ordering: continue to support schema 0025 until 0026 is applied.
            await env.DB.prepare(
                """
                INSERT INTO institutional_positions
                (filing_id, manager_cik, manager_name, filing_date, period_of_report,
                 issuer_cik, ticker, issuer_name, cusip, title_of_class, shares,
                 value_thousands, value_dollars, position_weight_pct, previous_shares,
                 previous_value_dollars, share_change_pct, is_new_position, position_score,
                 comparison_status, predecessor_complete, corporate_action_suspected)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filing_id, cusip) DO UPDATE SET
                    issuer_cik=excluded.issuer_cik, ticker=excluded.ticker, issuer_name=excluded.issuer_name,
                    title_of_class=excluded.title_of_class, shares=excluded.shares,
                    value_thousands=excluded.value_thousands, value_dollars=excluded.value_dollars,
                    position_weight_pct=excluded.position_weight_pct, previous_shares=excluded.previous_shares,
                    previous_value_dollars=excluded.previous_value_dollars, share_change_pct=excluded.share_change_pct,
                    is_new_position=excluded.is_new_position, position_score=excluded.position_score,
                    comparison_status=excluded.comparison_status, predecessor_complete=excluded.predecessor_complete,
                    corporate_action_suspected=excluded.corporate_action_suspected
                """
            ).bind(
                int(report["filing_id"]), manager_cik, report.get("manager_name"), report.get("filing_date"),
                period, issuer_cik, ticker, issuer_name, cusip, pos.get("title_of_class"), shares,
                value_thousands, value_dollars, weight, previous_shares, previous_value_dollars,
                comparison["change_pct"], comparison["is_new"], comparison["score"],
                comparison["comparison_status"], 1 if predecessor_complete else 0,
                comparison["corporate_action_suspected"],
            ).run()

        master_updates.append({
            "cusip": cusip, "ticker": ticker, "issuer_cik": issuer_cik,
            "issuer_name": issuer_name, "normalized_issuer_name": normalized_issuer_name,
            "mapping_method": mapping_method, "mapping_confidence": mapping_confidence,
            "mapping_status": mapping_status,
        })

        if issuer_cik and not is_historical and mapping_status == "resolved":
            affected_ciks.add(str(issuer_cik))

    await _persist_security_master_updates(env, master_updates)

    # Recompute at most CHUNK_SIZE issuers per invocation. This keeps 13F HCS
    # refresh bounded while ensuring both new positive confirmation and later
    # reductions/removals are reflected without waiting for another SEC form.
    for issuer_cik in sorted(affected_ciks):
        await recompute_today_score(env, issuer_cik)


async def _after_chunk_done(env, report_id: int, work_key: str):
    """Advance one serial chunk chain after the current work row is durable.

    chunks_done is derived from completed work rows instead of incremented in
    place, so a retry cannot double-count a chunk after a partial failure.
    """
    status = await env.DB.prepare(
        """
        SELECT r.chunks_expected,
               (SELECT COUNT(*) FROM thirteen_f_work_items w
                WHERE w.report_id=r.id AND w.stage='chunk' AND w.status='done') AS done_count
        FROM thirteen_f_reports r
        WHERE r.id=?
        LIMIT 1
        """
    ).bind(report_id).run()
    rows = _rows(status)
    if not rows:
        return

    chunks_done = int(rows[0].get("done_count") or 0)
    chunks_expected = int(rows[0].get("chunks_expected") or 0)
    await env.DB.prepare(
        "UPDATE thirteen_f_reports SET chunks_done=? WHERE id=?"
    ).bind(chunks_done, report_id).run()

    if chunks_done < chunks_expected:
        await _enqueue_next_chunk_after(env, report_id, work_key)
        return


    # Finalization gets its own tiny invocation. This avoids doing report
    # completion, predecessor wake-up, and HCS work on the last chunk's CPU.
    await _enqueue_work(env, report_id, "finalize", "finalize")


async def _wake_waiting_successors(env, completed_report_id: int) -> int:
    """Wake direct quarter successors that were blocked on this report.

    A discovered 13F can depend on either a historical backfill report or another
    normally discovered report. v0.6.6 only woke the historical parent path,
    which stranded ordinary quarter chains after their predecessor completed.
    The explicit predecessor_report_id is the dependency graph, so waking direct
    dependents is deterministic and naturally advances one quarter at a time.
    """
    result = await env.DB.prepare(
        """
        SELECT id
        FROM thirteen_f_reports
        WHERE status='waiting_predecessor'
          AND predecessor_report_id=?
        ORDER BY id
        """
    ).bind(completed_report_id).run()

    woke = 0
    for row in _rows(result):
        successor_id = int(row["id"])
        changed = await env.DB.prepare(
            """
            UPDATE thirteen_f_reports
            SET status='pending', started_at=NULL, last_error=NULL
            WHERE id=? AND status='waiting_predecessor' AND predecessor_report_id=?
            """
        ).bind(successor_id, completed_report_id).run()
        if _result_changes(changed) == 0:
            continue
        await _enqueue_work(
            env, successor_id, f"prepare_after_prior:{completed_report_id}", "prepare"
        )
        woke += 1
    return woke


async def _finalize_report(env, work: dict, report: dict, filing: dict):
    status = await env.DB.prepare(
        "SELECT chunks_done, chunks_expected FROM thirteen_f_reports WHERE id=? LIMIT 1"
    ).bind(int(report["id"])).run()
    rows = _rows(status)
    if not rows:
        return
    state = rows[0]
    if int(state.get("chunks_done") or 0) < int(state.get("chunks_expected") or 0):
        raise RuntimeError("13F finalize reached before all chunks completed")

    await env.DB.prepare(
        "UPDATE thirteen_f_reports SET status='done', processed_at=CURRENT_TIMESTAMP, started_at=NULL WHERE id=?"
    ).bind(int(report["id"])).run()
    await env.DB.prepare("UPDATE filings SET processed=1 WHERE id=?").bind(int(report["filing_id"])).run()
    await env.DB.prepare(
        "DELETE FROM thirteen_f_raw_positions_stage WHERE report_id=?"
    ).bind(int(report["id"])).run()

    # v0.6.7: every completed report wakes reports that explicitly depend on it,
    # whether the predecessor was a historical backfill or an ordinary filing.
    # This advances chains such as Q1 -> Q2 -> Q3 one quarter at a time.
    await _wake_waiting_successors(env, int(report["id"]))

    # v0.8.0 refreshes affected issuer scores inside each bounded five-position
    # chunk. Finalize therefore remains lightweight and does not fan out score work.


async def handle_13f_work(env, work_id: int):
    joined = await env.DB.prepare(
        """
        SELECT
            w.id AS work_id, w.report_id, w.work_key, w.stage, w.payload_json,
            w.status AS work_status, w.attempts AS work_attempts, w.started_at AS work_started_at,
            r.id, r.filing_id, r.parent_report_id, r.predecessor_report_id,
            r.predecessor_accession, r.is_historical, r.manager_cik, r.manager_name,
            r.filing_date, r.period_of_report, r.status AS report_status,
            f.accession_number, f.cik AS filing_cik, f.company_name, f.form_type,
            f.filed_at, f.filing_url
        FROM thirteen_f_work_items w
        JOIN thirteen_f_reports r ON r.id = w.report_id
        JOIN filings f ON f.id = r.filing_id
        WHERE w.id=?
        LIMIT 1
        """
    ).bind(work_id).run()
    rows = _rows(joined)
    if not rows:
        return "missing"
    row = rows[0]
    if row.get("work_status") in {"done", "ignored", "error"}:
        return "terminal"

    # Old v0.6.5 Queue messages may still arrive after deployment. ACK any
    # chunk that is ahead of an unfinished predecessor and clear its enqueue
    # marker so serial chaining can publish it again at the correct time.
    if row.get("stage") == "chunk" and await _chunk_has_unfinished_predecessor(
        env, int(row["report_id"]), row.get("work_key") or ""
    ):
        await env.DB.prepare(
            "UPDATE thirteen_f_work_items SET enqueued_at=NULL, started_at=NULL WHERE id=? AND status='pending'"
        ).bind(work_id).run()
        return "deferred"

    attempts = int(row.get("work_attempts") or 0)
    if attempts >= MAX_HARD_ATTEMPTS:
        await env.DB.prepare(
            "UPDATE thirteen_f_work_items SET status='error', processed_at=CURRENT_TIMESTAMP, started_at=NULL, last_error=COALESCE(last_error,'13F work retry limit reached') WHERE id=?"
        ).bind(work_id).run()
        await env.DB.prepare(
            "UPDATE thirteen_f_reports SET status='error', processed_at=CURRENT_TIMESTAMP, last_error='13F work retry limit reached' WHERE id=?"
        ).bind(int(row["report_id"])).run()
        await env.DB.prepare("UPDATE filings SET processed=1 WHERE id=?").bind(int(row["filing_id"])).run()
        return "error"

    await env.DB.prepare(
        "UPDATE thirteen_f_work_items SET status='processing', attempts=attempts+1, started_at=CURRENT_TIMESTAMP WHERE id=?"
    ).bind(work_id).run()

    work = {
        "id": work_id,
        "report_id": row["report_id"],
        "work_key": row["work_key"],
        "stage": row["stage"],
        "payload_json": row.get("payload_json"),
    }
    report = {k: row.get(k) for k in (
        "id", "filing_id", "parent_report_id", "predecessor_report_id", "predecessor_accession",
        "is_historical", "manager_cik", "manager_name", "filing_date", "period_of_report"
    )}
    filing = {
        "accession_number": row.get("accession_number"),
        "cik": row.get("filing_cik"),
        "company_name": row.get("company_name"),
        "form_type": row.get("form_type"),
        "filed_at": row.get("filed_at"),
        "filing_url": row.get("filing_url"),
    }

    try:
        if row["stage"] == "prepare":
            await _prepare_report(env, work, report, filing)
        elif row["stage"] == "scan":
            await _scan_report(env, work, report, filing)
        elif row["stage"] == "assemble":
            await _assemble_report(env, work, report, filing)
        elif row["stage"] == "chunk":
            await _process_chunk(env, work, report, filing)
        elif row["stage"] == "finalize":
            await _finalize_report(env, work, report, filing)
        else:
            raise ValueError(f"Unknown 13F stage: {row['stage']}")
    except Exception as exc:
        await env.DB.prepare(
            "UPDATE thirteen_f_work_items SET status='pending', started_at=NULL, enqueued_at=NULL, last_error=? WHERE id=?"
        ).bind(str(exc)[:500], work_id).run()
        raise

    # Make the stage durable before scheduling successor work. If successor
    # publication itself fails, this item remains done and Cron can recover the
    # next eligible pending row without repeating completed data writes.
    await env.DB.prepare(
        "UPDATE thirteen_f_work_items SET status='done', processed_at=CURRENT_TIMESTAMP, started_at=NULL, last_error=NULL WHERE id=?"
    ).bind(work_id).run()

    if row["stage"] in {"prepare", "scan", "assemble"}:
        await _advance_preprocess_chain(env, int(row["report_id"]))
    elif row["stage"] == "chunk":
        await _after_chunk_done(env, int(row["report_id"]), row.get("work_key") or "")

    return "done"
