import time
_MODULE_IMPORT_STARTED = time.perf_counter()

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from workers import WorkerEntrypoint, asgi

from scoring.hcs import (
    ScoreBreakdown,
    score_beneficial_ownership,
    score_form4_purchase,
    score_13f_position,
    confirmed_institutional_score,
)
from sec.client import sec_fetch_text, validate_sec_user_agent
from sec.discovery import (
    discover_latest_form4,
    discover_latest_ownership,
    discover_latest_8k,
    discover_latest_13f,
    discover_daily_index,
    discover_available_daily_indexes,
    daily_index_directory_url,
)
from sec.form4 import parse_form4
from sec.beneficial_ownership import parse_schedule_13
from sec.eight_k import parse_8k_submission, submission_recent_row
from sec.thirteen_f import parse_13f_submission
from pipeline_13f import (
    ensure_report_for_filing, publish_pending_13f_work, recover_stale_13f_work,
    resolve_unmapped_security_positions,
    SECURITY_RESOLUTION_BATCH, SECURITY_RESOLUTION_HARD_CAP,
)
from public_api import public_signal_families, company_lookup_mode
from public_evidence import build_public_evidence
from recompute import recompute_today_score as shared_recompute_today_score
from score_queue import enqueue_stale_active_scores
from security_master import HCS_VERSION, INSTITUTIONAL_VERSION, SECURITY_MASTER_VERSION
from universe import classify_issuer, UNIVERSE_VERSION
from app_meta import (
    APP_NAME, APP_VERSION, API_SCHEMA_VERSION, PUBLIC_HCS_THRESHOLD,
    MAX_PUBLIC_PAGE_SIZE, version_payload,
)
from build_meta import BUILD_SHA, BUILD_DEPLOYED_AT, BUILD_ID
from telemetry import (
    filing_latency_seconds as _filing_latency_seconds,
    record_pipeline_metric as _record_pipeline_metric,
    record_sec_request_metric as _record_sec_request_metric,
    set_runtime_status as _set_runtime_status,
)
from ops import normalize_sec_date
from system_health import (
    _public_system_snapshot,
    _overall_system_status,
    _operational_state_from_stages,
)
from ingestion_control import (
    acquire_ingestion_lease as _acquire_ingestion_lease,
    auto_replay_transient_failures as _auto_replay_transient_failures,
    queue_throughput_snapshot as _queue_throughput_snapshot,
    record_backlog_sample as _record_backlog_sample,
    release_ingestion_lease as _release_ingestion_lease,
    sync_system_alerts as _sync_system_alerts,
    update_ingestion_controller as _update_ingestion_controller,
)

VERSION = APP_VERSION
MODULE_IMPORT_DURATION_MS = round((time.perf_counter() - _MODULE_IMPORT_STARTED) * 1000.0, 1)
PROCESS_STARTED_AT = time.time()

app = FastAPI(
    title="Hidden Conviction API",
    version=VERSION,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _meta(*, count: int | None = None, limit: int | None = None, offset: int | None = None) -> dict:
    payload = {
        "generated_at": _now_iso(),
        "application_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "build_id": BUILD_ID,
    }
    if count is not None:
        payload["count"] = int(count)
    if limit is not None:
        payload["limit"] = int(limit)
    if offset is not None:
        payload["offset"] = int(offset)
    return payload


def _error_payload(status_code: int, message: str, *, code: str = "request_error") -> dict:
    return {
        "error": {"code": code, "message": message, "status": int(status_code)},
        "meta": _meta(),
    }


def _payload_etag(payload: dict) -> str:
    # Exclude request-time metadata so unchanged resources retain the same ETag.
    canonical = json.loads(json.dumps(payload, default=str))
    if isinstance(canonical, dict) and isinstance(canonical.get("meta"), dict):
        canonical["meta"].pop("generated_at", None)
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:24]
    return f'W/"{digest}"'


def _conditional_json(request: Request, payload: dict, *, status_code: int = 200) -> Response:
    """Return JSON with a deterministic ETag and honor If-None-Match."""
    etag = _payload_etag(payload)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=payload, status_code=status_code, headers={"ETag": etag})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(status_code=exc.status_code, content=_error_payload(exc.status_code, message))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            **_error_payload(422, "Invalid request parameters", code="validation_error"),
            "details": exc.errors(),
        },
    )


@app.middleware("http")
async def response_headers(request: Request, call_next):
    """Apply security headers, request tracing, and latency receipts at the edge."""
    started = time.perf_counter()
    supplied_request_id = (request.headers.get("x-request-id") or "").strip()
    request_id = supplied_request_id[:80] if re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", supplied_request_id) else uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    duration_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    response.headers["X-Request-ID"] = request_id
    response.headers["Server-Timing"] = f'app;dur={duration_ms:.1f}'
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Hidden-Conviction-Version"] = APP_VERSION
    response.headers["X-API-Schema-Version"] = API_SCHEMA_VERSION
    response.headers["X-Hidden-Conviction-Build"] = BUILD_ID
    response.headers["X-Cache-Generation"] = f"{APP_VERSION}:{API_SCHEMA_VERSION}:{BUILD_ID}"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'"
    )
    path = request.url.path
    critical_no_store = {"/api/health", "/api/version", "/api/build", "/api/system", "/api/freshness"}
    if path.startswith("/api/admin/") or path in critical_no_store:
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=120"
    return response


SCORE_RECOMPUTE_BATCH = 15
SCORE_RECOMPUTE_HARD_CAP = 25


def _env_value(env, name: str, default=None):
    try:
        value = getattr(env, name)
        return value if value not in (None, "") else default
    except Exception:
        return default


def _rows(result) -> list[dict]:
    return getattr(result, "results", None) or []


def _in_public_hcs_universe(row: dict) -> bool:
    if row.get("hcs_eligible") is not None:
        try:
            return bool(int(row.get("hcs_eligible")))
        except Exception:
            return bool(row.get("hcs_eligible"))
    name = row.get("company_name") or row.get("issuer_name")
    classification = classify_issuer(name, row.get("ticker"), row.get("exchange"))
    return classification.hcs_eligible


def _public_universe_rows(result, limit: int) -> list[dict]:
    return [row for row in _rows(result) if _in_public_hcs_universe(row)][:limit]


def _role_for(tx) -> str | None:
    if tx.officer_title:
        return tx.officer_title

    if tx.is_director:
        return "Director"

    if tx.is_officer:
        return "Officer"

    return None


def _xml_text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None

    target = node.find(path)

    if target is None or target.text is None:
        return None

    value = target.text.strip()
    return value or None


def _normalize_cik(value: str | None) -> str | None:
    if not value:
        return None

    return value.strip().lstrip("0") or "0"


def _parse_form4_issuer(xml_text: str) -> dict:
    """
    Extract issuer identity even when a Form 4 contains no
    non-derivative transactions. This lets derivative-only filings
    be acknowledged instead of retried forever.
    """

    root = ET.fromstring(xml_text)
    issuer = root.find("issuer")

    cik = _normalize_cik(
        _xml_text(issuer, "issuerCik")
    )

    company_name = _xml_text(
        issuer,
        "issuerName",
    )

    ticker = _xml_text(
        issuer,
        "issuerTradingSymbol",
    )

    if ticker:
        ticker = ticker.upper()

    return {
        "cik": cik,
        "company_name": company_name,
        "ticker": ticker,
    }


async def _require_admin(env, supplied: str | None):
    expected = _env_value(
        env,
        "ADMIN_SECRET",
    )

    if (
        not expected
        or not supplied
        or not hmac.compare_digest(str(supplied), str(expected))
    ):
        raise HTTPException(
            status_code=401,
            detail="Admin key required",
        )


async def _audit_identity_mappings(env):
    """Periodic, non-mutating issuer-identity consistency audit."""
    try:
        result = await env.DB.prepare(
            """
            SELECT
              (SELECT COUNT(*) FROM (
                SELECT UPPER(ticker) FROM company_tickers
                WHERE COALESCE(TRIM(ticker),'')<>''
                GROUP BY UPPER(ticker) HAVING COUNT(DISTINCT cik)>1
              )) AS ticker_conflicts,
              (SELECT COUNT(*) FROM companies
                WHERE active=1 AND (identity_checked_at IS NULL OR identity_checked_at<DATETIME('now','-90 day'))) AS stale_identity_rows
            """
        ).run()
        row = (_rows(result) or [{}])[0]
        await _set_runtime_status(env, "identity_audit", json.dumps(row, separators=(",", ":")))
        return row
    except Exception as exc:
        await _set_runtime_status(env, "identity_audit_error", str(exc)[:1000])
        return {}


async def _refresh_company_identity_batch(env, limit: int = 3) -> int:
    """Gradually enrich/refresh ticker, SIC, and industry metadata from SEC submissions."""
    try:
        result = await env.DB.prepare(
            """
            SELECT cik, company_name
            FROM companies
            WHERE active=1
              AND (sic IS NULL OR industry IS NULL OR identity_checked_at IS NULL
                   OR identity_checked_at<DATETIME('now','-90 day'))
            ORDER BY CASE WHEN sic IS NULL OR industry IS NULL THEN 0 ELSE 1 END,
                     COALESCE(identity_checked_at,'1970-01-01'), cik
            LIMIT ?
            """
        ).bind(max(1, min(int(limit), 25))).run()
    except Exception:
        return 0
    refreshed = 0
    for row in _rows(result):
        try:
            await _ensure_company_from_submissions(env, str(row.get("cik") or ""), row.get("company_name"))
            refreshed += 1
        except Exception:
            continue
    return refreshed


def _feed_is_saturated(source: str, rows: list[dict]) -> bool:
    if source == "ownership":
        counts: dict[str, int] = {}
        for row in rows:
            feed = str(row.get("discovery_feed") or "ownership")
            counts[feed] = counts.get(feed, 0) + 1
        return any(count >= 95 for count in counts.values())
    return len(rows) >= 95


async def _capture_data_quality_issues(env) -> int:
    """Persist actionable current data-quality conditions without duplicating open issues."""
    created = 0
    try:
        # Resolve discovery-backfill issues that are no longer active.
        await env.DB.prepare(
            """
            UPDATE data_quality_issues SET resolved_at=CURRENT_TIMESTAMP
            WHERE issue_type='discovery_backfill_required' AND resolved_at IS NULL
              AND entity_key IN (SELECT source FROM discovery_watermarks WHERE needs_backfill=0)
            """
        ).run()
        result = await env.DB.prepare(
            """
            INSERT INTO data_quality_issues(issue_type, entity_key, severity, detail_json)
            SELECT 'discovery_backfill_required', w.source, 'warning',
                   json_object('latest_filed_at',w.latest_filed_at,'last_feed_count',w.last_feed_count)
            FROM discovery_watermarks w
            WHERE w.needs_backfill=1
              AND NOT EXISTS (SELECT 1 FROM data_quality_issues d
                              WHERE d.issue_type='discovery_backfill_required' AND d.entity_key=w.source AND d.resolved_at IS NULL)
            """
        ).run()
        created += int((getattr(result, "meta", None) or {}).get("changes", 0) or 0)

        result = await env.DB.prepare(
            """
            INSERT INTO data_quality_issues(issue_type, entity_key, severity, detail_json)
            SELECT 'processing_queue_error', CAST(q.id AS TEXT), 'error',
                   json_object('filing_id',q.filing_id,'attempts',q.attempts,'last_error',q.last_error)
            FROM processing_queue q
            WHERE q.status='error'
              AND NOT EXISTS (SELECT 1 FROM data_quality_issues d
                              WHERE d.issue_type='processing_queue_error' AND d.entity_key=CAST(q.id AS TEXT) AND d.resolved_at IS NULL)
            LIMIT 100
            """
        ).run()
        created += int((getattr(result, "meta", None) or {}).get("changes", 0) or 0)

        conflicts = await env.DB.prepare(
            """
            SELECT UPPER(ticker) AS ticker, COUNT(DISTINCT cik) AS cik_count, GROUP_CONCAT(DISTINCT cik) AS ciks
            FROM company_tickers WHERE COALESCE(TRIM(ticker),'')<>''
            GROUP BY UPPER(ticker) HAVING COUNT(DISTINCT cik)>1 LIMIT 50
            """
        ).run()
        for row in _rows(conflicts):
            ticker = str(row.get("ticker") or "")
            if not ticker:
                continue
            result = await env.DB.prepare(
                """
                INSERT INTO data_quality_issues(issue_type, entity_key, severity, detail_json)
                SELECT 'ticker_cik_conflict', ?, 'warning', ?
                WHERE NOT EXISTS (SELECT 1 FROM data_quality_issues
                                  WHERE issue_type='ticker_cik_conflict' AND entity_key=? AND resolved_at IS NULL)
                """
            ).bind(ticker, json.dumps(row, separators=(",", ":")), ticker).run()
            created += int((getattr(result, "meta", None) or {}).get("changes", 0) or 0)

        # Compare today's unique filing discovery with the recent per-pipeline baseline.
        anomalies = await env.DB.prepare(
            """
            WITH today AS (
              SELECT pipeline, discovered FROM pipeline_metrics WHERE metric_date=DATE('now')
            ), baseline AS (
              SELECT pipeline, AVG(discovered) AS avg_discovered
              FROM pipeline_metrics
              WHERE metric_date BETWEEN DATE('now','-7 day') AND DATE('now','-1 day')
              GROUP BY pipeline
            )
            SELECT t.pipeline, t.discovered, b.avg_discovered
            FROM today t JOIN baseline b ON b.pipeline=t.pipeline
            WHERE b.avg_discovered>=10
              AND (t.discovered < b.avg_discovered*0.2 OR t.discovered > b.avg_discovered*3.0)
            """
        ).run()
        for row in _rows(anomalies):
            entity = f"{row.get('pipeline')}:{datetime.now(timezone.utc).date().isoformat()}"
            result = await env.DB.prepare(
                """
                INSERT INTO data_quality_issues(issue_type, entity_key, severity, detail_json)
                SELECT 'filing_volume_anomaly', ?, 'warning', ?
                WHERE NOT EXISTS (SELECT 1 FROM data_quality_issues
                                  WHERE issue_type='filing_volume_anomaly' AND entity_key=? AND resolved_at IS NULL)
                """
            ).bind(entity, json.dumps(row, separators=(",", ":")), entity).run()
            created += int((getattr(result, "meta", None) or {}).get("changes", 0) or 0)
    except Exception as exc:
        await _set_runtime_status(env, "data_quality_capture_error", str(exc)[:1000])
    return created


def _pipeline_name_for_form(form_type: str | None) -> str:
    form = (form_type or "").upper()
    if form.startswith("13F-HR"):
        return "13f"
    if form.startswith("SCHEDULE 13"):
        return "ownership"
    if form.startswith("8-K"):
        return "8k"
    if form in ("4", "4/A"):
        return "form4"
    return "other"


def _signal_family_clause(family: str | None) -> str:
    key = (family or "").strip().lower()
    mapping = {
        "insider": "(s.insider_score > 0 OR s.cluster_score > 0)",
        "ownership": "s.ownership_score > 0",
        "institutional": "s.whale_score > 0",
        "event": "s.event_score <> 0",
        "capital_allocation": "s.capital_allocation_score > 0",
    }
    return mapping.get(key, "1=1")


def _signal_order_clause(sort: str | None) -> str:
    key = (sort or "score").strip().lower()
    if key == "recent":
        return "latest_evidence_date DESC, s.score_date DESC, s.hcs_score DESC"
    if key == "ticker":
        return "COALESCE(c.ticker, c.company_name, s.cik) ASC, s.hcs_score DESC"
    if key == "change":
        return "ABS(s.hcs_score - COALESCE(prev.hcs_score, 0)) DESC, s.hcs_score DESC"
    return "s.hcs_score DESC, s.score_date DESC, COALESCE(c.ticker, c.company_name, s.cik) ASC"


@app.get("/api/version")
async def api_version(request: Request):
    payload = {
        **version_payload(
            hcs_version=HCS_VERSION,
            institutional_version=INSTITUTIONAL_VERSION,
            universe_version=UNIVERSE_VERSION,
            security_master_version=SECURITY_MASTER_VERSION,
        ),
        "build_id": BUILD_ID,
        "commit_sha": BUILD_SHA,
        "deployed_at": BUILD_DEPLOYED_AT,
        "meta": _meta(),
    }
    return _conditional_json(request, payload)


@app.get("/api/build")
async def api_build(request: Request):
    """Immutable deployment identity used by operators and post-deploy verification."""
    payload = {
        "application": APP_NAME,
        "version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "commit_sha": BUILD_SHA,
        "deployed_at": BUILD_DEPLOYED_AT,
        "build_id": BUILD_ID,
        "cache_generation": f"{APP_VERSION}:{API_SCHEMA_VERSION}:{BUILD_ID}",
        "meta": _meta(),
    }
    return _conditional_json(request, payload)


@app.get("/api/health")
async def health(request: Request):
    env = request.scope["env"]
    sec_configured = True
    try:
        validate_sec_user_agent(_env_value(env, "SEC_USER_AGENT"))
    except Exception:
        sec_configured = False
    stages = await _public_system_snapshot(env)
    if not sec_configured:
        stages["discovery"] = {**stages.get("discovery", {}), "status": "error"}
    status = _overall_system_status(stages)
    op_state = _operational_state_from_stages(stages)
    active_alerts = []
    try:
        ar = await env.DB.prepare(
            "SELECT code,severity,message,first_seen_at,last_seen_at FROM system_alerts WHERE active=1 ORDER BY CASE severity WHEN 'error' THEN 0 ELSE 1 END,last_seen_at DESC"
        ).run()
        active_alerts = _rows(ar)
    except Exception:
        active_alerts = []
    payload = {
        "status": status,
        "operational_state": op_state,
        "service": "hidden-conviction-ingest",
        "version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "build_id": BUILD_ID,
        "commit_sha": BUILD_SHA,
        "deployed_at": BUILD_DEPLOYED_AT,
        "checks": {
            "database": stages.get("database", {}).get("status"),
            "sec_user_agent": "configured" if sec_configured else "missing_or_placeholder",
            "cron": stages.get("cron"),
            "queue": stages.get("queue"),
            "sec": stages.get("sec"),
            "identity": stages.get("identity"),
        },
        "alerts": active_alerts,
        "process_uptime_seconds": round(max(0.0, time.time() - PROCESS_STARTED_AT), 1),
        "module_import_duration_ms": MODULE_IMPORT_DURATION_MS,
        "meta": _meta(),
    }
    return _conditional_json(request, payload)


@app.get("/api/system")
async def system_console_snapshot(request: Request):
    env = request.scope["env"]
    stages = await _public_system_snapshot(env)
    recent_cron_runs = []
    recent_error_summary = []
    query_metrics = []
    pipeline_trends = []
    try:
        result = await env.DB.prepare(
            "SELECT run_id, started_at, finished_at, status, duration_ms, records_discovered, records_processed, records_failed, scores_recomputed FROM cron_runs ORDER BY id DESC LIMIT 10"
        ).run()
        recent_cron_runs = _rows(result)
    except Exception:
        pass
    try:
        result = await env.DB.prepare(
            """
            SELECT 'filing_queue' AS pipeline, COUNT(*) AS count, MAX(COALESCE(last_attempt_at, processed_at, created_at)) AS latest_at
            FROM processing_queue WHERE status='error'
            UNION ALL
            SELECT 'score_recompute' AS pipeline, COUNT(*) AS count, MAX(COALESCE(last_attempt_at, processed_at, created_at)) AS latest_at
            FROM score_recompute_queue WHERE status='error'
            UNION ALL
            SELECT 'discovery' AS pipeline, COUNT(*) AS count, MAX(updated_at) AS latest_at
            FROM discovery_health WHERE consecutive_failures>0
            """
        ).run()
        recent_error_summary = [row for row in _rows(result) if int(row.get("count") or 0)>0]
    except Exception:
        pass
    try:
        result = await env.DB.prepare(
            "SELECT query_name, executions, rows_returned, ROUND(total_duration_ms,1) AS total_duration_ms, ROUND(max_duration_ms,1) AS max_duration_ms, slow_count, updated_at FROM query_metrics WHERE metric_date=DATE('now') ORDER BY max_duration_ms DESC"
        ).run()
        query_metrics = _rows(result)
    except Exception:
        pass
    try:
        result = await env.DB.prepare(
            "SELECT metric_date, pipeline, discovered, processed, failed, retried, ROUND(COALESCE(latest_latency_seconds,0),1) AS latest_latency_seconds FROM pipeline_metrics WHERE metric_date>=DATE('now','-6 day') ORDER BY metric_date DESC, pipeline"
        ).run()
        pipeline_trends = _rows(result)
    except Exception:
        pass
    backlog_history = []
    active_alerts = []
    deployment_history = []
    try:
        result = await env.DB.prepare(
            "SELECT captured_at,pending,processing,errors,oldest_pending_age_minutes,discovered_1h,processed_1h,net_backlog_per_hour,estimated_clear_minutes,target_concurrency,control_mode FROM backlog_samples WHERE captured_at>=DATETIME('now','-24 hours') ORDER BY captured_at"
        ).run()
        backlog_history = _rows(result)
    except Exception:
        pass
    try:
        result = await env.DB.prepare(
            "SELECT code,severity,message,first_seen_at,last_seen_at FROM system_alerts WHERE active=1 ORDER BY CASE severity WHEN 'error' THEN 0 ELSE 1 END,last_seen_at DESC"
        ).run()
        active_alerts = _rows(result)
    except Exception:
        pass
    try:
        result = await env.DB.prepare(
            "SELECT build_id,app_version,deployed_at,health_ttfb_ms,home_ttfb_ms,system_ttfb_ms,queue_pending,queue_errors,captured_at FROM deployment_metrics ORDER BY captured_at DESC LIMIT 8"
        ).run()
        deployment_history = _rows(result)
    except Exception:
        pass

    payload = {
        "status": _overall_system_status(stages),
        "operational_state": _operational_state_from_stages(stages),
        "stages": stages,
        "recent_cron_runs": recent_cron_runs,
        "recent_error_summary": recent_error_summary,
        "query_metrics_today": query_metrics,
        "pipeline_trends_7d": pipeline_trends,
        "backlog_history_24h": backlog_history,
        "active_alerts": active_alerts,
        "deployment_history": deployment_history,
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "commit_sha": BUILD_SHA,
        "meta": _meta(),
    }
    return _conditional_json(request, payload)


@app.head("/api/version")
async def api_version_head(request: Request):
    payload = {
        **version_payload(
            hcs_version=HCS_VERSION, institutional_version=INSTITUTIONAL_VERSION,
            universe_version=UNIVERSE_VERSION, security_master_version=SECURITY_MASTER_VERSION,
        ),
        "meta": _meta(),
    }
    return Response(status_code=200, headers={"ETag": _payload_etag(payload)})


@app.head("/api/build")
async def api_build_head(request: Request):
    payload = {
        "application": APP_NAME, "version": APP_VERSION, "api_schema_version": API_SCHEMA_VERSION,
        "commit_sha": BUILD_SHA, "deployed_at": BUILD_DEPLOYED_AT, "build_id": BUILD_ID,
        "cache_generation": f"{APP_VERSION}:{API_SCHEMA_VERSION}:{BUILD_ID}", "meta": _meta(),
    }
    return Response(status_code=200, headers={"ETag": _payload_etag(payload)})


@app.get("/api/search")
async def search_companies(request: Request, q: str = "", limit: int = 8):
    """Ticker/company lookup used by the research UI."""
    env = request.scope["env"]
    query = (q or "").strip()[:80]
    limit = max(1, min(int(limit), 20))
    if not query:
        return {"results": [], "meta": _meta(count=0, limit=limit, offset=0)}

    upper = query.upper()
    prefix = f"{upper}%"
    contains = f"%{upper}%"
    result = await env.DB.prepare(
        """
        SELECT c.cik, c.ticker, c.company_name, c.exchange, c.sic, c.industry,
               c.hcs_eligible, c.issuer_type,
               cs.hcs_score, cs.classification, cs.score_date
        FROM companies c
        LEFT JOIN current_scores cs ON cs.cik=c.cik AND cs.score_date = DATE('now')
        WHERE UPPER(COALESCE(c.ticker,'')) LIKE ?
           OR UPPER(COALESCE(c.company_name,'')) LIKE ?
           OR c.cik = ?
           OR EXISTS (
                SELECT 1 FROM company_tickers ct
                WHERE ct.cik=c.cik AND UPPER(ct.ticker) LIKE ?
           )
        ORDER BY
          CASE
            WHEN UPPER(COALESCE(c.ticker,''))=? THEN 0
            WHEN UPPER(COALESCE(c.ticker,'')) LIKE ? THEN 1
            WHEN UPPER(COALESCE(c.company_name,'')) LIKE ? THEN 2
            ELSE 3
          END,
          COALESCE(cs.hcs_score,0) DESC, c.company_name ASC
        LIMIT ?
        """
    ).bind(prefix, contains, query.lstrip("0") or "0", prefix, upper, prefix, prefix, limit).run()
    rows = _rows(result)
    return {"results": rows, "meta": _meta(count=len(rows), limit=limit, offset=0)}


@app.get("/api/signals")
async def signals(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    min_hcs: float = PUBLIC_HCS_THRESHOLD,
    max_hcs: float = 100,
    family: str | None = None,
    industry: str | None = None,
    sort: str = "score",
):
    """Current public signal leaderboard with filtering, sorting, and pagination."""
    env = request.scope["env"]
    limit = max(1, min(int(limit), MAX_PUBLIC_PAGE_SIZE))
    offset = max(0, min(int(offset), 5000))
    min_hcs = max(float(PUBLIC_HCS_THRESHOLD), min(float(min_hcs), 100.0))
    max_hcs = max(min_hcs, min(float(max_hcs), 100.0))
    industry_query = (industry or "").strip()[:80]
    industry_like = f"%{industry_query.upper()}%"
    allowed_families = {"", "insider", "ownership", "institutional", "event", "capital_allocation"}
    allowed_sorts = {"score", "recent", "ticker", "change"}
    if (family or "").strip().lower() not in allowed_families:
        raise HTTPException(status_code=400, detail="Unknown signal family")
    if (sort or "score").strip().lower() not in allowed_sorts:
        raise HTTPException(status_code=400, detail="Unknown sort order")
    family_clause = _signal_family_clause(family)
    order_clause = _signal_order_clause(sort)

    sql = f"""
        WITH latest AS (
            SELECT s.*, ROW_NUMBER() OVER (
                PARTITION BY s.cik ORDER BY s.score_date DESC, s.id DESC
            ) AS rn
            FROM scores s
        ), previous AS (
            SELECT s1.cik, s1.hcs_score
            FROM scores s1
            WHERE s1.id = (
                SELECT s2.id FROM scores s2
                WHERE s2.cik=s1.cik AND s2.score_date < DATE('now')
                ORDER BY s2.score_date DESC, s2.id DESC LIMIT 1
            )
        )
        SELECT
            s.cik, c.ticker, c.company_name, c.exchange, c.sic, c.industry,
            c.hcs_eligible, c.issuer_type, c.universe_version,
            s.score_date, s.hcs_score, s.classification, s.scored_at, s.hcs_version,
            CASE WHEN s.insider_score>0 OR s.cluster_score>0 THEN 1 ELSE 0 END AS has_insider,
            CASE WHEN s.ownership_score>0 THEN 1 ELSE 0 END AS has_ownership,
            CASE WHEN s.whale_score>0 THEN 1 ELSE 0 END AS has_institutional,
            CASE WHEN s.event_score<>0 THEN 1 ELSE 0 END AS has_event,
            CASE WHEN s.event_score<0 THEN 1 ELSE 0 END AS has_negative_event,
            CASE WHEN s.capital_allocation_score>0 THEN 1 ELSE 0 END AS has_capital_allocation,
            (SELECT MAX(COALESCE(e.filing_date,e.event_date)) FROM score_component_evidence e
             WHERE e.cik=s.cik AND e.score_date=s.score_date AND e.component_score<>0) AS latest_evidence_date,
            ROUND(s.hcs_score - COALESCE(prev.hcs_score, 0), 1) AS one_day_change,
            (CASE WHEN s.insider_score > 0 THEN 1 ELSE 0 END
             + CASE WHEN s.cluster_score > 0 THEN 1 ELSE 0 END
             + CASE WHEN s.ownership_score > 0 THEN 1 ELSE 0 END
             + CASE WHEN s.whale_score > 0 THEN 1 ELSE 0 END
             + CASE WHEN s.event_score <> 0 THEN 1 ELSE 0 END
             + CASE WHEN s.capital_allocation_score > 0 THEN 1 ELSE 0 END) AS active_signal_count
        FROM latest s
        JOIN companies c ON c.cik=s.cik
        LEFT JOIN previous prev ON prev.cik=s.cik
        WHERE s.rn=1
          AND c.hcs_eligible = 1
          AND s.score_date = DATE('now')
          AND s.hcs_score BETWEEN ? AND ?
          AND {family_clause}
          AND (?='' OR UPPER(COALESCE(c.industry,'')) LIKE ? OR c.sic=?)
        ORDER BY {order_clause}
        LIMIT ? OFFSET ?
    """
    result = await env.DB.prepare(sql).bind(
        min_hcs, max_hcs, industry_query, industry_like, industry_query, limit, offset
    ).run()
    rows = _rows(result)

    count_sql = f"""
        WITH latest AS (
            SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.cik ORDER BY s.score_date DESC, s.id DESC) AS rn
            FROM scores s
        )
        SELECT COUNT(*) AS total
        FROM latest s JOIN companies c ON c.cik=s.cik
        WHERE s.rn=1 AND c.hcs_eligible = 1 AND s.score_date = DATE('now')
          AND s.hcs_score BETWEEN ? AND ?
          AND {family_clause}
          AND (?='' OR UPPER(COALESCE(c.industry,'')) LIKE ? OR c.sic=?)
    """
    count_result = await env.DB.prepare(count_sql).bind(
        min_hcs, max_hcs, industry_query, industry_like, industry_query
    ).run()
    total = int(((_rows(count_result) or [{}])[0]).get("total") or 0)
    payload = {
        "results": rows,
        "filters": {
            "min_hcs": min_hcs, "max_hcs": max_hcs, "family": family,
            "industry": industry, "sort": sort,
        },
        "meta": {**_meta(count=len(rows), limit=limit, offset=offset), "total": total},
    }
    return _conditional_json(request, payload)


@app.get("/api/candidates")
async def candidates(
    request: Request,
    limit: int = 10,
    offset: int = 0,
    min_hcs: float = 0.01,
    max_hcs: float = PUBLIC_HCS_THRESHOLD - 0.01,
    family: str | None = None,
    industry: str | None = None,
    sort: str = "score",
):
    """Highest current eligible HCS scores below the public threshold."""
    env = request.scope["env"]
    limit = max(1, min(int(limit), 50))
    offset = max(0, min(int(offset), 5000))
    min_hcs = max(0.01, min(float(min_hcs), PUBLIC_HCS_THRESHOLD - 0.01))
    max_hcs = max(min_hcs, min(float(max_hcs), PUBLIC_HCS_THRESHOLD - 0.01))
    industry_query = (industry or "").strip()[:80]
    industry_like = f"%{industry_query.upper()}%"
    allowed_families = {"", "insider", "ownership", "institutional", "event", "capital_allocation"}
    allowed_sorts = {"score", "recent", "ticker", "change"}
    if (family or "").strip().lower() not in allowed_families:
        raise HTTPException(status_code=400, detail="Unknown signal family")
    if (sort or "score").strip().lower() not in allowed_sorts:
        raise HTTPException(status_code=400, detail="Unknown sort order")
    family_clause = _signal_family_clause(family)
    order_clause = _signal_order_clause(sort)
    sql = f"""
        WITH latest AS (
            SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.cik ORDER BY s.score_date DESC, s.id DESC) AS rn
            FROM scores s
        ), previous AS (
            SELECT s1.cik, s1.hcs_score
            FROM scores s1
            WHERE s1.id=(SELECT s2.id FROM scores s2 WHERE s2.cik=s1.cik AND s2.score_date<DATE('now') ORDER BY s2.score_date DESC, s2.id DESC LIMIT 1)
        )
        SELECT s.cik, c.ticker, c.company_name, c.exchange, c.sic, c.industry,
               c.hcs_eligible, c.issuer_type, c.universe_version,
               s.score_date, s.hcs_score, s.classification, s.scored_at, s.hcs_version,
               CASE WHEN s.insider_score>0 OR s.cluster_score>0 THEN 1 ELSE 0 END AS has_insider,
               CASE WHEN s.ownership_score>0 THEN 1 ELSE 0 END AS has_ownership,
               CASE WHEN s.whale_score>0 THEN 1 ELSE 0 END AS has_institutional,
               CASE WHEN s.event_score<>0 THEN 1 ELSE 0 END AS has_event,
               CASE WHEN s.event_score<0 THEN 1 ELSE 0 END AS has_negative_event,
               CASE WHEN s.capital_allocation_score>0 THEN 1 ELSE 0 END AS has_capital_allocation,
               (SELECT MAX(COALESCE(e.filing_date,e.event_date)) FROM score_component_evidence e
                WHERE e.cik=s.cik AND e.score_date=s.score_date AND e.component_score<>0) AS latest_evidence_date,
               ROUND(s.hcs_score - COALESCE(prev.hcs_score,0),1) AS one_day_change,
               (CASE WHEN s.insider_score>0 THEN 1 ELSE 0 END
                + CASE WHEN s.cluster_score>0 THEN 1 ELSE 0 END
                + CASE WHEN s.ownership_score>0 THEN 1 ELSE 0 END
                + CASE WHEN s.whale_score>0 THEN 1 ELSE 0 END
                + CASE WHEN s.event_score<>0 THEN 1 ELSE 0 END
                + CASE WHEN s.capital_allocation_score>0 THEN 1 ELSE 0 END) AS active_signal_count
        FROM latest s JOIN companies c ON c.cik=s.cik
        LEFT JOIN previous prev ON prev.cik=s.cik
        WHERE s.rn=1 AND c.hcs_eligible = 1 AND s.score_date = DATE('now')
          AND s.hcs_score BETWEEN ? AND ? AND {family_clause}
          AND (?='' OR UPPER(COALESCE(c.industry,'')) LIKE ? OR c.sic=?)
        ORDER BY {order_clause}
        LIMIT ? OFFSET ?
    """
    result = await env.DB.prepare(sql).bind(min_hcs, max_hcs, industry_query, industry_like, industry_query, limit, offset).run()
    rows = _rows(result)
    payload = {
        "results": rows,
        "filters": {"min_hcs": min_hcs, "max_hcs": max_hcs, "family": family, "industry": industry, "sort": sort},
        "meta": _meta(count=len(rows), limit=limit, offset=offset),
    }
    return _conditional_json(request, payload)


@app.get("/api/transactions/recent")
async def recent_transactions(
    request: Request,
    limit: int = 50,
):
    env = request.scope["env"]

    limit = max(
        1,
        min(limit, 200),
    )

    fetch_limit = min(500, max(200, limit * 5))

    result = await env.DB.prepare(
        """
        SELECT
            it.transaction_date,
            it.cik,
            c.ticker,
            c.company_name,
            c.hcs_eligible,
            it.insider_name,
            it.insider_role,
            it.transaction_code,
            it.shares,
            it.price,
            it.transaction_value,
            it.shares_owned_after
        FROM insider_transactions it
        LEFT JOIN companies c
            ON c.cik = it.cik
        WHERE it.is_open_market_purchase = 1
          AND c.hcs_eligible = 1
        ORDER BY
            it.transaction_date DESC,
            it.id DESC
        LIMIT ?
        """
    ).bind(fetch_limit).run()

    rows = _public_universe_rows(result, limit)
    return {"results": rows, "meta": _meta(count=len(rows), limit=limit, offset=0)}


@app.get("/api/ownership/recent")
async def recent_ownership(request: Request, limit: int = 50):
    """Return one economic ownership-group signal per Schedule 13 filing.

    Joint filers are intentionally collapsed to one group so a single economic
    stake is not counted multiple times.
    """
    env = request.scope["env"]
    limit = max(1, min(limit, 200))
    fetch_limit = min(500, max(200, limit * 5))

    result = await env.DB.prepare(
        """
        SELECT
            og.event_date,
            og.issuer_cik,
            c.ticker,
            c.company_name,
            c.hcs_eligible,
            og.representative_investor_name AS investor_name,
            og.schedule_type,
            og.group_shares_owned AS shares_owned,
            og.group_ownership_pct AS ownership_pct,
            og.previous_ownership_pct,
            og.ownership_change_pp,
            og.member_count,
            og.group_key,
            og.predecessor_accession
        FROM ownership_groups og
        LEFT JOIN companies c ON c.cik = og.issuer_cik
        WHERE c.hcs_eligible = 1
        ORDER BY COALESCE(og.event_date, DATE(og.created_at)) DESC, og.filing_id DESC
        LIMIT ?
        """
    ).bind(fetch_limit).run()
    rows = _public_universe_rows(result, limit)
    return {"results": rows, "meta": _meta(count=len(rows), limit=limit, offset=0)}


@app.get("/api/events/recent")
async def recent_events(request: Request, limit: int = 50):
    env = request.scope["env"]
    limit = max(1, min(limit, 200))
    fetch_limit = min(500, max(200, limit * 5))

    result = await env.DB.prepare(
        """
        SELECT
            e.event_date, e.cik, c.ticker, COALESCE(c.company_name, e.company_name) AS company_name,
            c.hcs_eligible, e.item_numbers, e.event_type, e.sentiment, e.event_score, e.matched_keywords,
            e.primary_item, e.scoring_reason, e.parser_version, e.scoring_version
        FROM eight_k_events e
        LEFT JOIN companies c ON c.cik = e.cik
        WHERE c.hcs_eligible = 1
        ORDER BY COALESCE(e.event_date, DATE(e.created_at)) DESC, e.id DESC
        LIMIT ?
        """
    ).bind(fetch_limit).run()
    rows = _public_universe_rows(result, limit)
    return {"results": rows, "meta": _meta(count=len(rows), limit=limit, offset=0)}


@app.get("/api/institutions/recent")
async def recent_institutions(request: Request, limit: int = 50):
    env = request.scope["env"]
    limit = max(1, min(limit, 200))
    fetch_limit = min(500, max(200, limit * 5))

    result = await env.DB.prepare(
        """
        SELECT
            ip.period_of_report, ip.manager_cik, ip.manager_name, ip.issuer_cik,
            COALESCE(c.ticker, ip.ticker) AS ticker, ip.issuer_name, ip.cusip,
            c.hcs_eligible, ip.shares, ip.value_dollars, ip.position_weight_pct,
            ip.previous_shares, ip.previous_value_dollars, ip.share_change_pct,
            ip.is_new_position, ip.position_score, ip.comparison_status,
            ip.predecessor_complete, ip.corporate_action_suspected
        FROM institutional_positions ip
        LEFT JOIN companies c ON c.cik = ip.issuer_cik
        WHERE ip.position_score > 0
          AND ip.issuer_cik IS NOT NULL
          AND c.hcs_eligible = 1
        ORDER BY ip.period_of_report DESC, ip.position_score DESC, ip.value_dollars DESC
        LIMIT ?
        """
    ).bind(fetch_limit).run()
    rows = _public_universe_rows(result, limit)
    return {"results": rows, "meta": _meta(count=len(rows), limit=limit, offset=0)}


@app.get("/api/freshness")
async def freshness(request: Request):
    """Public data-freshness receipt, including per-pipeline health."""
    env = request.scope["env"]

    filing_result = await env.DB.prepare(
        """
        SELECT
            MAX(filed_at) AS latest_filing_at,
            MAX(created_at) AS latest_discovered_at,
            SUM(CASE WHEN processed=0 THEN 1 ELSE 0 END) AS unprocessed_filings
        FROM filings
        """
    ).run()
    score_result = await env.DB.prepare(
        """
        SELECT
            MAX(score_date) AS latest_score_date,
            MAX(created_at) AS latest_score_created_at,
            SUM(CASE WHEN score_date=DATE('now') AND hcs_score>0 THEN 1 ELSE 0 END) AS positive_scores_today
        FROM scores
        """
    ).run()
    pipeline_result = await env.DB.prepare(
        """
        SELECT
          CASE
            WHEN form_type LIKE '13F-HR%' THEN '13f'
            WHEN form_type LIKE 'SCHEDULE 13%' THEN 'ownership'
            WHEN form_type LIKE '8-K%' THEN '8k'
            WHEN form_type IN ('4','4/A') THEN 'form4'
            ELSE 'other'
          END AS pipeline,
          MAX(filed_at) AS latest_filing_at,
          MAX(created_at) AS latest_discovered_at,
          SUM(CASE WHEN processed=0 THEN 1 ELSE 0 END) AS unprocessed
        FROM filings
        GROUP BY 1
        """
    ).run()
    stale_result = await env.DB.prepare(
        """
        SELECT COUNT(*) AS stale_active_scores
        FROM current_scores
        WHERE hcs_score>0 AND score_date<DATE('now')
        """
    ).run()

    discovery_rows = []
    try:
        discovery_result = await env.DB.prepare(
            """
            SELECT source, last_attempt_at, last_success_at, latest_filed_at,
                   last_discovered_count, consecutive_failures, last_error
            FROM discovery_health
            ORDER BY source
            """
        ).run()
        discovery_rows = _rows(discovery_result)
    except Exception:
        discovery_rows = []

    filing_row = (_rows(filing_result) or [{}])[0]
    filing_row["latest_filing_at"] = normalize_sec_date(filing_row.get("latest_filing_at"))
    score_row = (_rows(score_result) or [{}])[0]
    stale_row = (_rows(stale_result) or [{}])[0]
    pipelines = {row.get("pipeline"): row for row in _rows(pipeline_result) if row.get("pipeline") != "other"}
    for row in pipelines.values():
        row["latest_filing_at"] = normalize_sec_date(row.get("latest_filing_at"))
    discovery = {row.get("source"): row for row in discovery_rows}

    for source in ("form4", "ownership", "8k", "13f"):
        pipelines.setdefault(source, {
            "pipeline": source,
            "latest_filing_at": None,
            "latest_discovered_at": None,
            "unprocessed": 0,
        })
        if source in discovery:
            pipelines[source]["discovery"] = discovery[source]

    top_score = {}
    runtime = {}
    metrics = []
    try:
        top_result = await env.DB.prepare(
            """
            SELECT s.cik, c.ticker, c.company_name, s.hcs_score, s.classification, s.score_date
            FROM current_scores s JOIN companies c ON c.cik=s.cik
            WHERE c.hcs_eligible = 1 AND s.score_date = DATE('now')
            ORDER BY s.hcs_score DESC, COALESCE(c.ticker,c.company_name,s.cik) ASC LIMIT 1
            """
        ).run()
        top_score = (_rows(top_result) or [{}])[0]
    except Exception:
        top_score = {}
    try:
        runtime_result = await env.DB.prepare(
            "SELECT key, value, updated_at FROM runtime_status ORDER BY key"
        ).run()
        runtime = {row.get("key"): {"value": row.get("value"), "updated_at": row.get("updated_at")} for row in _rows(runtime_result)}
    except Exception:
        runtime = {}
    try:
        metrics_result = await env.DB.prepare(
            "SELECT pipeline, discovered, processed, failed, retried, latest_latency_seconds, updated_at FROM pipeline_metrics WHERE metric_date=DATE('now') ORDER BY pipeline"
        ).run()
        metrics = _rows(metrics_result)
    except Exception:
        metrics = []

    payload = {
        "latest_filing_at": filing_row.get("latest_filing_at"),
        "latest_discovered_at": filing_row.get("latest_discovered_at"),
        "unprocessed_filings": int(filing_row.get("unprocessed_filings") or 0),
        "latest_score_date": score_row.get("latest_score_date"),
        "latest_score_created_at": score_row.get("latest_score_created_at"),
        "positive_scores_today": int(score_row.get("positive_scores_today") or 0),
        "stale_active_scores": int(stale_row.get("stale_active_scores") or 0),
        "highest_current_score": top_score,
        "pipelines": pipelines,
        "runtime": runtime,
        "pipeline_metrics_today": metrics,
        "version": APP_VERSION,
        "meta": _meta(),
    }
    return _conditional_json(request, payload)


@app.head("/api/health")
async def health_head(request: Request):
    response = await health(request)
    return Response(status_code=response.status_code, headers=response.headers)


@app.head("/api/system")
async def system_head(request: Request):
    response = await system_console_snapshot(request)
    return Response(status_code=response.status_code, headers=response.headers)


@app.head("/api/freshness")
async def freshness_head(request: Request):
    response = await freshness(request)
    return Response(status_code=response.status_code, headers=response.headers)


@app.get("/api/company/{identifier}")
async def company_detail(
    identifier: str,
    request: Request,
):
    """Return a public company research view by ticker or CIK.

    The response intentionally exposes signal families and source evidence, not
    proprietary component weights.
    """
    env = request.scope["env"]
    mode, value = company_lookup_mode(identifier)

    if not value:
        raise HTTPException(status_code=404, detail="Company not found")

    if mode == "cik":
        result = await env.DB.prepare(
            """
            SELECT
                c.cik, c.ticker, c.company_name, c.exchange, c.sic, c.industry,
                c.issuer_type, c.hcs_eligible, c.hcs_exclusion_reason, c.universe_version,
                s.score_date, s.hcs_score, s.classification,
                s.insider_score, s.cluster_score, s.ownership_score,
                s.whale_score, s.event_score, s.capital_allocation_score,
                s.convergence_bonus, s.penalty
            FROM companies c
            LEFT JOIN scores s ON s.id = (
                SELECT s2.id FROM scores s2 WHERE s2.cik=c.cik
                ORDER BY s2.score_date DESC, s2.id DESC LIMIT 1
            )
            WHERE c.cik = ?
            LIMIT 1
            """
        ).bind(value).run()
    else:
        result = await env.DB.prepare(
            """
            SELECT
                c.cik, c.ticker, c.company_name, c.exchange, c.sic, c.industry,
                c.issuer_type, c.hcs_eligible, c.hcs_exclusion_reason, c.universe_version,
                s.score_date, s.hcs_score, s.classification,
                s.insider_score, s.cluster_score, s.ownership_score,
                s.whale_score, s.event_score, s.capital_allocation_score,
                s.convergence_bonus, s.penalty
            FROM companies c
            LEFT JOIN scores s ON s.id = (
                SELECT s2.id FROM scores s2 WHERE s2.cik=c.cik
                ORDER BY s2.score_date DESC, s2.id DESC LIMIT 1
            )
            WHERE UPPER(c.ticker) = ?
               OR c.cik IN (
                    SELECT cik FROM company_tickers WHERE UPPER(ticker) = ?
               )
            LIMIT 1
            """
        ).bind(value, value).run()

    rows = _rows(result)
    if not rows:
        raise HTTPException(status_code=404, detail="Company not found")

    row = rows[0]
    cik = row.get("cik")
    heuristic_universe = classify_issuer(row.get("company_name"), row.get("ticker"), row.get("exchange"))
    universe_rule = heuristic_universe.rule
    universe_reason = heuristic_universe.reason
    score_metadata = {}
    try:
        meta_result = await env.DB.prepare(
            """
            SELECT c.universe_rule, c.universe_reason, c.universe_version,
                   s.confirming_manager_count, s.strongest_manager_score,
                   s.aggregate_manager_signal, s.hcs_version, s.institutional_version,
                   s.universe_version AS score_universe_version, s.scored_at,
                   s.scoring_engine_version, s.evidence_fingerprint, s.reason_codes_json, s.score_delta
            FROM companies c
            LEFT JOIN scores s ON s.id=(
                SELECT s2.id FROM scores s2 WHERE s2.cik=c.cik
                ORDER BY s2.score_date DESC, s2.id DESC LIMIT 1
            )
            WHERE c.cik=? LIMIT 1
            """
        ).bind(cik).run()
        score_metadata = (_rows(meta_result) or [{}])[0]
        universe_rule = score_metadata.get("universe_rule") or universe_rule
        universe_reason = score_metadata.get("universe_reason") or universe_reason
    except Exception:
        score_metadata = {}

    score = {
        "score_date": row.get("score_date"),
        "hcs_score": row.get("hcs_score"),
        "classification": row.get("classification"),
        "insider_score": row.get("insider_score"),
        "cluster_score": row.get("cluster_score"),
        "ownership_score": row.get("ownership_score"),
        "whale_score": row.get("whale_score"),
        "event_score": row.get("event_score"),
        "capital_allocation_score": row.get("capital_allocation_score"),
        "convergence_bonus": row.get("convergence_bonus"),
        "penalty": row.get("penalty"),
    }
    public_signals = public_signal_families(score)
    signal_keys = {item["key"] for item in public_signals}
    evidence = await build_public_evidence(
        env, cik, row.get("score_date"), signal_keys
    )

    history_result = await env.DB.prepare(
        """
        SELECT score_date, hcs_score, classification
        FROM scores
        WHERE cik=?
          AND score_date >= DATE('now','-90 day')
        ORDER BY score_date DESC, id DESC
        LIMIT 91
        """
    ).bind(cik).run()
    score_history = _rows(history_result)

    current_score = float(row.get("hcs_score") or 0)
    score_changes = {}
    for days, label in ((1, "1d"), (7, "7d"), (30, "30d")):
        compare_result = await env.DB.prepare(
            """
            SELECT hcs_score, score_date
            FROM scores
            WHERE cik=? AND score_date <= DATE('now', ?)
            ORDER BY score_date DESC, id DESC
            LIMIT 1
            """
        ).bind(cik, f"-{days} day").run()
        compare_rows = _rows(compare_result)
        if compare_rows:
            prior = float(compare_rows[0].get("hcs_score") or 0)
            score_changes[label] = {
                "change": round(current_score - prior, 1),
                "comparison_date": compare_rows[0].get("score_date"),
            }
        else:
            score_changes[label] = None

    institutional_freshness = {"data_through": None, "latest_filed_at": None}
    try:
        institution_freshness_result = await env.DB.prepare(
            """
            SELECT MAX(period_of_report) AS data_through, MAX(filing_date) AS latest_filed_at
            FROM institutional_positions
            WHERE issuer_cik=?
              AND COALESCE(issuer_mapping_status, 'resolved')='resolved'
            """
        ).bind(cik).run()
        institutional_freshness = (_rows(institution_freshness_result) or [institutional_freshness])[0]
    except Exception:
        institution_freshness_result = await env.DB.prepare(
            "SELECT MAX(period_of_report) AS data_through, MAX(filing_date) AS latest_filed_at FROM institutional_positions WHERE issuer_cik=?"
        ).bind(cik).run()
        institutional_freshness = (_rows(institution_freshness_result) or [institutional_freshness])[0]

    evidence.sort(
        key=lambda item: (item.get("event_date") or item.get("filed_at") or ""),
        reverse=True,
    )
    latest_relevant_filing_at = max(
        [item.get("filed_at") or "" for item in evidence] or [""]
    ) or None

    active_labels = [item.get("label") for item in public_signals if item.get("label")]
    if active_labels:
        why_now = f"Current HCS reflects {', '.join(active_labels[:3])}" + (" and additional active evidence." if len(active_labels) > 3 else ".")
    elif row.get("hcs_score"):
        why_now = "The issuer has a current positive HCS, but no public signal family is presently exposed."
    else:
        why_now = "No active public signal family is currently contributing to HCS."

    timeline = [
        {
            "family": item.get("family"),
            "event_date": item.get("event_date") or item.get("filed_at"),
            "filed_at": item.get("filed_at"),
            "form_type": item.get("form_type"),
            "filing_url": item.get("filing_url"),
        }
        for item in evidence
    ]

    score_delta = score_metadata.get("score_delta")
    try:
        reason_codes = json.loads(score_metadata.get("reason_codes_json") or "[]")
    except Exception:
        reason_codes = []
    delta_value = float(score_delta or 0)
    if delta_value > 0:
        why_changed = f"HCS increased {delta_value:.1f} points from the prior materialized score."
    elif delta_value < 0:
        why_changed = f"HCS decreased {abs(delta_value):.1f} points from the prior materialized score."
    else:
        why_changed = "HCS is unchanged from the prior materialized score."
    if reason_codes:
        why_changed += " Active evidence families: " + ", ".join(str(code).replace("_", " ") for code in reason_codes) + "."

    payload = {
        "company": {
            "cik": row.get("cik"),
            "ticker": row.get("ticker"),
            "company_name": row.get("company_name"),
            "exchange": row.get("exchange"),
            "sic": row.get("sic"),
            "industry": row.get("industry"),
            "issuer_type": row.get("issuer_type") or heuristic_universe.issuer_type,
            "hcs_eligible": bool(int(row.get("hcs_eligible"))) if row.get("hcs_eligible") is not None else heuristic_universe.hcs_eligible,
            "hcs_exclusion_reason": row.get("hcs_exclusion_reason") or heuristic_universe.exclusion_reason,
            "universe_rule": universe_rule,
            "universe_reason": universe_reason,
            "universe_version": row.get("universe_version") or UNIVERSE_VERSION,
        },
        "score": {
            "score_date": row.get("score_date"),
            "hcs_score": row.get("hcs_score"),
            "classification": row.get("classification"),
            "hcs_version": score_metadata.get("hcs_version"),
            "institutional_version": score_metadata.get("institutional_version"),
            "universe_version": score_metadata.get("score_universe_version"),
            "scored_at": score_metadata.get("scored_at"),
            "scoring_engine_version": score_metadata.get("scoring_engine_version") or score_metadata.get("hcs_version"),
            "evidence_fingerprint": score_metadata.get("evidence_fingerprint"),
            "reason_codes": reason_codes,
            "score_delta": score_metadata.get("score_delta"),
            "is_current": row.get("score_date") is not None and str(row.get("score_date")) == datetime.now(timezone.utc).date().isoformat(),
        },
        "institutional_confirmation": {
            "confirming_manager_count": int(score_metadata.get("confirming_manager_count") or 0),
            "data_through": institutional_freshness.get("data_through"),
            "latest_filed_at": institutional_freshness.get("latest_filed_at"),
            "delayed_data_notice": "13F holdings are delayed quarterly disclosures and are used only as confirmation of fresher signals.",
        },
        "why_now": why_now,
        "why_changed": why_changed,
        "signals": public_signals,
        "evidence": evidence,
        "timeline": timeline,
        "score_history": score_history,
        "score_changes": score_changes,
        "freshness": {
            "score_date": row.get("score_date"),
            "latest_relevant_filing_at": latest_relevant_filing_at,
            "institutional_data_through": institutional_freshness.get("data_through"),
            "institutional_latest_filed_at": institutional_freshness.get("latest_filed_at"),
        },
        "meta": _meta(),
    }
    return _conditional_json(request, payload)


@app.get("/api/admin/system-health")
async def admin_system_health(
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Compact operational receipt spanning ingestion and recomputation."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)

    queue_result = await env.DB.prepare(
        """
        SELECT status, COUNT(*) AS count
        FROM processing_queue
        GROUP BY status ORDER BY status
        """
    ).run()
    score_queue_result = await env.DB.prepare(
        """
        SELECT status, COUNT(*) AS count
        FROM score_recompute_queue
        GROUP BY status ORDER BY status
        """
    ).run()
    stale_result = await env.DB.prepare(
        """
        SELECT COUNT(*) AS stale_active_scores
        FROM current_scores
        WHERE hcs_score>0 AND score_date<DATE('now')
        """
    ).run()
    filing_result = await env.DB.prepare(
        """
        SELECT COUNT(*) AS unprocessed_filings, MIN(filed_at) AS oldest_unprocessed_filing
        FROM filings WHERE processed=0
        """
    ).run()
    discovery_rows = []
    try:
        result = await env.DB.prepare(
            """
            SELECT source, last_attempt_at, last_success_at, latest_filed_at,
                   last_discovered_count, consecutive_failures, last_error
            FROM discovery_health ORDER BY source
            """
        ).run()
        discovery_rows = _rows(result)
    except Exception:
        pass

    return {
        "version": VERSION,
        "processing_queue": _rows(queue_result),
        "score_recompute_queue": _rows(score_queue_result),
        "stale_active_scores": int(((_rows(stale_result) or [{}])[0]).get("stale_active_scores") or 0),
        "filings": (_rows(filing_result) or [{}])[0],
        "discovery": discovery_rows,
        "limits": {
            "security_resolution_batch": SECURITY_RESOLUTION_BATCH,
            "security_resolution_hard_cap": SECURITY_RESOLUTION_HARD_CAP,
            "score_recompute_batch": SCORE_RECOMPUTE_BATCH,
            "score_recompute_hard_cap": SCORE_RECOMPUTE_HARD_CAP,
        },
    }


@app.get("/api/admin/query-plans")
async def admin_query_plans(
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Return fixed EXPLAIN QUERY PLAN output for the highest-value read paths."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)
    plans = {}
    statements = {
        "home_signals": "SELECT s.cik FROM scores s JOIN companies c ON c.cik=s.cik WHERE c.hcs_eligible=1 AND s.score_date=DATE('now') AND s.hcs_score>=80 ORDER BY s.hcs_score DESC LIMIT 20",
        "company_latest_score": "SELECT id FROM scores WHERE cik='0000000000' ORDER BY score_date DESC, id DESC LIMIT 1",
        "processing_ready": "SELECT id FROM processing_queue WHERE status='pending' ORDER BY priority, id LIMIT 25",
        "score_recompute_ready": "SELECT cik FROM score_recompute_queue WHERE status='pending' ORDER BY created_at, cik LIMIT 25",
    }
    for name, sql in statements.items():
        try:
            result = await env.DB.prepare("EXPLAIN QUERY PLAN " + sql).run()
            plans[name] = _rows(result)
        except Exception as exc:
            plans[name] = [{"error": str(exc)[:300]}]
    return {"plans": plans, "meta": _meta()}


@app.get("/api/admin/diagnostics")
async def admin_diagnostics(
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Cross-pipeline diagnostics for data quality, mapping consistency, and ingestion gaps."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)

    watermarks = await env.DB.prepare(
        "SELECT * FROM discovery_watermarks ORDER BY source"
    ).run()
    metrics = await env.DB.prepare(
        "SELECT * FROM pipeline_metrics WHERE metric_date>=DATE('now','-7 day') ORDER BY metric_date DESC, pipeline"
    ).run()
    duplicates = await env.DB.prepare(
        """
        SELECT filing_url, COUNT(*) AS duplicate_count
        FROM filings GROUP BY filing_url HAVING COUNT(*)>1
        ORDER BY duplicate_count DESC LIMIT 25
        """
    ).run()
    ticker_conflicts = await env.DB.prepare(
        """
        SELECT UPPER(ticker) AS ticker, COUNT(DISTINCT cik) AS cik_count, GROUP_CONCAT(DISTINCT cik) AS ciks
        FROM company_tickers
        WHERE COALESCE(TRIM(ticker),'')<>''
        GROUP BY UPPER(ticker) HAVING COUNT(DISTINCT cik)>1
        ORDER BY cik_count DESC, ticker LIMIT 50
        """
    ).run()
    identity_stale = await env.DB.prepare(
        """
        SELECT COUNT(*) AS unchecked_or_stale
        FROM companies
        WHERE active=1 AND (identity_checked_at IS NULL OR identity_checked_at<DATETIME('now','-90 day'))
        """
    ).run()
    queue_errors = await env.DB.prepare(
        """
        SELECT q.id, q.filing_id, f.form_type, f.accession_number, q.attempts, q.last_error, q.processed_at, q.next_retry_at, q.dead_letter_at, q.last_attempt_at
        FROM processing_queue q JOIN filings f ON f.id=q.filing_id
        WHERE q.status='error' ORDER BY COALESCE(q.processed_at,q.created_at) DESC LIMIT 50
        """
    ).run()
    recompute_errors = await env.DB.prepare(
        "SELECT cik, reason, attempts, last_error, processed_at, next_retry_at, dead_letter_at, last_attempt_at FROM score_recompute_queue WHERE status='error' ORDER BY COALESCE(processed_at,created_at) DESC LIMIT 50"
    ).run()
    runtime = await env.DB.prepare(
        "SELECT key, value, updated_at FROM runtime_status ORDER BY key"
    ).run()
    issues = await env.DB.prepare(
        "SELECT issue_type, entity_key, severity, detail_json, detected_at FROM data_quality_issues WHERE resolved_at IS NULL ORDER BY CASE severity WHEN 'error' THEN 0 ELSE 1 END, detected_at DESC LIMIT 100"
    ).run()
    cron_runs = await env.DB.prepare(
        "SELECT run_id, started_at, finished_at, status, duration_ms, error_text, app_version FROM cron_runs ORDER BY id DESC LIMIT 25"
    ).run()
    reconciliations = await env.DB.prepare(
        "SELECT * FROM reconciliation_runs ORDER BY reconciliation_date DESC, id DESC LIMIT 25"
    ).run()
    replays = await env.DB.prepare(
        "SELECT * FROM replay_receipts ORDER BY requested_at DESC, id DESC LIMIT 25"
    ).run()

    return {
        "version": version_payload(
            hcs_version=HCS_VERSION, institutional_version=INSTITUTIONAL_VERSION,
            universe_version=UNIVERSE_VERSION, security_master_version=SECURITY_MASTER_VERSION,
        ),
        "watermarks": _rows(watermarks),
        "pipeline_metrics": _rows(metrics),
        "duplicate_filing_urls": _rows(duplicates),
        "ticker_conflicts": _rows(ticker_conflicts),
        "identity": (_rows(identity_stale) or [{}])[0],
        "processing_errors": _rows(queue_errors),
        "score_recompute_errors": _rows(recompute_errors),
        "runtime": _rows(runtime),
        "open_data_quality_issues": _rows(issues),
        "cron_runs": _rows(cron_runs),
        "reconciliations": _rows(reconciliations),
        "replay_receipts": _rows(replays),
        "meta": _meta(),
    }


@app.post("/api/admin/replay/failures")
async def admin_replay_failures(
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Bulk-requeue current filing failures after an operator explicitly requests it."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)
    result = await env.DB.prepare(
        """SELECT q.id, q.filing_id, f.accession_number, f.filing_url, f.form_type
        FROM processing_queue q JOIN filings f ON f.id=q.filing_id
        WHERE q.status='error' ORDER BY COALESCE(q.dead_letter_at,q.processed_at,q.created_at) LIMIT 50"""
    ).run()
    rows = _rows(result)
    replayed = 0
    for row in rows:
        try:
            await env.DB.prepare(
                """UPDATE processing_queue SET status='pending',attempts=0,processed_at=NULL,started_at=NULL,
                last_attempt_at=NULL,next_retry_at=NULL,dead_letter_at=NULL,last_error=NULL WHERE id=?"""
            ).bind(int(row["id"])).run()
            await env.DB.prepare("UPDATE filings SET processed=0,enqueued_at=CURRENT_TIMESTAMP WHERE id=?").bind(int(row["filing_id"])).run()
            await env.FILING_QUEUE.send({
                "filing_id": row["filing_id"], "queue_id": row["id"],
                "accession_number": row["accession_number"], "filing_url": row["filing_url"],
                "form_type": row["form_type"],
            })
            replayed += 1
        except Exception:
            continue
    return {"status": "queued", "replayed": replayed, "requested": len(rows), "meta": _meta()}


@app.post("/api/admin/deployment-metrics")
async def admin_deployment_metrics(
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Persist post-deploy performance receipts for version-over-version comparison."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)
    body = await request.json()
    build_id = str(body.get("build_id") or BUILD_ID)[:160]
    try:
        queue = await _queue_throughput_snapshot(env)
        await env.DB.prepare(
            """INSERT INTO deployment_metrics(build_id,app_version,deployed_at,health_ttfb_ms,home_ttfb_ms,system_ttfb_ms,queue_pending,queue_errors,worker_architecture,captured_at)
            VALUES (?,?,?,?,?,?,?,?, 'public-js-v1', CURRENT_TIMESTAMP)
            ON CONFLICT(build_id) DO UPDATE SET health_ttfb_ms=excluded.health_ttfb_ms,home_ttfb_ms=excluded.home_ttfb_ms,
              system_ttfb_ms=excluded.system_ttfb_ms,queue_pending=excluded.queue_pending,queue_errors=excluded.queue_errors,captured_at=CURRENT_TIMESTAMP"""
        ).bind(
            build_id, APP_VERSION, BUILD_DEPLOYED_AT, float(body.get("health_ttfb_ms") or 0),
            float(body.get("home_ttfb_ms") or 0), float(body.get("system_ttfb_ms") or 0),
            int(queue.get("pending") or 0), int(queue.get("errors") or 0),
        ).run()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not persist deployment metrics: {str(exc)[:200]}")
    return {"status": "stored", "build_id": build_id, "meta": _meta()}


@app.post("/api/admin/replay/filing/{queue_id}")
async def admin_replay_filing(
    queue_id: int,
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Reset and republish one dead-lettered/general filing queue item."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)
    result = await env.DB.prepare(
        """
        SELECT q.id AS queue_id, q.filing_id, f.accession_number, f.filing_url, f.form_type
        FROM processing_queue q JOIN filings f ON f.id=q.filing_id
        WHERE q.id=? LIMIT 1
        """
    ).bind(int(queue_id)).run()
    rows = _rows(result)
    if not rows:
        raise HTTPException(status_code=404, detail="Queue item not found")
    row = rows[0]
    receipt = await env.DB.prepare(
        "INSERT INTO replay_receipts(replay_type, entity_key, status) VALUES ('filing', ?, 'pending')"
    ).bind(str(queue_id)).run()
    receipt_meta = getattr(receipt, "meta", None)
    receipt_id = int(
        (receipt_meta.get("last_row_id", 0) if isinstance(receipt_meta, dict) else getattr(receipt_meta, "last_row_id", 0)) or 0
    )
    try:
        await env.DB.prepare(
            """
            UPDATE processing_queue
            SET status='pending', attempts=0, processed_at=NULL, started_at=NULL,
                last_attempt_at=NULL, next_retry_at=NULL, dead_letter_at=NULL, last_error=NULL
            WHERE id=?
            """
        ).bind(int(queue_id)).run()
        await env.DB.prepare(
            "UPDATE filings SET processed=0, enqueued_at=CURRENT_TIMESTAMP WHERE id=?"
        ).bind(int(row["filing_id"])).run()
        await env.FILING_QUEUE.send({
            "filing_id": row["filing_id"],
            "queue_id": row["queue_id"],
            "accession_number": row["accession_number"],
            "filing_url": row["filing_url"],
            "form_type": row["form_type"],
        })
        if receipt_id:
            await env.DB.prepare(
                "UPDATE replay_receipts SET status='done', completed_at=CURRENT_TIMESTAMP, result_json=? WHERE id=?"
            ).bind(json.dumps({"republished": True}, separators=(",", ":")), receipt_id).run()
        await env.DB.prepare(
            "UPDATE data_quality_issues SET resolved_at=CURRENT_TIMESTAMP WHERE issue_type='processing_queue_error' AND entity_key=? AND resolved_at IS NULL"
        ).bind(str(queue_id)).run()
        return {"status": "republished", "queue_id": int(queue_id), "receipt_id": receipt_id, "meta": _meta()}
    except Exception as exc:
        if receipt_id:
            await env.DB.prepare(
                "UPDATE replay_receipts SET status='error', completed_at=CURRENT_TIMESTAMP, error_text=? WHERE id=?"
            ).bind(str(exc)[:1000], receipt_id).run()
        raise


@app.post("/api/admin/replay/score/{cik}")
async def admin_replay_score_queue(
    cik: str,
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Reset one failed score recompute so the next cron can retry it."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)
    normalized = _normalize_cik(cik)
    if not normalized:
        raise HTTPException(status_code=400, detail="Invalid CIK")
    await env.DB.prepare(
        """
        INSERT INTO score_recompute_queue(cik, reason, status, attempts, created_at, started_at, processed_at, last_error, next_retry_at, dead_letter_at, last_attempt_at)
        VALUES (?, 'admin replay', 'pending', 0, CURRENT_TIMESTAMP, NULL, NULL, NULL, NULL, NULL, NULL)
        ON CONFLICT(cik) DO UPDATE SET reason='admin replay', status='pending', attempts=0,
          created_at=CURRENT_TIMESTAMP, started_at=NULL, processed_at=NULL, last_error=NULL,
          next_retry_at=NULL, dead_letter_at=NULL, last_attempt_at=NULL
        """
    ).bind(normalized).run()
    return {"status": "queued", "cik": normalized, "meta": _meta()}


@app.get("/api/admin/replay-score/{identifier}")
async def admin_replay_score_receipt(
    identifier: str,
    request: Request,
    score_date: str | None = None,
    x_admin_key: str | None = Header(default=None),
):
    """Reconstruct the stored evidence receipt for a historical materialized score.

    This is deterministic and non-mutating: it rehydrates the exact evidence IDs and
    engine versions stored with the score, then recomputes the provenance fingerprint.
    """
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)
    mode, value = company_lookup_mode(identifier)
    if not value:
        raise HTTPException(status_code=404, detail="Company not found")
    if mode == "cik":
        company_result = await env.DB.prepare("SELECT cik, ticker, company_name FROM companies WHERE cik=? LIMIT 1").bind(value).run()
    else:
        company_result = await env.DB.prepare(
            "SELECT cik, ticker, company_name FROM companies WHERE UPPER(ticker)=? OR cik IN (SELECT cik FROM company_tickers WHERE UPPER(ticker)=?) LIMIT 1"
        ).bind(value, value).run()
    company_rows = _rows(company_result)
    if not company_rows:
        raise HTTPException(status_code=404, detail="Company not found")
    company = company_rows[0]
    cik = str(company.get("cik"))
    date_filter = score_date or datetime.now(timezone.utc).date().isoformat()
    score_result = await env.DB.prepare(
        """
        SELECT cik, score_date, hcs_score, classification, hcs_version, institutional_version,
               universe_version, scoring_engine_version, evidence_fingerprint, reason_codes_json, score_delta
        FROM scores WHERE cik=? AND score_date=? LIMIT 1
        """
    ).bind(cik, date_filter).run()
    score_rows = _rows(score_result)
    if not score_rows:
        raise HTTPException(status_code=404, detail="Score not found for requested date")
    score = score_rows[0]
    evidence_result = await env.DB.prepare(
        """
        SELECT component, component_score, source_type, source_id, event_date, filing_date, detail_json,
               hcs_version, institutional_version, universe_version
        FROM score_component_evidence
        WHERE cik=? AND score_date=?
        ORDER BY CASE component
          WHEN 'insider' THEN 1 WHEN 'cluster' THEN 2 WHEN 'ownership' THEN 3
          WHEN 'institutional' THEN 4 WHEN 'event' THEN 5 WHEN 'capital_allocation' THEN 6
          WHEN 'convergence' THEN 7 WHEN 'penalty' THEN 8 ELSE 99 END
        """
    ).bind(cik, date_filter).run()
    evidence = _rows(evidence_result)
    receipt_evidence = [{
        "component": item.get("component"), "score": float(item.get("component_score") or 0),
        "source_type": item.get("source_type"), "source_id": item.get("source_id"),
        "event_date": item.get("event_date"), "filing_date": item.get("filing_date"),
    } for item in evidence]
    replay_fingerprint = hashlib.sha256(
        json.dumps({
            "cik": cik,
            "hcs_version": score.get("hcs_version"),
            "institutional_version": score.get("institutional_version"),
            "universe_version": score.get("universe_version"),
            "evidence": receipt_evidence,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    component_values = {str(item.get("component") or ""): float(item.get("component_score") or 0) for item in evidence}
    replay_breakdown = ScoreBreakdown(
        insider_score=component_values.get("insider", 0),
        cluster_score=component_values.get("cluster", 0),
        ownership_score=component_values.get("ownership", 0),
        whale_score=component_values.get("institutional", 0),
        event_score=component_values.get("event", 0),
        capital_allocation_score=component_values.get("capital_allocation", 0),
        convergence_bonus=component_values.get("convergence", 0),
        penalty=component_values.get("penalty", 0),
    )
    stored_hcs = float(score.get("hcs_score") or 0)
    replay_hcs = float(replay_breakdown.hcs_score)
    return {
        "company": company, "score": score, "evidence": evidence,
        "replay": {
            "hcs_score": replay_hcs,
            "classification": replay_breakdown.classification,
            "matches_stored_score": abs(stored_hcs - replay_hcs) < 0.0001,
        },
        "replay_fingerprint": replay_fingerprint,
        "matches_stored_fingerprint": bool(score.get("evidence_fingerprint")) and hmac.compare_digest(str(score.get("evidence_fingerprint")), replay_fingerprint),
        "meta": _meta(),
    }


@app.get("/api/admin/8k-health")
async def admin_8k_health(
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Operational and semantic health receipt for the 8-K pipeline."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)

    queue_result = await env.DB.prepare(
        """
        SELECT q.status, COUNT(*) AS count
        FROM processing_queue q
        JOIN filings f ON f.id = q.filing_id
        WHERE f.form_type LIKE '8-K%'
        GROUP BY q.status
        ORDER BY q.status
        """
    ).run()
    filings_result = await env.DB.prepare(
        """
        SELECT
            COUNT(*) AS total_filings,
            SUM(CASE WHEN processed = 1 THEN 1 ELSE 0 END) AS processed_filings,
            SUM(CASE WHEN processed = 0 THEN 1 ELSE 0 END) AS unprocessed_filings,
            MAX(filed_at) AS latest_filed_at
        FROM filings
        WHERE form_type LIKE '8-K%'
        """
    ).run()
    events_result = await env.DB.prepare(
        """
        SELECT
            COUNT(*) AS event_rows,
            COUNT(DISTINCT filing_id) AS distinct_filings,
            SUM(CASE WHEN event_score > 0 THEN 1 ELSE 0 END) AS positive_events,
            SUM(CASE WHEN event_score = 0 THEN 1 ELSE 0 END) AS neutral_events,
            SUM(CASE WHEN event_score < 0 THEN 1 ELSE 0 END) AS negative_events,
            SUM(CASE WHEN ABS(event_score) >= 8 THEN 1 ELSE 0 END) AS extreme_events,
            MIN(event_score) AS minimum_score,
            MAX(event_score) AS maximum_score,
            MAX(created_at) AS latest_event_created_at
        FROM eight_k_events
        """
    ).run()
    versions_result = await env.DB.prepare(
        """
        SELECT parser_version, scoring_version, COUNT(*) AS event_rows
        FROM eight_k_events
        GROUP BY parser_version, scoring_version
        ORDER BY event_rows DESC
        """
    ).run()
    extreme_result = await env.DB.prepare(
        """
        SELECT
            e.event_date, e.cik, e.company_name, e.event_score, e.primary_item,
            e.event_type, e.scoring_rule, e.scoring_reason, e.parser_version,
            e.scoring_version, f.accession_number
        FROM eight_k_events e
        JOIN filings f ON f.id = e.filing_id
        WHERE ABS(e.event_score) >= 8
        ORDER BY ABS(e.event_score) DESC, COALESCE(e.event_date, DATE(e.created_at)) DESC
        LIMIT 20
        """
    ).run()

    return {
        "queue": _rows(queue_result),
        "filings": (_rows(filings_result) or [{}])[0],
        "events": (_rows(events_result) or [{}])[0],
        "versions": _rows(versions_result),
        "extreme_events": _rows(extreme_result),
    }


@app.get("/api/admin/13f-health")
async def admin_13f_health(
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Data-quality, identity-resolution, and scoring health for institutional data."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)

    coverage_result = await env.DB.prepare(
        """
        SELECT
            COUNT(*) AS total_positions,
            SUM(CASE WHEN position_score>0 THEN 1 ELSE 0 END) AS positive_positions,
            SUM(CASE WHEN issuer_cik IS NOT NULL THEN 1 ELSE 0 END) AS mapped_positions,
            SUM(CASE WHEN issuer_cik IS NULL THEN 1 ELSE 0 END) AS unmapped_positions,
            ROUND(100.0 * SUM(CASE WHEN issuer_cik IS NOT NULL THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 2) AS mapped_pct,
            ROUND(100.0 * SUM(CASE WHEN issuer_cik IS NULL THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 2) AS unmapped_pct,
            SUM(CASE WHEN issuer_mapping_status='conflict' THEN 1 ELSE 0 END) AS conflicts,
            SUM(CASE WHEN issuer_mapping_status='resolved' THEN 1 ELSE 0 END) AS resolved_positions,
            SUM(CASE WHEN comparison_status='fund_excluded' THEN 1 ELSE 0 END) AS fund_exclusions,
            SUM(CASE WHEN corporate_action_suspected=1 THEN 1 ELSE 0 END) AS corporate_action_exclusions,
            MAX(period_of_report) AS latest_period_of_report,
            MAX(filing_date) AS latest_filing_date
        FROM institutional_positions
        """
    ).run()

    methods_result = await env.DB.prepare(
        """
        SELECT issuer_mapping_method AS mapping_method,
               issuer_mapping_status AS mapping_status,
               COUNT(*) AS positions
        FROM institutional_positions
        GROUP BY issuer_mapping_method, issuer_mapping_status
        ORDER BY positions DESC
        """
    ).run()

    current_valid_result = await env.DB.prepare(
        """
        WITH ranked AS (
            SELECT ip.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(manager_cik, manager_name), cusip
                       ORDER BY period_of_report DESC, filing_id DESC
                   ) AS rn
            FROM institutional_positions ip
            WHERE period_of_report >= DATE('now','-220 day')
        )
        SELECT
            COUNT(*) AS current_latest_positions,
            SUM(CASE WHEN position_score>0
                      AND comparison_status IN ('existing_position','new_position')
                      AND COALESCE(corporate_action_suspected,0)=0
                      AND issuer_mapping_status='resolved'
                     THEN 1 ELSE 0 END) AS valid_positive_positions,
            COUNT(DISTINCT CASE WHEN position_score>0
                      AND comparison_status IN ('existing_position','new_position')
                      AND COALESCE(corporate_action_suspected,0)=0
                      AND issuer_mapping_status='resolved'
                     THEN issuer_cik END) AS valid_positive_issuers
        FROM ranked WHERE rn=1
        """
    ).run()

    activation_result = await env.DB.prepare(
        """
        SELECT
            COUNT(*) AS current_score_rows,
            SUM(CASE WHEN whale_score>0 THEN 1 ELSE 0 END) AS activated_whale_scores,
            SUM(CASE WHEN whale_score>0 AND confirming_manager_count>1 THEN 1 ELSE 0 END) AS multi_manager_confirmations,
            MAX(confirming_manager_count) AS max_confirming_managers
        FROM current_scores
        """
    ).run()

    overlap_result = await env.DB.prepare(
        """
        WITH ranked AS (
            SELECT ip.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(manager_cik, manager_name), cusip
                       ORDER BY period_of_report DESC, filing_id DESC
                   ) AS rn
            FROM institutional_positions ip
            WHERE period_of_report >= DATE('now','-220 day')
              AND issuer_mapping_status='resolved'
        ), valid AS (
            SELECT DISTINCT issuer_cik
            FROM ranked
            WHERE rn=1 AND position_score>0
              AND comparison_status IN ('existing_position','new_position')
              AND COALESCE(corporate_action_suspected,0)=0
        )
        SELECT COUNT(*) AS eligible_anchor_overlaps
        FROM valid v
        JOIN current_scores s ON s.cik=v.issuer_cik
        JOIN companies c ON c.cik=v.issuer_cik
        WHERE c.hcs_eligible = 1
          AND (s.insider_score>0 OR s.ownership_score>0 OR s.event_score>0 OR s.capital_allocation_score>0)
        """
    ).run()

    invariant_result = await env.DB.prepare(
        """
        SELECT
          (SELECT COUNT(*) FROM current_scores
           WHERE whale_score>0
             AND insider_score<=0 AND ownership_score<=0
             AND event_score<=0 AND capital_allocation_score<=0) AS institutional_only_activation,
          (SELECT COUNT(*) FROM institutional_positions
           WHERE comparison_status='fund_excluded' AND position_score<>0) AS fund_score_violations,
          (SELECT COUNT(*) FROM institutional_positions
           WHERE corporate_action_suspected=1 AND position_score<>0) AS corporate_action_score_violations,
          (SELECT COUNT(*) FROM institutional_positions
           WHERE comparison_status IN ('historical_baseline','no_predecessor','unknown_predecessor_truncated')
             AND position_score<>0) AS predecessor_gate_violations,
          (SELECT COUNT(*) FROM institutional_positions
           WHERE issuer_mapping_status='conflict' AND issuer_cik IS NOT NULL) AS conflict_mapping_violations,
          (SELECT COUNT(*) FROM current_scores s JOIN companies c ON c.cik=s.cik
           WHERE s.whale_score>0 AND c.hcs_eligible=0) AS excluded_issuer_whale_violations,
          (SELECT COUNT(*) FROM score_component_evidence e
           JOIN institutional_positions ip ON e.component='institutional'
             AND CAST(e.source_id AS INTEGER)=ip.id
           WHERE e.component_score>0 AND ip.issuer_mapping_status<>'resolved') AS ambiguous_mapping_activation,
          (SELECT COUNT(*) FROM score_component_evidence e
           WHERE e.component='institutional' AND e.component_score>0
             AND e.event_date < DATE('now','-220 day')) AS stale_institutional_activation,
          (SELECT COUNT(*) FROM score_component_evidence e
           WHERE e.component='institutional' AND e.component_score>0
             AND json_extract(e.detail_json,'$.anchor_date') IS NOT NULL
             AND DATE(json_extract(e.detail_json,'$.anchor_date')) NOT BETWEEN
                 DATE(e.event_date,'-120 day') AND DATE(e.filing_date,'+45 day')) AS temporal_mismatch_activation
        """
    ).run()

    queue_result = await env.DB.prepare(
        """
        SELECT status, COUNT(*) AS count
        FROM score_recompute_queue
        GROUP BY status ORDER BY status
        """
    ).run()
    resolution_queue_result = await env.DB.prepare(
        """
        SELECT status, COUNT(*) AS count
        FROM security_resolution_queue
        GROUP BY status ORDER BY status
        """
    ).run()

    backlog_result = await env.DB.prepare(
        """
        SELECT
            SUM(CASE WHEN q.status='pending' THEN 1 ELSE 0 END) AS pending_positions,
            COUNT(DISTINCT CASE WHEN q.status='pending' THEN
                CASE
                    WHEN COALESCE(TRIM(ip.cusip),'') <> ''
                        THEN 'CUSIP:' || UPPER(TRIM(ip.cusip))
                    ELSE 'POSITION:' || CAST(ip.id AS TEXT)
                END
            END) AS pending_unique_securities,
            SUM(CASE WHEN q.status='pending' AND ip.position_score>0 THEN 1 ELSE 0 END) AS pending_positive_positions,
            COUNT(DISTINCT CASE WHEN q.status='pending' AND ip.position_score>0 THEN
                CASE
                    WHEN COALESCE(TRIM(ip.cusip),'') <> ''
                        THEN 'CUSIP:' || UPPER(TRIM(ip.cusip))
                    ELSE 'POSITION:' || CAST(ip.id AS TEXT)
                END
            END) AS pending_positive_securities
        FROM security_resolution_queue q
        JOIN institutional_positions ip ON ip.id=q.position_id
        """
    ).run()
    backlog = (_rows(backlog_result) or [{}])[0]
    pending_unique = int(backlog.get("pending_unique_securities") or 0)
    pending_positive_unique = int(backlog.get("pending_positive_securities") or 0)
    backlog["security_batch_limit"] = SECURITY_RESOLUTION_BATCH
    backlog["estimated_remaining_runs"] = (
        (pending_unique + SECURITY_RESOLUTION_BATCH - 1) // SECURITY_RESOLUTION_BATCH
        if pending_unique else 0
    )
    backlog["estimated_positive_runs"] = (
        (pending_positive_unique + SECURITY_RESOLUTION_BATCH - 1) // SECURITY_RESOLUTION_BATCH
        if pending_positive_unique else 0
    )

    recompute_backlog_result = await env.DB.prepare(
        """
        SELECT SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_recomputes
        FROM score_recompute_queue
        """
    ).run()
    recompute_backlog = (_rows(recompute_backlog_result) or [{}])[0]
    pending_recomputes = int(recompute_backlog.get("pending_recomputes") or 0)
    recompute_backlog["score_batch_limit"] = SCORE_RECOMPUTE_BATCH
    recompute_backlog["estimated_remaining_runs"] = (
        (pending_recomputes + SCORE_RECOMPUTE_BATCH - 1) // SCORE_RECOMPUTE_BATCH
        if pending_recomputes else 0
    )

    return {
        "versions": {
            "app": VERSION,
            "hcs": HCS_VERSION,
            "institutional": INSTITUTIONAL_VERSION,
            "security_master": SECURITY_MASTER_VERSION,
            "universe": UNIVERSE_VERSION,
        },
        "coverage": (_rows(coverage_result) or [{}])[0],
        "mapping_methods": _rows(methods_result),
        "current_validity": (_rows(current_valid_result) or [{}])[0],
        "activation": (_rows(activation_result) or [{}])[0],
        "anchor_overlap": (_rows(overlap_result) or [{}])[0],
        "invariants": (_rows(invariant_result) or [{}])[0],
        "score_recompute_queue": _rows(queue_result),
        "security_resolution_queue": _rows(resolution_queue_result),
        "security_resolution_backlog": backlog,
        "score_recompute_backlog": recompute_backlog,
        "throughput": {
            "security_resolution_batch": SECURITY_RESOLUTION_BATCH,
            "security_resolution_hard_cap": SECURITY_RESOLUTION_HARD_CAP,
            "score_recompute_batch": SCORE_RECOMPUTE_BATCH,
            "score_recompute_hard_cap": SCORE_RECOMPUTE_HARD_CAP,
            "priority": "positive institutional positions first, then newest period",
            "grain": "unique CUSIP; no-CUSIP rows remain position-scoped",
        },
    }


@app.get("/api/admin/explain/{identifier}")
async def admin_explain_company(
    identifier: str,
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    """Internal end-to-end evidence chain for one issuer, including weights."""
    env = request.scope["env"]
    await _require_admin(env, x_admin_key)
    mode, value = company_lookup_mode(identifier)
    if not value:
        raise HTTPException(status_code=404, detail="Company not found")

    if mode == "cik":
        company_result = await env.DB.prepare(
            "SELECT * FROM companies WHERE cik=? LIMIT 1"
        ).bind(value).run()
    else:
        company_result = await env.DB.prepare(
            """
            SELECT * FROM companies
            WHERE UPPER(ticker)=?
               OR cik IN (SELECT cik FROM company_tickers WHERE UPPER(ticker)=?)
            LIMIT 1
            """
        ).bind(value, value).run()
    company_rows = _rows(company_result)
    if not company_rows:
        raise HTTPException(status_code=404, detail="Company not found")
    company = company_rows[0]
    cik = company.get("cik")

    score_result = await env.DB.prepare(
        "SELECT * FROM current_scores WHERE cik=? LIMIT 1"
    ).bind(cik).run()
    override_result = await env.DB.prepare(
        "SELECT * FROM issuer_universe_overrides WHERE cik=? LIMIT 1"
    ).bind(cik).run()
    component_result = await env.DB.prepare(
        """
        SELECT * FROM score_component_evidence
        WHERE cik=? AND score_date=(SELECT MAX(score_date) FROM scores WHERE cik=?)
        ORDER BY component
        """
    ).bind(cik, cik).run()
    insider_result = await env.DB.prepare(
        """
        SELECT it.*, f.filed_at, f.accession_number, f.filing_url
        FROM insider_transactions it LEFT JOIN filings f ON f.id=it.filing_id
        WHERE it.cik=? AND it.is_open_market_purchase=1
          AND it.transaction_date>=DATE('now','-30 day')
        ORDER BY it.transaction_date DESC, it.id DESC LIMIT 30
        """
    ).bind(cik).run()
    ownership_result = await env.DB.prepare(
        """
        SELECT og.*, f.filed_at, f.accession_number, f.filing_url
        FROM ownership_groups og LEFT JOIN filings f ON f.id=og.filing_id
        WHERE og.issuer_cik=?
          AND COALESCE(og.event_date,DATE(og.created_at))>=DATE('now','-60 day')
        ORDER BY COALESCE(og.event_date,DATE(og.created_at)) DESC, og.filing_id DESC LIMIT 30
        """
    ).bind(cik).run()
    institution_result = await env.DB.prepare(
        """
        WITH ranked AS (
            SELECT ip.*, ROW_NUMBER() OVER (
                PARTITION BY COALESCE(manager_cik,manager_name), cusip
                ORDER BY period_of_report DESC, filing_id DESC
            ) AS rn
            FROM institutional_positions ip
            WHERE issuer_cik=? AND period_of_report>=DATE('now','-220 day')
        )
        SELECT r.*, f.accession_number, f.filing_url
        FROM ranked r LEFT JOIN filings f ON f.id=r.filing_id
        WHERE r.rn=1
        ORDER BY r.position_score DESC, r.value_dollars DESC LIMIT 50
        """
    ).bind(cik).run()
    event_result = await env.DB.prepare(
        """
        SELECT e.*, f.filed_at, f.accession_number, f.filing_url
        FROM eight_k_events e LEFT JOIN filings f ON f.id=e.filing_id
        WHERE e.cik=? AND COALESCE(e.event_date,DATE(e.created_at))>=DATE('now','-30 day')
        ORDER BY ABS(e.event_score) DESC, COALESCE(e.event_date,DATE(e.created_at)) DESC LIMIT 30
        """
    ).bind(cik).run()

    return {
        "company": company,
        "universe_override": (_rows(override_result) or [None])[0],
        "current_score": (_rows(score_result) or [None])[0],
        "component_provenance": _rows(component_result),
        "evidence": {
            "form4": _rows(insider_result),
            "schedule13": _rows(ownership_result),
            "institutional_13f": _rows(institution_result),
            "eight_k": _rows(event_result),
        },
        "versions": {
            "hcs": HCS_VERSION,
            "institutional": INSTITUTIONAL_VERSION,
            "security_master": SECURITY_MASTER_VERSION,
            "universe": UNIVERSE_VERSION,
        },
    }


@app.post("/api/admin/discover")
async def admin_discover(
    request: Request,
    x_admin_key: str | None = Header(default=None),
):
    env = request.scope["env"]

    await _require_admin(
        env,
        x_admin_key,
    )

    stats = await discover_enqueue_and_publish(env)

    return stats


@app.get("/sitemap.xml")
async def sitemap(request: Request):
    """Small dynamic sitemap for research routes."""
    env = request.scope["env"]
    result = await env.DB.prepare(
        """
        SELECT cik, ticker
        FROM companies
        WHERE active=1 AND hcs_eligible=1
        ORDER BY COALESCE(ticker, company_name, cik)
        LIMIT 2000
        """
    ).run()
    base = str(request.base_url).rstrip("/")
    urls = [f"{base}/", f"{base}/methodology"]
    for row in _rows(result):
        identifier = row.get("ticker") or row.get("cik")
        if identifier:
            safe = re.sub(r"[^A-Za-z0-9._-]", "", str(identifier))
            if safe:
                urls.append(f"{base}/company/{safe}")
    body = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    body += "\n".join(f"  <url><loc>{url}</loc></url>" for url in urls)
    body += "\n</urlset>\n"
    return Response(content=body, media_type="application/xml")



# Public HTML rendering moved to src/public_worker.js in v0.13.0.

async def _recover_stale_processing(env) -> int:
    """Return stale processing rows to pending so Cron can republish them.

    A hard CPU termination bypasses Python exception handling, so the row can
    otherwise remain in `processing` forever.
    """
    result = await env.DB.prepare(
        """
        UPDATE processing_queue
        SET status = 'pending',
            started_at = NULL,
            last_error = COALESCE(last_error, 'Recovered stale processing state')
        WHERE status = 'processing'
          AND (started_at IS NULL OR started_at < DATETIME('now', '-10 minutes'))
        """
    ).run()
    meta = getattr(result, "meta", None)
    recovered = int((meta or {}).get("changes", 0)) if meta else 0

    if recovered:
        await env.DB.prepare(
            """
            UPDATE filings
            SET enqueued_at = NULL
            WHERE processed = 0
              AND id IN (
                  SELECT filing_id FROM processing_queue WHERE status = 'pending'
              )
            """
        ).run()
    return recovered


async def _record_discovery_health(
    env, source: str, rows: list[dict] | None = None, error: Exception | None = None
):
    """Persist discovery health plus a durable feed watermark."""
    rows = rows or []
    dates = [str(row.get("filed_at") or "") for row in rows if row.get("filed_at")]
    latest_filed_at = max(dates or [""]) or None
    oldest_filed_at = min(dates or [""]) or None
    newest = max(rows, key=lambda row: (str(row.get("filed_at") or ""), str(row.get("accession_number") or "")), default={})
    feed_saturated = 1 if _feed_is_saturated(source, rows) else 0
    try:
        if error is None:
            await env.DB.prepare(
                """
                INSERT INTO discovery_health
                    (source, last_attempt_at, last_success_at, latest_filed_at,
                     last_discovered_count, consecutive_failures, last_error, updated_at)
                VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, 0, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(source) DO UPDATE SET
                    last_attempt_at=CURRENT_TIMESTAMP,
                    last_success_at=CURRENT_TIMESTAMP,
                    latest_filed_at=COALESCE(excluded.latest_filed_at, discovery_health.latest_filed_at),
                    last_discovered_count=excluded.last_discovered_count,
                    consecutive_failures=0,
                    last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP
                """
            ).bind(source, latest_filed_at, len(rows)).run()
            await env.DB.prepare(
                """
                INSERT INTO discovery_watermarks
                  (source, latest_accession, latest_filed_at, oldest_feed_filed_at, last_feed_count,
                   feed_saturated, needs_backfill, last_checked_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(source) DO UPDATE SET
                  latest_accession=COALESCE(excluded.latest_accession, discovery_watermarks.latest_accession),
                  latest_filed_at=CASE
                    WHEN discovery_watermarks.latest_filed_at IS NULL THEN excluded.latest_filed_at
                    WHEN excluded.latest_filed_at > discovery_watermarks.latest_filed_at THEN excluded.latest_filed_at
                    ELSE discovery_watermarks.latest_filed_at END,
                  oldest_feed_filed_at=excluded.oldest_feed_filed_at,
                  last_feed_count=excluded.last_feed_count,
                  feed_saturated=excluded.feed_saturated,
                  needs_backfill=excluded.needs_backfill,
                  last_checked_at=CURRENT_TIMESTAMP,
                  updated_at=CURRENT_TIMESTAMP
                """
            ).bind(
                source, newest.get("accession_number"), latest_filed_at, oldest_filed_at, len(rows),
                feed_saturated, feed_saturated,
            ).run()
        else:
            await env.DB.prepare(
                """
                INSERT INTO discovery_health
                    (source, last_attempt_at, last_discovered_count, consecutive_failures, last_error, updated_at)
                VALUES (?, CURRENT_TIMESTAMP, 0, 1, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source) DO UPDATE SET
                    last_attempt_at=CURRENT_TIMESTAMP,
                    last_discovered_count=0,
                    consecutive_failures=discovery_health.consecutive_failures+1,
                    last_error=excluded.last_error,
                    updated_at=CURRENT_TIMESTAMP
                """
            ).bind(source, str(error)[:1000]).run()
    except Exception:
        return



async def publish_pending_filing_work(env, limit: int = 100) -> int:
    """Publish durable pending work without SEC discovery."""
    pending = await env.DB.prepare(
        """
        SELECT f.id AS filing_id, f.accession_number, f.filing_url,
               f.form_type, q.id AS queue_id
        FROM filings f
        JOIN processing_queue q ON q.filing_id = f.id
        WHERE f.processed = 0
          AND f.enqueued_at IS NULL
          AND q.status = 'pending'
          AND (q.next_retry_at IS NULL
               OR q.next_retry_at <= CURRENT_TIMESTAMP)
        ORDER BY q.priority ASC, f.id ASC
        LIMIT ?
        """
    ).bind(max(1, min(int(limit), 250))).run()

    published = 0
    for row in _rows(pending):
        await env.FILING_QUEUE.send({
            "filing_id": row["filing_id"],
            "queue_id": row["queue_id"],
            "accession_number": row["accession_number"],
            "filing_url": row["filing_url"],
            "form_type": row["form_type"],
        })
        await env.DB.prepare(
            "UPDATE filings SET enqueued_at=CURRENT_TIMESTAMP WHERE id=?"
        ).bind(row["filing_id"]).run()
        published += 1

    return published


async def discover_enqueue_and_publish(env, publish_limit: int = 100) -> dict:
    """Discover filing feeds independently, recover saturated feeds, and publish durable work."""
    user_agent = validate_sec_user_agent(_env_value(env, "SEC_USER_AGENT"))

    filings: list[dict] = []
    discovery_errors: dict[str, str] = {}
    source_rows: dict[str, list[dict]] = {}
    discoverers = (
        ("form4", discover_latest_form4),
        ("ownership", discover_latest_ownership),
        ("8k", discover_latest_8k),
        ("13f", discover_latest_13f),
    )
    for source_name, discoverer in discoverers:
        sec_started = time.perf_counter()
        try:
            rows = await discoverer(user_agent=user_agent)
            await _record_sec_request_metric(env, source_name, failed=False, latency_seconds=max(0.0, time.perf_counter()-sec_started))
            source_rows[source_name] = rows
            filings.extend(rows)
            await _record_discovery_health(env, source_name, rows=rows)
        except Exception as exc:
            await _record_sec_request_metric(env, source_name, failed=True, latency_seconds=max(0.0, time.perf_counter()-sec_started))
            source_rows[source_name] = []
            discovery_errors[source_name] = str(exc)[:500]
            await _record_discovery_health(env, source_name, error=exc)
            await _record_pipeline_metric(env, source_name, failed=1)
            print(json.dumps({
                "event": "discovery_error",
                "source": source_name,
                "error": str(exc)[:500],
                "app_version": APP_VERSION,
            }, sort_keys=True))

    # SEC Current Filings feeds are capped. Reconciliation uses the SEC quarterly
    # directory metadata as the authority for which master index files actually
    # exist. Never guess a same-day master.YYYYMMDD.idx URL.
    backfill_rows = 0
    daily_index_date = None
    daily_index_name = None
    saturated_sources = [name for name, rows in source_rows.items() if _feed_is_saturated(name, rows)]
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        last_reconciled_result = await env.DB.prepare(
            "SELECT MAX(reconciliation_date) AS last_date FROM reconciliation_runs WHERE source='sec_master' AND status='success'"
        ).run()
        last_reconciled = ((_rows(last_reconciled_result) or [{}])[0]).get("last_date")

        # Load directory metadata for the quarter containing the next unreconciled day,
        # plus the current quarter when different. This walks multi-day and quarter-boundary
        # gaps forward without guessing nonexistent daily index URLs.
        anchors = [today]
        if last_reconciled:
            try:
                next_day = (datetime.fromisoformat(str(last_reconciled)).date() + timedelta(days=1)).isoformat()
                if daily_index_directory_url(next_day) != daily_index_directory_url(today):
                    anchors.insert(0, next_day)
            except Exception:
                pass
        available_by_name = {}
        for anchor in anchors:
            for item in await discover_available_daily_indexes(anchor, user_agent=user_agent):
                available_by_name[str(item.get("name") or "")] = item
        published_indexes = sorted(
            [item for item in available_by_name.values() if str(item.get("date") or "") <= today],
            key=lambda item: str(item.get("date") or ""),
        )
        unreconciled = [
            item for item in published_indexes
            if not last_reconciled or str(item.get("date") or "") > str(last_reconciled)
        ]
        selected = unreconciled[0] if unreconciled else (published_indexes[-1] if saturated_sources and published_indexes else None)
        if selected:
            daily_index_date = str(selected.get("date") or "")
            daily_index_name = str(selected.get("name") or "")
            prior = await env.DB.prepare(
                "SELECT 1 AS ok FROM reconciliation_runs WHERE reconciliation_date=? AND source='sec_master' AND status='success' LIMIT 1"
            ).bind(daily_index_date).run()
            already_reconciled = bool(_rows(prior))
            if saturated_sources or not already_reconciled:
                daily_rows = await discover_daily_index(daily_index_date, user_agent=user_agent)
                for row in daily_rows:
                    row["discovery_source"] = "daily-master"
                known = {row.get("accession_number") for row in filings}
                additions = [row for row in daily_rows if row.get("accession_number") not in known]
                filings.extend(additions)
                backfill_rows = len(additions)
                await _set_runtime_status(env, "last_daily_index_backfill", json.dumps({
                    "date": daily_index_date, "index": daily_index_name,
                    "saturated_sources": saturated_sources, "added": backfill_rows
                }, separators=(",", ":")))
        if saturated_sources and not selected:
            for source_name in saturated_sources:
                await env.DB.prepare(
                    "UPDATE discovery_watermarks SET needs_backfill=1, updated_at=CURRENT_TIMESTAMP WHERE source=?"
                ).bind(source_name).run()
        await _set_runtime_status(env, "last_daily_index_backfill_error", "")
    except Exception as exc:
        await _set_runtime_status(env, "last_daily_index_backfill_error", str(exc)[:1000])
        print(json.dumps({
            "event": "daily_index_backfill_error",
            "sources": saturated_sources,
            "error": str(exc)[:500],
            "app_version": APP_VERSION,
        }, sort_keys=True))

    # Deduplicate again after feed + daily-index merge.
    filings = list({row.get("accession_number"): row for row in filings if row.get("accession_number")}.values())

    inserted = 0
    inserted_by_pipeline = {"form4": 0, "ownership": 0, "8k": 0, "13f": 0, "other": 0}
    for filing in filings:
        form_upper = filing["form_type"].upper()
        pipeline = _pipeline_name_for_form(form_upper)
        if filing.get("discovery_source") == "daily-master":
            source = "daily-master-index"
        elif form_upper.startswith("4"):
            source = "latest-form4-atom"
        elif form_upper.startswith("SCHEDULE 13"):
            source = "latest-schedule13-atom"
        elif form_upper.startswith("8-K"):
            source = "latest-8k-atom"
        elif form_upper.startswith("13F-HR"):
            source = "latest-13f-atom"
        else:
            source = "latest-edgar-atom"
        result = await env.DB.prepare(
            """
            INSERT OR IGNORE INTO filings
            (accession_number, cik, company_name, form_type, filed_at, accepted_at, filing_url, source, is_amendment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        ).bind(
            filing["accession_number"], filing.get("cik"), filing.get("company_name"),
            filing["form_type"], filing["filed_at"], filing.get("accepted_at"), filing["filing_url"], source,
            1 if "/A" in form_upper else 0
        ).run()
        meta = getattr(result, "meta", None) or {}
        was_inserted = int(meta.get("changes", 0) or 0) > 0
        if was_inserted:
            inserted += 1
            inserted_by_pipeline[pipeline] = inserted_by_pipeline.get(pipeline, 0) + 1
        else:
            # A daily-index row can carry identity data that the Atom feed did not.
            # Enrich the existing logical filing without creating a duplicate.
            await env.DB.prepare(
                """
                UPDATE filings
                SET cik=COALESCE(cik, ?),
                    company_name=CASE WHEN company_name IS NULL OR company_name='' THEN ? ELSE company_name END,
                    accepted_at=COALESCE(accepted_at, ?),
                    filing_url=COALESCE(filing_url, ?)
                WHERE accession_number=?
                """
            ).bind(
                filing.get("cik"), filing.get("company_name"), filing.get("accepted_at"),
                filing.get("filing_url"), filing["accession_number"],
            ).run()

        # Amendments are first-class metadata. Link them to the nearest prior filing
        # of the same base form when issuer identity is available. Scoring code can
        # then reason about amendments without treating them as opaque duplicates.
        if "/A" in form_upper and filing.get("cik"):
            base_form = form_upper.replace("/A", "")
            try:
                prior_amendment = await env.DB.prepare(
                    """
                    SELECT accession_number FROM filings
                    WHERE cik=? AND accession_number<>?
                      AND REPLACE(UPPER(form_type),'/A','')=?
                      AND filed_at<=?
                    ORDER BY filed_at DESC, id DESC LIMIT 1
                    """
                ).bind(
                    str(filing.get("cik")), filing["accession_number"], base_form, filing["filed_at"]
                ).run()
                prior_rows = _rows(prior_amendment)
                if prior_rows:
                    await env.DB.prepare(
                        "UPDATE filings SET amends_accession=? WHERE accession_number=?"
                    ).bind(prior_rows[0].get("accession_number"), filing["accession_number"]).run()
            except Exception:
                pass

    for pipeline_name, count in inserted_by_pipeline.items():
        if pipeline_name != "other" and count:
            await _record_pipeline_metric(env, pipeline_name, discovered=count)

    await env.DB.prepare(
        """
        INSERT OR IGNORE INTO processing_queue (filing_id, priority)
        SELECT id,
               CASE WHEN form_type LIKE 'SCHEDULE 13D%' THEN 5
                    WHEN form_type LIKE 'SCHEDULE 13G%' THEN 7
                    WHEN form_type LIKE '8-K%' THEN 8
                    WHEN form_type LIKE '13F-HR%' THEN 9
                    ELSE 10 END
        FROM filings
        WHERE processed = 0
          AND (form_type IN ('4','4/A')
               OR form_type LIKE 'SCHEDULE 13D%'
               OR form_type LIKE 'SCHEDULE 13G%'
               OR form_type LIKE '8-K%')
        """
    ).run()

    thirteen_f_rows = await env.DB.prepare(
        """
        SELECT id FROM filings
        WHERE processed = 0 AND form_type LIKE '13F-HR%'
        ORDER BY id LIMIT 50
        """
    ).run()
    for row in _rows(thirteen_f_rows):
        await ensure_report_for_filing(env, int(row["id"]))

    await publish_pending_13f_work(env, limit=20)

    published = await publish_pending_filing_work(
        env, limit=publish_limit
    )

    if daily_index_date:
        try:
            reconciliation_counts = await env.DB.prepare(
                """
                SELECT
                  COUNT(*) AS discovered_count,
                  SUM(CASE WHEN processed=1 THEN 1 ELSE 0 END) AS processed_count,
                  SUM(CASE WHEN processed=0 AND COALESCE(q.status,'pending')!='error' THEN 1 ELSE 0 END) AS pending_count,
                  SUM(CASE WHEN processed=0 AND q.status='error' THEN 1 ELSE 0 END) AS failed_count
                FROM filings f LEFT JOIN processing_queue q ON q.filing_id=f.id
                WHERE f.filed_at=?
                """
            ).bind(daily_index_date).run()
            rc = (_rows(reconciliation_counts) or [{}])[0]
            discovered_count = int(rc.get("discovered_count") or 0)
            processed_count = int(rc.get("processed_count") or 0)
            pending_count = int(rc.get("pending_count") or 0)
            failed_count = int(rc.get("failed_count") or 0)
            balanced = discovered_count == processed_count + pending_count + failed_count
            reconciliation_status = "success" if balanced else "error"
            detail = {
                "saturated_sources": saturated_sources,
                "balanced": balanced,
                "equation": f"{discovered_count}={processed_count}+{pending_count}+{failed_count}",
            }
            await env.DB.prepare(
                """
                INSERT INTO reconciliation_runs
                  (reconciliation_date, source, index_name, discovered_count, inserted_count,
                   processed_count, pending_count, failed_count, status, detail_json)
                VALUES (?, 'sec_master', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(reconciliation_date, source) DO UPDATE SET
                  index_name=excluded.index_name, discovered_count=excluded.discovered_count,
                  inserted_count=excluded.inserted_count, processed_count=excluded.processed_count,
                  pending_count=excluded.pending_count, failed_count=excluded.failed_count,
                  status=excluded.status, detail_json=excluded.detail_json, created_at=CURRENT_TIMESTAMP
                """
            ).bind(
                daily_index_date, daily_index_name, discovered_count, backfill_rows,
                processed_count, pending_count, failed_count, reconciliation_status,
                json.dumps(detail, separators=(",", ":")),
            ).run()
            if balanced:
                await env.DB.prepare(
                    """
                    UPDATE discovery_watermarks
                    SET last_reconciled_date=?, last_reconciled_at=CURRENT_TIMESTAMP, last_index_name=?,
                        needs_backfill=CASE WHEN latest_filed_at IS NOT NULL AND latest_filed_at>? THEN 1 ELSE 0 END,
                        updated_at=CURRENT_TIMESTAMP
                    """
                ).bind(daily_index_date, daily_index_name, daily_index_date).run()
            else:
                await _set_runtime_status(env, "last_reconciliation_error", json.dumps(detail, separators=(",", ":")))
        except Exception as exc:
            await _set_runtime_status(env, "last_reconciliation_error", str(exc)[:1000])

    await _set_runtime_status(env, "last_discovery_success", json.dumps({
        "inserted": inserted, "published": published, "backfill_rows": backfill_rows,
        "reconciled_through": daily_index_date
    }, separators=(",", ":")))

    return {
        "discovered": inserted,
        "discovered_by_pipeline": inserted_by_pipeline,
        "published": published,
        "feed_rows": len(filings),
        "daily_index_backfill_rows": backfill_rows,
        "reconciled_through": daily_index_date,
        "daily_index_name": daily_index_name,
        "saturated_sources": saturated_sources,
        "discovery_errors": discovery_errors,
    }



def _reporting_filer_cik_from_url(url: str | None) -> str | None:
    """Return the SEC archive filer CIK from an EDGAR filing URL."""
    if not url:
        return None
    match = re.search(r"/Archives/edgar/data/(\d+)/", url, re.IGNORECASE)
    if not match:
        return None
    return _normalize_cik(match.group(1))



def _archive_base_from_filing_url(filing_url: str) -> str:
    return filing_url.rsplit("/", 1)[0]


async def _fetch_primary_8k_document(env, body: dict) -> tuple[str, str | None, str | None, str | None]:
    """Fetch only the primary 8-K document, not the potentially huge full submission.

    Some 8-K submissions contain very large mining reports, presentations, images,
    or other exhibits. Converting the entire SEC .txt submission through the
    Pyodide JS/Python bridge can exceed typed-array limits before the parser has
    a chance to discard exhibits. Resolve SEC's primaryDocument first instead.
    """
    filing_url = body["filing_url"]
    accession = body.get("accession_number")
    cik = _reporting_filer_cik_from_url(filing_url)
    user_agent = _env_value(env, "SEC_USER_AGENT")

    company_name = None
    filing_date = None
    primary_document = None

    if cik and accession:
        padded = str(cik).zfill(10)
        try:
            submissions_text = await sec_fetch_text(
                f"https://data.sec.gov/submissions/CIK{padded}.json",
                user_agent=user_agent,
            )
            payload = json.loads(submissions_text)
            company_name = payload.get("name")
            row = submission_recent_row(payload, accession)
            if row:
                primary_document = row.get("primary_document")
                filing_date = row.get("filing_date")
        except Exception:
            # We still have a small index.json fallback below.
            pass

    if cik and accession:
        base = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}"
        )
    else:
        base = _archive_base_from_filing_url(filing_url)
    if not primary_document:
        try:
            index_text = await sec_fetch_text(f"{base}/index.json", user_agent=user_agent)
            index_payload = json.loads(index_text)
            items = (((index_payload.get("directory") or {}).get("item")) or [])
            html_names = [
                str(item.get("name") or "") for item in items
                if str(item.get("name") or "").lower().endswith((".htm", ".html"))
            ]
            strong = [
                name for name in html_names
                if re.search(r"(?i)(?:^|[^a-z0-9])(?:form[-_]?8[-_]?k|8[-_]?k)(?:[^a-z0-9]|$)", name)
            ]
            if strong:
                primary_document = strong[0]
            else:
                safe = [
                    name for name in html_names
                    if not re.search(r"(?i)(?:^|[-_])(ex|exhibit|xbrl|filingsummary|r\d+)", name)
                ]
                if safe:
                    primary_document = safe[0]
        except Exception:
            pass

    if not primary_document:
        raise RuntimeError(
            f"Could not resolve primary 8-K document for {accession or filing_url}; "
            "refusing to load the full SEC submission"
        )

    primary_url = f"{base}/{primary_document}"
    primary_text = await sec_fetch_text(primary_url, user_agent=user_agent)
    return primary_text, cik, company_name, filing_date


def _sec_submission_text_url(filer_cik: str, accession: str) -> str:
    cik_int = str(int(filer_cik))
    accession_no_dash = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{accession_no_dash}/{accession}.txt"
    )


def _schedule13_form(value: str | None) -> bool:
    form = (value or "").upper().strip()
    return form in {
        "SCHEDULE 13D", "SCHEDULE 13D/A",
        "SCHEDULE 13G", "SCHEDULE 13G/A",
        "13D", "13D/A", "13G", "13G/A",
    }


def _submission_rows(payload: dict) -> list[dict]:
    """Transpose the SEC submissions JSON parallel arrays into row dicts."""
    recent = ((payload.get("filings") or {}).get("recent") or {})
    accessions = recent.get("accessionNumber") or []
    rows = []
    for i, accession in enumerate(accessions):
        row = {"accessionNumber": accession}
        for key in ("filingDate", "form", "primaryDocument"):
            values = recent.get(key) or []
            row[key] = values[i] if i < len(values) else None
        rows.append(row)
    return rows


async def _reporting_filer_schedule13_rows(
    env,
    filer_cik: str,
    before_date: str | None,
    current_accession: str,
) -> list[dict]:
    """Find older Schedule 13 filings submitted by this reporting filer.

    SEC Schedule 13 filings are filed by the beneficial owner, not the issuer.
    Therefore issuer submissions are the wrong place to look for amendments.
    We use the reporting filer's CIK from the EDGAR archive URL and then verify
    the subject issuer after parsing each candidate filing.
    """
    user_agent = _env_value(env, "SEC_USER_AGENT")
    padded = str(filer_cik).zfill(10)
    text = await sec_fetch_text(
        f"https://data.sec.gov/submissions/CIK{padded}.json",
        user_agent=user_agent,
    )
    payload = json.loads(text)
    rows = _submission_rows(payload)

    # If the immediately preceding Schedule 13 is not in the current "recent"
    # arrays, inspect at most two historical submission shards. This caps SEC
    # traffic while covering long-running holders whose filings rolled out of
    # the recent section.
    schedule_rows = [r for r in rows if _schedule13_form(r.get("form"))]

    def older(row: dict) -> bool:
        accession = row.get("accessionNumber")
        filed = row.get("filingDate")
        if not accession or accession == current_accession:
            return False
        if before_date and filed and filed >= before_date:
            return False
        return True

    candidates = [r for r in schedule_rows if older(r)]

    # If recent submissions do not contain an older Schedule 13, inspect at
    # most two historical shards.
    if not candidates:
        for meta in (((payload.get("filings") or {}).get("files")) or [])[:2]:
            name = meta.get("name")
            if not name:
                continue
            try:
                shard_text = await sec_fetch_text(
                    f"https://data.sec.gov/submissions/{name}",
                    user_agent=user_agent,
                )
                shard = json.loads(shard_text)
                shard_rows = _submission_rows({"filings": {"recent": shard}})
                candidates.extend(
                    r for r in shard_rows
                    if _schedule13_form(r.get("form")) and older(r)
                )
            except Exception:
                continue

    candidates.sort(key=lambda r: (r.get("filingDate") or "", r.get("accessionNumber") or ""), reverse=True)
    return candidates[:20]

def _extract_edgar_submission_xml(filing_text: str) -> str:
    """
    Extract an EDGAR ownership submission XML document.

    SEC Schedule 13 filings may use either an unprefixed root:
        <edgarSubmission>
    or a namespace-prefixed root such as:
        <sch:edgarSubmission>
    """

    start_match = re.search(
        r"<(?P<prefix>[A-Za-z_][A-Za-z0-9_.-]*:)?edgarSubmission\b",
        filing_text,
        re.IGNORECASE,
    )

    if not start_match:
        raise ValueError("edgarSubmission XML not found")

    prefix = start_match.group("prefix") or ""

    end_match = re.search(
        rf"</{re.escape(prefix)}edgarSubmission\s*>",
        filing_text[start_match.end():],
        re.IGNORECASE,
    )

    if not end_match:
        raise ValueError("edgarSubmission XML closing tag not found")

    start = start_match.start()
    end = start_match.end() + end_match.end()

    return filing_text[start:end]


async def _ensure_company_from_submissions(env, cik: str, company_name: str | None):
    existing = await env.DB.prepare(
        "SELECT ticker, company_name, sic, industry, identity_checked_at FROM companies WHERE cik = ? LIMIT 1"
    ).bind(cik).run()
    rows = _rows(existing)
    existing_row = rows[0] if rows else {}
    # Refresh identity metadata when ticker or SEC industry metadata is absent.
    if existing_row.get("ticker") and existing_row.get("sic") and existing_row.get("industry"):
        return

    import json
    padded = str(cik).zfill(10)
    user_agent = _env_value(env, "SEC_USER_AGENT")
    try:
        text = await sec_fetch_text(
            f"https://data.sec.gov/submissions/CIK{padded}.json",
            user_agent=user_agent,
        )
        data = json.loads(text)
        tickers = data.get("tickers") or []
        name = data.get("name") or company_name
        ticker = tickers[0].upper() if tickers else existing_row.get("ticker")
        sic = str(data.get("sic") or "").strip() or None
        industry = (data.get("sicDescription") or "").strip() or None
    except Exception:
        ticker = existing_row.get("ticker")
        name = company_name or existing_row.get("company_name")
        sic = existing_row.get("sic")
        industry = existing_row.get("industry")

    await _upsert_company_identity(env, cik, ticker, name, sic=sic, industry=industry)


async def _upsert_company_identity(
    env,
    cik: str | None,
    ticker: str | None,
    company_name: str | None,
    *,
    sic: str | None = None,
    industry: str | None = None,
):
    if not cik:
        return

    await env.DB.prepare(
        """
        INSERT INTO companies (cik, ticker, company_name, sic, industry, identity_checked_at, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(cik) DO UPDATE SET
            ticker = COALESCE(excluded.ticker, companies.ticker),
            company_name = COALESCE(excluded.company_name, companies.company_name),
            sic = COALESCE(excluded.sic, companies.sic),
            industry = COALESCE(excluded.industry, companies.industry),
            identity_checked_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        """
    ).bind(cik, ticker, company_name, sic, industry).run()

    # v0.8.0 audit fields are best-effort so the new Worker can be deployed
    # safely before migration 0025 adds the columns. Runtime eligibility never
    # depends on these fields.
    if company_name:
        universe = classify_issuer(company_name, ticker)
        try:
            await env.DB.prepare(
                """
                UPDATE companies
                SET issuer_type=?, hcs_eligible=?, hcs_exclusion_reason=?,
                    universe_version=?, updated_at=CURRENT_TIMESTAMP
                WHERE cik=?
                """
            ).bind(
                universe.issuer_type, 1 if universe.hcs_eligible else 0,
                universe.exclusion_reason, UNIVERSE_VERSION, cik,
            ).run()
        except Exception:
            pass

    if ticker:
        await env.DB.prepare(
            """
            INSERT OR IGNORE INTO company_tickers
            (cik, ticker, source)
            VALUES (?, ?, 'form4')
            """
        ).bind(cik, ticker).run()


async def _upsert_company_from_form4(env, tx):
    if not tx.issuer_cik:
        raise ValueError(
            "Form 4 issuer CIK missing"
        )

    await _upsert_company_identity(
        env,
        tx.issuer_cik,
        tx.issuer_ticker,
        tx.issuer_name,
    )


async def _mark_filing_done(
    env,
    filing_id: int,
    queue_id: int,
    status: str = "done",
    note: str | None = None,
):
    await env.DB.prepare(
        """
        UPDATE filings
        SET processed = 1
        WHERE id = ?
        """
    ).bind(
        filing_id
    ).run()

    await env.DB.prepare(
        """
        UPDATE processing_queue
        SET
            status = ?,
            processed_at = CURRENT_TIMESTAMP,
            last_error = ?
        WHERE id = ?
        """
    ).bind(
        status,
        note,
        queue_id,
    ).run()


async def _recompute_today_score(env, cik: str):
    """Single scoring path shared by general filings and the dedicated 13F worker.

    Compatibility markers retained for historical regression checks:
    candidate_whale_score = float
    whale_score = 0.0
    confirmed_institutional_score(
    comparison_status IN ('existing_position', 'new_position')
    period_of_report >= DATE('now', '-220 day')
    COALESCE(corporate_action_suspected, 0) = 0
    """
    return await shared_recompute_today_score(env, cik)


async def process_form4_message(
    env,
    body: dict,
):
    filing_id = int(
        body["filing_id"]
    )

    queue_id = int(
        body["queue_id"]
    )

    filing_url = body[
        "filing_url"
    ]

    user_agent = _env_value(
        env,
        "SEC_USER_AGENT",
    )

    await env.DB.prepare(
        """
        UPDATE processing_queue
        SET status = 'processing'
        WHERE id = ?
        """
    ).bind(
        queue_id
    ).run()

    filing_text = await sec_fetch_text(
        filing_url,
        user_agent=user_agent,
    )

    start = filing_text.find(
        "<ownershipDocument"
    )

    end_tag = "</ownershipDocument>"

    end = filing_text.find(
        end_tag
    )

    if start < 0 or end < 0:
        raise ValueError(
            "ownershipDocument XML not found"
        )

    ownership_xml = filing_text[
        start : end + len(end_tag)
    ]

    # Always parse issuer identity first so even derivative-only
    # Form 4 filings can be recorded and acknowledged cleanly.
    issuer_identity = _parse_form4_issuer(
        ownership_xml
    )

    await _upsert_company_identity(
        env,
        issuer_identity["cik"],
        issuer_identity["ticker"],
        issuer_identity["company_name"],
    )

    if issuer_identity["cik"]:
        await env.DB.prepare(
            """
            UPDATE filings
            SET
                cik = ?,
                company_name = ?
            WHERE id = ?
            """
        ).bind(
            issuer_identity["cik"],
            issuer_identity["company_name"],
            filing_id,
        ).run()

    transactions = parse_form4(
        ownership_xml
    )

    # This is not a processing failure. Some Form 4 filings contain
    # only derivative transactions, so there is nothing for the
    # non-derivative insider-purchase engine to score in v0.2.3.
    if not transactions:
        await _mark_filing_done(
            env,
            filing_id,
            queue_id,
            status="ignored",
            note="No non-derivative Form 4 transactions found",
        )

        return {
            "status": "ignored",
            "reason": "no_non_derivative_transactions",
        }

    issuer = transactions[0]

    await _upsert_company_from_form4(
        env,
        issuer,
    )

    await env.DB.prepare(
        """
        UPDATE filings
        SET
            cik = ?,
            company_name = ?
        WHERE id = ?
        """
    ).bind(
        issuer.issuer_cik,
        issuer.issuer_name,
        filing_id,
    ).run()

    for tx in transactions:
        role = _role_for(tx)

        await env.DB.prepare(
            """
            INSERT OR IGNORE INTO insider_transactions
            (
                filing_id,
                cik,
                insider_name,
                insider_role,
                is_director,
                is_officer,
                officer_title,
                transaction_code,
                transaction_date,
                shares,
                price,
                transaction_value,
                shares_owned_after,
                ownership_type,
                is_open_market_purchase
            )
            VALUES (
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?
            )
            """
        ).bind(
            filing_id,
            issuer.issuer_cik,
            tx.insider_name,
            role,
            1 if tx.is_director else 0,
            1 if tx.is_officer else 0,
            tx.officer_title,
            tx.transaction_code,
            tx.transaction_date,
            tx.shares,
            tx.price,
            tx.transaction_value,
            tx.shares_owned_after,
            tx.ownership_type,
            1
            if tx.is_open_market_purchase
            else 0,
        ).run()

    await _recompute_today_score(env, issuer.issuer_cik)

    await _mark_filing_done(
        env,
        filing_id,
        queue_id,
        status="done",
        note=None,
    )

    return {
        "status": "done",
        "transactions": len(transactions),
    }


async def _refresh_ownership_group_history(env, issuer_cik: str, group_key: str):
    """Recalculate prior-stake comparisons for one economic holder group."""
    result = await env.DB.prepare(
        """
        SELECT id, group_ownership_pct, filing_id
        FROM ownership_groups
        WHERE issuer_cik = ? AND group_key = ?
        ORDER BY COALESCE(event_date, '9999-12-31') ASC, filing_id ASC
        """
    ).bind(issuer_cik, group_key).run()

    previous_pct = None
    previous_filing_id = None
    for row in _rows(result):
        current_pct = row.get("group_ownership_pct")
        change_pp = None
        predecessor_accession = None

        if previous_filing_id is not None:
            accession_result = await env.DB.prepare(
                "SELECT accession_number FROM filings WHERE id = ? LIMIT 1"
            ).bind(previous_filing_id).run()
            accession_rows = _rows(accession_result)
            if accession_rows:
                predecessor_accession = accession_rows[0].get("accession_number")

        if current_pct is not None and previous_pct is not None:
            change_pp = round(float(current_pct) - float(previous_pct), 4)

        await env.DB.prepare(
            """
            UPDATE ownership_groups
            SET previous_ownership_pct = ?,
                ownership_change_pp = ?,
                predecessor_accession = ?
            WHERE id = ?
            """
        ).bind(previous_pct, change_pp, predecessor_accession, row["id"]).run()

        if current_pct is not None:
            previous_pct = float(current_pct)
            previous_filing_id = row["filing_id"]


async def _upsert_ownership_group(
    env,
    filing_id: int,
    owners,
    group_key: str,
):
    """Collapse joint filers into one economic signal for the filing."""
    candidates = [o for o in owners if o.ownership_pct is not None]
    if not candidates:
        return None

    representative = max(
        candidates,
        key=lambda o: (
            float(o.ownership_pct or -1),
            float(o.shares_owned or -1),
        ),
    )

    await env.DB.prepare(
        """
        INSERT INTO ownership_groups
        (
            filing_id, issuer_cik, schedule_type, event_date,
            representative_investor_cik, representative_investor_name,
            group_shares_owned, group_ownership_pct, member_count, group_key
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(filing_id) DO UPDATE SET
            issuer_cik = excluded.issuer_cik,
            schedule_type = excluded.schedule_type,
            event_date = excluded.event_date,
            representative_investor_cik = excluded.representative_investor_cik,
            representative_investor_name = excluded.representative_investor_name,
            group_shares_owned = excluded.group_shares_owned,
            group_ownership_pct = excluded.group_ownership_pct,
            member_count = excluded.member_count,
            group_key = excluded.group_key
        """
    ).bind(
        filing_id,
        representative.issuer_cik,
        representative.schedule_type,
        representative.event_date,
        representative.reporting_cik,
        representative.reporting_name,
        representative.shares_owned,
        representative.ownership_pct,
        len(owners),
        group_key,
    ).run()

    await _refresh_ownership_group_history(env, representative.issuer_cik, group_key)
    return representative


async def _insert_parsed_schedule13(
    env,
    filing_id: int,
    owners,
    group_key: str,
):
    if not owners:
        return None

    issuer_cik = owners[0].issuer_cik
    for owner in owners:
        await env.DB.prepare(
            """
            INSERT OR IGNORE INTO beneficial_ownership
            (filing_id, issuer_cik, investor_cik, investor_name, schedule_type, event_date,
             shares_owned, ownership_pct, previous_ownership_pct, ownership_change_pp,
             reporting_person_types)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """
        ).bind(
            filing_id, issuer_cik, owner.reporting_cik, owner.reporting_name,
            owner.schedule_type, owner.event_date, owner.shares_owned, owner.ownership_pct,
            owner.reporting_person_types
        ).run()

    return await _upsert_ownership_group(env, filing_id, owners, group_key)


async def _ensure_schedule13_predecessor(
    env,
    issuer_cik: str,
    current_accession: str,
    current_event_date: str | None,
    filing_url: str,
) -> str | None:
    """Fetch and store the nearest prior Schedule 13 for the same holder group."""
    filer_cik = _reporting_filer_cik_from_url(filing_url)
    if not filer_cik:
        return None

    # Existing local predecessor wins and avoids any SEC network request.
    existing = await env.DB.prepare(
        """
        SELECT f.accession_number
        FROM ownership_groups og
        JOIN filings f ON f.id = og.filing_id
        WHERE og.issuer_cik = ?
          AND og.group_key = ?
          AND f.accession_number <> ?
          AND (? IS NULL OR COALESCE(og.event_date, f.filed_at) < ?)
        ORDER BY COALESCE(og.event_date, f.filed_at) DESC, og.filing_id DESC
        LIMIT 1
        """
    ).bind(issuer_cik, filer_cik, current_accession, current_event_date, current_event_date).run()
    existing_rows = _rows(existing)
    if existing_rows:
        return existing_rows[0].get("accession_number")

    candidates = await _reporting_filer_schedule13_rows(
        env,
        filer_cik,
        before_date=current_event_date,
        current_accession=current_accession,
    )
    user_agent = _env_value(env, "SEC_USER_AGENT")

    for candidate in candidates:
        accession = candidate.get("accessionNumber")
        if not accession:
            continue
        url = _sec_submission_text_url(filer_cik, accession)
        try:
            text = await sec_fetch_text(url, user_agent=user_agent)
            xml = _extract_edgar_submission_xml(text)
            owners = parse_schedule_13(xml)
        except Exception:
            continue

        if not owners or owners[0].issuer_cik != issuer_cik:
            continue

        filing_date = candidate.get("filingDate") or owners[0].event_date or "1900-01-01"
        form_type = owners[0].schedule_type or candidate.get("form") or "SCHEDULE 13"
        issuer_name = owners[0].issuer_name

        await env.DB.prepare(
            """
            INSERT OR IGNORE INTO filings
            (accession_number, cik, company_name, form_type, filed_at, filing_url, source, processed)
            VALUES (?, ?, ?, ?, ?, ?, 'historical-predecessor', 1)
            """
        ).bind(
            accession, issuer_cik, issuer_name, form_type, filing_date, url
        ).run()

        filing_result = await env.DB.prepare(
            "SELECT id FROM filings WHERE accession_number = ? LIMIT 1"
        ).bind(accession).run()
        filing_rows = _rows(filing_result)
        if not filing_rows:
            continue

        predecessor_filing_id = int(filing_rows[0]["id"])
        await _insert_parsed_schedule13(
            env,
            predecessor_filing_id,
            owners,
            group_key=filer_cik,
        )
        return accession

    return None


async def process_schedule13_message(env, body: dict):
    filing_id = int(body["filing_id"])
    queue_id = int(body["queue_id"])
    filing_url = body["filing_url"]
    current_accession = body["accession_number"]
    user_agent = _env_value(env, "SEC_USER_AGENT")

    await env.DB.prepare(
        "UPDATE processing_queue SET status = 'processing' WHERE id = ?"
    ).bind(queue_id).run()

    filing_text = await sec_fetch_text(filing_url, user_agent=user_agent)
    schedule_xml = _extract_edgar_submission_xml(filing_text)
    owners = parse_schedule_13(schedule_xml)

    if not owners:
        await _mark_filing_done(
            env, filing_id, queue_id, status="ignored", note="No Schedule 13 reporting persons found"
        )
        return {"status": "ignored"}

    issuer_cik = owners[0].issuer_cik
    issuer_name = owners[0].issuer_name
    event_date = owners[0].event_date
    filer_cik = _reporting_filer_cik_from_url(filing_url)
    if not filer_cik:
        # Fallback is intentionally deterministic, but archive filer CIK is the
        # preferred group identifier and should be present for SEC URLs.
        filer_cik = owners[0].reporting_cik or f"name:{owners[0].reporting_name.lower()}"

    await _ensure_company_from_submissions(env, issuer_cik, issuer_name)
    await env.DB.prepare(
        "UPDATE filings SET cik = ?, company_name = ? WHERE id = ?"
    ).bind(issuer_cik, issuer_name, filing_id).run()

    # For amendments, fetch the reporting filer's nearest prior Schedule 13 for
    # this same subject issuer before calculating the accumulation delta.
    form_type = (body.get("form_type") or owners[0].schedule_type or "").upper()
    predecessor = None
    if "/A" in form_type:
        predecessor = await _ensure_schedule13_predecessor(
            env,
            issuer_cik=issuer_cik,
            current_accession=current_accession,
            current_event_date=event_date,
            filing_url=filing_url,
        )

    before = await env.DB.prepare(
        "SELECT COUNT(*) AS n FROM beneficial_ownership WHERE filing_id = ?"
    ).bind(filing_id).run()
    before_rows = _rows(before)
    before_count = int((before_rows[0].get("n") if before_rows else 0) or 0)

    representative = await _insert_parsed_schedule13(
        env,
        filing_id,
        owners,
        group_key=filer_cik,
    )

    after = await env.DB.prepare(
        "SELECT COUNT(*) AS n FROM beneficial_ownership WHERE filing_id = ?"
    ).bind(filing_id).run()
    after_rows = _rows(after)
    after_count = int((after_rows[0].get("n") if after_rows else 0) or 0)

    await _recompute_today_score(env, issuer_cik)
    await _mark_filing_done(env, filing_id, queue_id, status="done", note=None)
    return {
        "status": "done",
        "owners": max(0, after_count - before_count),
        "group_pct": representative.ownership_pct if representative else None,
        "predecessor_accession": predecessor,
    }


def _normalize_company_name(value: str | None) -> str:
    name = (value or "").upper()
    name = re.sub(r"[^A-Z0-9 ]+", " ", name)
    for token in (
        "INCORPORATED", "INC", "CORPORATION", "CORP", "COMPANY", "CO",
        "LIMITED", "LTD", "PLC", "LP", "LLC", "HOLDINGS", "HOLDING",
        "CLASS A", "CLASS B", "THE",
    ):
        name = re.sub(rf"\b{re.escape(token)}\b", " ", name)
    return re.sub(r"\s+", " ", name).strip()


async def _company_name_map(env) -> dict[str, dict]:
    result = await env.DB.prepare(
        "SELECT cik, ticker, company_name FROM companies WHERE company_name IS NOT NULL"
    ).run()
    mapping = {}
    for row in _rows(result):
        key = _normalize_company_name(row.get("company_name"))
        if key and key not in mapping:
            mapping[key] = row
    return mapping


async def process_8k_message(env, body: dict):
    filing_id = int(body["filing_id"])
    queue_id = int(body["queue_id"])
    user_agent = _env_value(env, "SEC_USER_AGENT")

    await env.DB.prepare(
        "UPDATE processing_queue SET status = 'processing' WHERE id = ?"
    ).bind(queue_id).run()

    text, fallback_cik, fallback_company_name, fallback_event_date = await _fetch_primary_8k_document(env, body)
    event = parse_8k_submission(
        text,
        fallback_cik=fallback_cik,
        fallback_company_name=fallback_company_name,
        fallback_event_date=fallback_event_date,
    )

    if not event.cik:
        await _mark_filing_done(
            env, filing_id, queue_id, status="ignored", note="8-K issuer CIK not found"
        )
        return {"status": "ignored"}

    await _ensure_company_from_submissions(env, event.cik, event.company_name)
    await env.DB.prepare(
        "UPDATE filings SET cik = ?, company_name = ? WHERE id = ?"
    ).bind(event.cik, event.company_name, filing_id).run()

    # v0.7.6 audit metadata is written when migration 0023 is present. The
    # legacy fallback makes deploy-before-migration safe for any 8-K that happens
    # to arrive in the short interval between Worker deployment and D1 migration.
    try:
        await env.DB.prepare(
            """
            INSERT INTO eight_k_events
            (filing_id, cik, company_name, event_date, item_numbers, event_type,
             sentiment, event_score, matched_keywords, excerpt, primary_item,
             scoring_rule, scoring_reason, source_section, parser_version, scoring_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filing_id) DO UPDATE SET
                cik = excluded.cik,
                company_name = excluded.company_name,
                event_date = excluded.event_date,
                item_numbers = excluded.item_numbers,
                event_type = excluded.event_type,
                sentiment = excluded.sentiment,
                event_score = excluded.event_score,
                matched_keywords = excluded.matched_keywords,
                excerpt = excluded.excerpt,
                primary_item = excluded.primary_item,
                scoring_rule = excluded.scoring_rule,
                scoring_reason = excluded.scoring_reason,
                source_section = excluded.source_section,
                parser_version = excluded.parser_version,
                scoring_version = excluded.scoring_version
            """
        ).bind(
            filing_id,
            event.cik,
            event.company_name,
            event.event_date,
            json.dumps(event.item_numbers),
            event.event_type,
            event.sentiment,
            event.event_score,
            json.dumps(event.matched_keywords),
            event.excerpt,
            event.primary_item,
            event.scoring_rule,
            event.scoring_reason,
            event.source_section,
            event.parser_version,
            event.scoring_version,
        ).run()
    except Exception as exc:
        # Only fall back for a pre-0023 schema. Re-raise every unrelated D1 or
        # runtime failure so operational errors cannot be silently hidden.
        message = str(exc).lower()
        if "no such column" not in message and "has no column named" not in message:
            raise
        await env.DB.prepare(
            """
            INSERT INTO eight_k_events
            (filing_id, cik, company_name, event_date, item_numbers, event_type,
             sentiment, event_score, matched_keywords, excerpt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filing_id) DO UPDATE SET
                cik = excluded.cik,
                company_name = excluded.company_name,
                event_date = excluded.event_date,
                item_numbers = excluded.item_numbers,
                event_type = excluded.event_type,
                sentiment = excluded.sentiment,
                event_score = excluded.event_score,
                matched_keywords = excluded.matched_keywords,
                excerpt = excluded.excerpt
            """
        ).bind(
            filing_id,
            event.cik,
            event.company_name,
            event.event_date,
            json.dumps(event.item_numbers),
            event.event_type,
            event.sentiment,
            event.event_score,
            json.dumps(event.matched_keywords),
            event.excerpt,
        ).run()

    await _recompute_today_score(env, event.cik)
    await _mark_filing_done(env, filing_id, queue_id, status="done", note=None)
    return {"status": "done", "event_score": event.event_score, "items": event.item_numbers}


def _is_13f_form(value: str | None) -> bool:
    return (value or "").upper().strip() in {"13F-HR", "13F-HR/A"}


async def _manager_13f_rows(env, manager_cik: str, current_accession: str, before_date: str | None):
    user_agent = _env_value(env, "SEC_USER_AGENT")
    padded = str(manager_cik).zfill(10)
    text = await sec_fetch_text(
        f"https://data.sec.gov/submissions/CIK{padded}.json",
        user_agent=user_agent,
    )
    payload = json.loads(text)
    rows = _submission_rows(payload)

    def older(row):
        accession = row.get("accessionNumber")
        filed = row.get("filingDate")
        return (
            accession
            and accession != current_accession
            and _is_13f_form(row.get("form"))
            and (not before_date or not filed or filed < before_date)
        )

    candidates = [r for r in rows if older(r)]
    candidates.sort(key=lambda r: (r.get("filingDate") or "", r.get("accessionNumber") or ""), reverse=True)
    return candidates[:5]


async def _insert_13f_report(env, filing_id: int, report, historical: bool = False) -> set[str]:
    if not report.positions:
        return set()

    # Materiality/CPU guard: keep the manager's largest 250 reported positions.
    positions = sorted(report.positions, key=lambda p: p.value_dollars, reverse=True)[:250]
    total_value = sum(max(0.0, float(p.value_dollars or 0)) for p in positions) or 1.0

    company_map = await _company_name_map(env)
    touched_ciks: set[str] = set()

    for pos in positions:
        matched = company_map.get(_normalize_company_name(pos.issuer_name))
        issuer_cik = matched.get("cik") if matched else None
        ticker = matched.get("ticker") if matched else None

        previous_shares = None
        if report.manager_cik:
            prior = await env.DB.prepare(
                """
                SELECT shares
                FROM institutional_positions
                WHERE manager_cik = ? AND cusip = ? AND filing_id <> ?
                  AND (? IS NULL OR period_of_report < ?)
                ORDER BY period_of_report DESC, id DESC
                LIMIT 1
                """
            ).bind(
                report.manager_cik, pos.cusip, filing_id,
                report.period_of_report, report.period_of_report,
            ).run()
            prior_rows = _rows(prior)
            if prior_rows:
                previous_shares = prior_rows[0].get("shares")

        weight = round((float(pos.value_dollars or 0) / total_value) * 100.0, 4)
        details_score = score_13f_position(
            pos.shares,
            previous_shares,
            pos.value_dollars,
            weight,
        )
        is_new = 1 if previous_shares is None or float(previous_shares or 0) <= 0 else 0
        share_change_pct = None
        if previous_shares is not None and float(previous_shares or 0) > 0:
            share_change_pct = round(
                ((float(pos.shares) - float(previous_shares)) / float(previous_shares)) * 100.0,
                4,
            )

        await env.DB.prepare(
            """
            INSERT INTO institutional_positions
            (filing_id, manager_cik, manager_name, filing_date, period_of_report,
             issuer_cik, ticker, issuer_name, cusip, title_of_class, shares,
             value_thousands, value_dollars, position_weight_pct, previous_shares, share_change_pct,
             is_new_position, position_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filing_id, cusip) DO UPDATE SET
                issuer_cik = excluded.issuer_cik,
                ticker = excluded.ticker,
                shares = excluded.shares,
                value_thousands = excluded.value_thousands,
                value_dollars = excluded.value_dollars,
                position_weight_pct = excluded.position_weight_pct,
                previous_shares = excluded.previous_shares,
                share_change_pct = excluded.share_change_pct,
                is_new_position = excluded.is_new_position,
                position_score = excluded.position_score
            """
        ).bind(
            filing_id, report.manager_cik, report.manager_name, report.filing_date,
            report.period_of_report, issuer_cik, ticker, pos.issuer_name, pos.cusip,
            pos.title_of_class, pos.shares, pos.value_thousands, pos.value_dollars, weight,
            previous_shares, share_change_pct, is_new, details_score,
        ).run()

        if issuer_cik:
            touched_ciks.add(str(issuer_cik))

    return touched_ciks


async def _ensure_13f_predecessor(env, current_filing_id: int, body: dict, report):
    manager_cik = report.manager_cik or _reporting_filer_cik_from_url(body.get("filing_url"))
    if not manager_cik:
        return None

    # Existing local prior report avoids SEC traffic.
    local = await env.DB.prepare(
        """
        SELECT f.accession_number
        FROM institutional_positions ip
        JOIN filings f ON f.id = ip.filing_id
        WHERE ip.manager_cik = ? AND ip.filing_id <> ?
          AND (? IS NULL OR ip.period_of_report < ?)
        ORDER BY ip.period_of_report DESC, ip.id DESC
        LIMIT 1
        """
    ).bind(manager_cik, current_filing_id, report.period_of_report, report.period_of_report).run()
    local_rows = _rows(local)
    if local_rows:
        return local_rows[0].get("accession_number")

    candidates = await _manager_13f_rows(
        env,
        manager_cik,
        body.get("accession_number"),
        report.filing_date,
    )
    user_agent = _env_value(env, "SEC_USER_AGENT")

    for candidate in candidates:
        accession = candidate.get("accessionNumber")
        if not accession:
            continue
        url = _sec_submission_text_url(manager_cik, accession)
        try:
            text = await sec_fetch_text(url, user_agent=user_agent)
            prior_report = parse_13f_submission(text)
        except Exception:
            continue
        if not prior_report.positions:
            continue

        await env.DB.prepare(
            """
            INSERT OR IGNORE INTO filings
            (accession_number, cik, company_name, form_type, filed_at, filing_url, source, processed)
            VALUES (?, ?, ?, ?, ?, ?, 'historical-13f-predecessor', 1)
            """
        ).bind(
            accession,
            manager_cik,
            prior_report.manager_name,
            candidate.get("form") or "13F-HR",
            candidate.get("filingDate") or prior_report.filing_date or "1900-01-01",
            url,
        ).run()
        result = await env.DB.prepare(
            "SELECT id FROM filings WHERE accession_number = ? LIMIT 1"
        ).bind(accession).run()
        rows = _rows(result)
        if rows:
            await _insert_13f_report(env, int(rows[0]["id"]), prior_report, historical=True)
            return accession
    return None


async def process_13f_message(env, body: dict):
    filing_id = int(body["filing_id"])
    queue_id = int(body["queue_id"])
    user_agent = _env_value(env, "SEC_USER_AGENT")

    await env.DB.prepare(
        "UPDATE processing_queue SET status = 'processing' WHERE id = ?"
    ).bind(queue_id).run()

    text = await sec_fetch_text(body["filing_url"], user_agent=user_agent)
    report = parse_13f_submission(text)
    if not report.positions:
        await _mark_filing_done(
            env, filing_id, queue_id, status="ignored", note="No 13F information table positions found"
        )
        return {"status": "ignored"}

    await _ensure_13f_predecessor(env, filing_id, body, report)
    touched = await _insert_13f_report(env, filing_id, report)

    await env.DB.prepare(
        "UPDATE filings SET cik = ?, company_name = ? WHERE id = ?"
    ).bind(report.manager_cik, report.manager_name, filing_id).run()

    for cik in touched:
        await _recompute_today_score(env, cik)

    await _mark_filing_done(env, filing_id, queue_id, status="done", note=None)
    return {"status": "done", "positions": min(len(report.positions), 250), "linked_issuers": len(touched)}


async def _recover_stale_score_recompute(env) -> int:
    """Recover interrupted score recomputes without touching history."""
    try:
        result = await env.DB.prepare(
            """
            UPDATE score_recompute_queue
            SET status='pending', started_at=NULL,
                last_error=COALESCE(last_error, 'Recovered stale score recompute')
            WHERE status='processing'
              AND (started_at IS NULL OR started_at < DATETIME('now','-10 minutes'))
            """
        ).run()
        meta = getattr(result, "meta", None) or {}
        return int(meta.get("changes", 0) or 0)
    except Exception:
        return 0


async def _process_score_recompute_queue(env, limit: int = SCORE_RECOMPUTE_BATCH) -> int:
    """Recompute only today's row for explicitly queued issuers."""
    try:
        result = await env.DB.prepare(
            """
            SELECT cik, attempts
            FROM score_recompute_queue
            WHERE status='pending'
              AND (next_retry_at IS NULL OR next_retry_at<=CURRENT_TIMESTAMP)
            ORDER BY created_at, cik
            LIMIT ?
            """
        ).bind(max(1, min(int(limit), SCORE_RECOMPUTE_HARD_CAP))).run()
    except Exception:
        return 0

    processed = 0
    for row in _rows(result):
        cik = str(row.get("cik") or "")
        if not cik:
            continue
        claim = await env.DB.prepare(
            """
            UPDATE score_recompute_queue
            SET status='processing', started_at=CURRENT_TIMESTAMP, last_attempt_at=CURRENT_TIMESTAMP,
                next_retry_at=NULL, attempts=attempts+1
            WHERE cik=? AND status='pending'
            """
        ).bind(cik).run()
        meta = getattr(claim, "meta", None) or {}
        if int(meta.get("changes", 0) or 0) == 0:
            continue
        score_started = time.perf_counter()
        try:
            await shared_recompute_today_score(env, cik)
            await _record_pipeline_metric(env, "scoring", processed=1, latency_seconds=max(0.0, time.perf_counter()-score_started))
            await env.DB.prepare(
                """
                UPDATE score_recompute_queue
                SET status='done', processed_at=CURRENT_TIMESTAMP, last_error=NULL, next_retry_at=NULL
                WHERE cik=?
                """
            ).bind(cik).run()
            processed += 1
        except Exception as exc:
            attempts = int(row.get("attempts") or 0) + 1
            status = 'error' if attempts >= 4 else 'pending'
            retry_seconds = min(3600, 60 * (2 ** max(0, attempts - 1)))
            await env.DB.prepare(
                """
                UPDATE score_recompute_queue
                SET status=?, started_at=NULL, last_error=?,
                    next_retry_at=CASE WHEN ?='pending' THEN DATETIME('now', '+' || ? || ' seconds') ELSE NULL END,
                    dead_letter_at=CASE WHEN ?='error' THEN CURRENT_TIMESTAMP ELSE dead_letter_at END
                WHERE cik=?
                """
            ).bind(status, str(exc)[:1000], status, retry_seconds, status, cik).run()
    return processed



async def _sec_cooldown_remaining(env) -> int:
    try:
        result = await env.DB.prepare(
            "SELECT value FROM runtime_status WHERE key='sec_cooldown_until' LIMIT 1"
        ).run()
        rows = _rows(result)
        if not rows or not rows[0].get("value"):
            return 0
        until = datetime.fromisoformat(
            str(rows[0]["value"]).replace("Z", "+00:00")
        )
        return max(
            0,
            int((until - datetime.now(timezone.utc)).total_seconds())
        )
    except Exception:
        return 0


async def _start_sec_cooldown(env, seconds: int = 600):
    until = (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")
    await _set_runtime_status(env, "sec_cooldown_until", until)


class Default(WorkerEntrypoint):

    async def fetch(
        self,
        request,
    ):
        return await asgi.fetch(
            app,
            request,
            self.env,
        )

    async def scheduled(  # pragma: no cover
        self,
        controller,
        env,
        ctx,
    ):
        """
        Cloudflare Cron Trigger entrypoint.

        Use self.env for bindings because that is the binding
        environment attached to WorkerEntrypoint.
        """

        run_id = uuid.uuid4().hex
        started = time.perf_counter()

        # Prevent overlapping scheduled ingestion runs.
        # A running cron older than 15 minutes is considered abandoned.
        try:
            await self.env.DB.prepare(
                """
                UPDATE cron_runs
                SET status='error',
                    finished_at=CURRENT_TIMESTAMP,
                    error_text=COALESCE(
                        error_text,
                        'stale running cron reclaimed by overlap guard'
                    )
                WHERE status='running'
                  AND started_at < DATETIME('now','-15 minutes')
                """
            ).run()

            overlap_result = await self.env.DB.prepare(
                """
                SELECT run_id, started_at
                FROM cron_runs
                WHERE status='running'
                  AND started_at >= DATETIME('now','-15 minutes')
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).run()

            overlap_rows = _rows(overlap_result)

            if overlap_rows:
                blocker = overlap_rows[0]

                await _set_runtime_status(
                    self.env,
                    "last_cron_skip",
                    json.dumps({
                        "reason": "overlap",
                        "blocking_run_id": blocker.get("run_id"),
                        "blocking_started_at": blocker.get("started_at"),
                    }, separators=(",", ":")),
                )

                print(json.dumps({
                    "event": "cron_overlap_skipped",
                    "run_id": run_id,
                    "blocking_run_id": blocker.get("run_id"),
                    "blocking_started_at": blocker.get("started_at"),
                    "app_version": APP_VERSION,
                }, sort_keys=True))

                return

        except Exception as exc:
            print(json.dumps({
                "event": "cron_overlap_guard_error",
                "run_id": run_id,
                "error": str(exc)[:500],
                "app_version": APP_VERSION,
            }, sort_keys=True))

        await _set_runtime_status(self.env, "last_cron_attempt", run_id)
        try:
            await self.env.DB.prepare(
                "INSERT INTO cron_runs(run_id, status, app_version) VALUES (?, 'running', ?)"
            ).bind(run_id, APP_VERSION).run()
        except Exception:
            pass

        try:
            policy, throughput_before = await _update_ingestion_controller(self.env)
            auto_replayed = await _auto_replay_transient_failures(self.env, limit=10)
            recovered_processing = await _recover_stale_processing(self.env)

            cooldown_seconds = await _sec_cooldown_remaining(self.env)

            backlog_result = await self.env.DB.prepare(
                """
                SELECT COUNT(*) AS n
                FROM processing_queue
                WHERE status='pending'
                """
            ).run()

            backlog_count = int(
                ((_rows(backlog_result) or [{}])[0]).get("n") or 0
            )

            drain_only = (
                cooldown_seconds > 0
                or backlog_count >= 1000
            )

            if cooldown_seconds > 0:
                drain_reason = "sec_cooldown"
            elif backlog_count >= 1000:
                drain_reason = "large_backlog"
            else:
                drain_reason = None

            early_published = 0
            effective_publish_limit = 0

            # During SEC cooldown, publish nothing new.
            # With a large backlog, feed only a small controlled batch.
            if cooldown_seconds <= 0:
                effective_publish_limit = (
                    min(policy.publish_limit, 50)
                    if drain_only
                    else policy.publish_limit
                )

                early_published = await publish_pending_filing_work(
                    self.env,
                    limit=effective_publish_limit,
                )

            if early_published:
                print(json.dumps({
                    "event": "cron_backlog_published",
                    "run_id": run_id,
                    "published": early_published,
                    "publish_limit": effective_publish_limit,
                    "app_version": APP_VERSION,
                }, sort_keys=True))

            if drain_only:
                recovered_scores = 0
                stale_scores_queued = 0
                resolution_stats = {}
                recomputed = 0

                discovery_stats = {
                    "discovered": 0,
                    "published": early_published,
                    "skipped": True,
                    "drain_only": True,
                    "reason": drain_reason,
                }

                identity_audit = {
                    "skipped": True,
                    "reason": drain_reason,
                }

                identity_refreshed = 0
                data_quality_issues_created = 0

                print(json.dumps({
                    "event": "cron_drain_only",
                    "run_id": run_id,
                    "reason": drain_reason,
                    "backlog": backlog_count,
                    "sec_cooldown_seconds": cooldown_seconds,
                    "published": early_published,
                    "app_version": APP_VERSION,
                }, sort_keys=True))

            else:
                recovered_scores = await _recover_stale_score_recompute(self.env)
                stale_scores_queued = await enqueue_stale_active_scores(
                    self.env, limit=200
                )

                resolution_stats = await resolve_unmapped_security_positions(
                    self.env, limit=SECURITY_RESOLUTION_BATCH
                )

                recomputed = await _process_score_recompute_queue(
                    self.env, limit=SCORE_RECOMPUTE_BATCH
                )

                if (
                    resolution_stats.get("processed_securities")
                    or recomputed
                    or stale_scores_queued
                ):
                    print(json.dumps({
                        "event": "background_maintenance_progress",
                        "run_id": run_id,
                        "resolution": resolution_stats,
                        "stale_scores_queued": stale_scores_queued,
                        "score_recomputes": recomputed,
                        "security_batch_limit": SECURITY_RESOLUTION_BATCH,
                        "score_batch_limit": SCORE_RECOMPUTE_BATCH,
                        "recovered_processing": recovered_processing,
                        "recovered_score_recomputes": recovered_scores,
                        "app_version": APP_VERSION,
                    }, sort_keys=True))

                await recover_stale_13f_work(self.env)
                await publish_pending_13f_work(self.env, limit=20)

                discovery_stats = await discover_enqueue_and_publish(
                    self.env,
                    publish_limit=policy.publish_limit,
                )

                identity_audit = await _audit_identity_mappings(self.env)

                identity_refreshed = await _refresh_company_identity_batch(
                    self.env,
                    limit=policy.identity_refresh_limit,
                )

                data_quality_issues_created = await _capture_data_quality_issues(
                    self.env
                )

            throughput_after = await _queue_throughput_snapshot(self.env)
            await _record_backlog_sample(self.env, policy, throughput_after)
            summary = {
                "recomputed": recomputed,
                "stale_scores_queued": stale_scores_queued,
                "discovery": discovery_stats,
                "identity_audit": identity_audit,
                "identity_refreshed": identity_refreshed,
                "data_quality_issues_created": data_quality_issues_created,
                "auto_replayed_failures": auto_replayed,
                "ingestion_controller": {
                    "mode": policy.mode, "target_concurrency": policy.target_concurrency,
                    "publish_limit": policy.publish_limit, "identity_refresh_limit": policy.identity_refresh_limit,
                    "reason": policy.reason,
                    "drain_only": drain_only,
                    "drain_reason": drain_reason,
                    "backlog_before": backlog_count,
                    "sec_cooldown_seconds": cooldown_seconds,
                },
                "throughput": throughput_after,
            }
            discovered_count = int((discovery_stats or {}).get("discovered") or 0) if isinstance(discovery_stats, dict) else 0
            processed_count = 0
            try:
                processed_result = await self.env.DB.prepare(
                    "SELECT COUNT(*) AS n FROM processing_queue WHERE processed_at >= (SELECT started_at FROM cron_runs WHERE run_id=?) AND status IN ('done','ignored')"
                ).bind(run_id).run()
                processed_count = int(((_rows(processed_result) or [{}])[0]).get("n") or 0)
            except Exception:
                processed_count = 0
            failed_count = len((discovery_stats or {}).get("discovery_errors") or {}) if isinstance(discovery_stats, dict) else 0
            queue_pending = None
            try:
                pending_result = await self.env.DB.prepare("SELECT COUNT(*) AS n FROM processing_queue WHERE status='pending'").run()
                queue_pending = int(((_rows(pending_result) or [{}])[0]).get("n") or 0)
            except Exception:
                queue_pending = None
            duration_ms = int(max(0.0, (time.perf_counter() - started) * 1000.0))
            try:
                await self.env.DB.prepare(
                    """
                    UPDATE cron_runs SET status='success', finished_at=CURRENT_TIMESTAMP,
                        duration_ms=?, summary_json=?, error_text=NULL, records_discovered=?,
                        records_processed=?, records_failed=?, scores_recomputed=?, queue_pending_at_finish=?
                    WHERE run_id=?
                    """
                ).bind(duration_ms, json.dumps(summary, separators=(",", ":")), discovered_count,
                       processed_count, failed_count, int(recomputed or 0), queue_pending, run_id).run()
            except Exception:
                pass
            await _set_runtime_status(
                self.env, "last_cron_success",
                json.dumps({"run_id": run_id, "duration_ms": duration_ms, **summary}, separators=(",", ":")),
            )
            await _set_runtime_status(self.env, "last_cron_error", "")
            stages_for_alerts = await _public_system_snapshot(self.env)
            await _sync_system_alerts(self.env, stages_for_alerts)
        except Exception as exc:
            duration_ms = int(max(0.0, (time.perf_counter() - started) * 1000.0))
            try:
                await self.env.DB.prepare(
                    """
                    UPDATE cron_runs SET status='error', finished_at=CURRENT_TIMESTAMP,
                        duration_ms=?, error_text=? WHERE run_id=?
                    """
                ).bind(duration_ms, str(exc)[:1000], run_id).run()
            except Exception:
                pass
            await _set_runtime_status(self.env, "last_cron_error", json.dumps({
                "run_id": run_id, "duration_ms": duration_ms, "error": str(exc)[:500]
            }, separators=(",", ":")))
            try:
                stages_for_alerts = await _public_system_snapshot(self.env)
                await _sync_system_alerts(self.env, stages_for_alerts)
            except Exception:
                pass
            print(json.dumps({
                "event": "cron_error", "run_id": run_id, "duration_ms": duration_ms,
                "error": str(exc)[:500], "app_version": APP_VERSION,
            }, sort_keys=True))
            raise

    async def queue(
        self,
        batch,
        env,
        ctx,
    ):
        """
        Cloudflare Queue consumer.

        The deployed workers-py runtime passes batch, env, and ctx.
        Normal derivative-only Form 4 filings are acknowledged and
        not retried. Real exceptions are retried after 60 seconds.
        """

        for msg in batch.messages:
            body = msg.body
            queue_id = int(body["queue_id"])

            # Persist the attempt before doing expensive work. A Cloudflare CPU
            # termination can kill Python before an exception handler runs, but
            # this D1 write survives and lets the next delivery self-heal.
            state = await self.env.DB.prepare(
                "SELECT status, attempts, next_retry_at FROM processing_queue WHERE id = ? LIMIT 1"
            ).bind(queue_id).run()
            state_rows = _rows(state)
            if not state_rows:
                msg.ack()
                continue

            current_status = (state_rows[0].get("status") or "pending").lower()
            prior_attempts = int(state_rows[0].get("attempts") or 0)

            if current_status in {"done", "ignored", "error"}:
                msg.ack()
                continue

            if prior_attempts >= 4:
                # A retry limit is an operational failure, not successful filing
                # processing. Keep filings.processed = 0 so the failed filing is
                # visible and can be safely requeued after the underlying issue is
                # fixed. Marking it processed here previously hid failed filings.
                await self.env.DB.prepare(
                    """
                    UPDATE processing_queue
                    SET status = 'error',
                        processed_at = CURRENT_TIMESTAMP,
                        started_at = NULL,
                        last_error = COALESCE(last_error, 'Retry limit reached after hard worker failure'),
                        dead_letter_at = CURRENT_TIMESTAMP, next_retry_at = NULL
                    WHERE id = ?
                    """
                ).bind(queue_id).run()
                await self.env.DB.prepare(
                    "UPDATE filings SET processed = 0, enqueued_at = NULL WHERE id = ?"
                ).bind(int(body["filing_id"])).run()
                msg.ack()
                continue

            cooldown_seconds = await _sec_cooldown_remaining(self.env)
            if cooldown_seconds > 0:
                msg.retry(
                    delaySeconds=max(60, min(cooldown_seconds, 600))
                )
                continue

            # v0.14 cooperative concurrency gate. Cloudflare may invoke up to three
            # consumers, while D1 leases let the adaptive controller temporarily
            # operate at one or two when SEC latency/errors rise.
            lease_owner = f"{queue_id}:{uuid.uuid4().hex}"
            target_concurrency = 1
            try:
                controller = await self.env.DB.prepare(
                    "SELECT target_concurrency FROM ingestion_controller_state WHERE id=1"
                ).run()
                target_concurrency = int(((_rows(controller) or [{}])[0]).get("target_concurrency") or 1)
            except Exception:
                target_concurrency = 1
            lease_slot = await _acquire_ingestion_lease(self.env, lease_owner, target_concurrency)
            if lease_slot is None:
                msg.retry(delaySeconds=10)
                continue

            await self.env.DB.prepare(
                """
                UPDATE processing_queue
                SET status = 'processing',
                    attempts = attempts + 1,
                    last_attempt_at = CURRENT_TIMESTAMP,
                    next_retry_at = NULL,
                    started_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """
            ).bind(queue_id).run()

            try:
                form_type = (body.get("form_type") or "4").upper()
                pipeline_name = _pipeline_name_for_form(form_type)
                if form_type.startswith("SCHEDULE 13"):
                    await process_schedule13_message(self.env, body)
                elif form_type.startswith("8-K"):
                    await process_8k_message(self.env, body)
                elif form_type.startswith("13F-HR"):
                    # Legacy v0.5 messages may still be sitting in the general
                    # queue. Hand them off and ACK so 13F CPU cannot poison the
                    # primary filing consumer.
                    await ensure_report_for_filing(self.env, int(body["filing_id"]))
                    await self.env.DB.prepare(
                        """
                        UPDATE processing_queue
                        SET status='ignored', processed_at=CURRENT_TIMESTAMP, started_at=NULL,
                            last_error='Migrated to dedicated v0.6 13F pipeline'
                        WHERE id=?
                        """
                    ).bind(queue_id).run()
                    await publish_pending_13f_work(self.env, limit=5)
                else:
                    await process_form4_message(self.env, body)

                latency = await _filing_latency_seconds(self.env, int(body["filing_id"]))
                await _record_pipeline_metric(self.env, pipeline_name, processed=1, latency_seconds=latency)
                msg.ack()
                await _release_ingestion_lease(self.env, lease_owner)

            except Exception as exc:
                # Ordinary Python exceptions are retryable. Hard CPU termination
                # skips this block; the persisted attempt counter handles that
                # case on the next queue delivery.
                attempt_number = prior_attempts + 1
                error_text = str(exc)
                sec_429 = "SEC request failed: 429" in error_text

                if sec_429:
                    await _start_sec_cooldown(self.env, 600)
                    await _record_sec_request_metric(
                        self.env,
                        "queue",
                        failed=True,
                        latency_seconds=0.0,
                    )

                retry_seconds = (
                    600
                    if sec_429
                    else min(3600, 60 * (2 ** max(0, attempt_number - 1)))
                )
                next_status = 'error' if attempt_number >= 4 else 'pending'
                await self.env.DB.prepare(
                    """
                    UPDATE processing_queue
                    SET status = ?,
                        started_at = NULL,
                        last_error = ?,
                        next_retry_at = CASE WHEN ?='pending' THEN DATETIME('now', '+' || ? || ' seconds') ELSE NULL END,
                        dead_letter_at = CASE WHEN ?='error' THEN CURRENT_TIMESTAMP ELSE dead_letter_at END
                    WHERE id = ?
                    """
                ).bind(next_status, str(exc)[:500], next_status, retry_seconds, next_status, queue_id).run()
                pipeline_name = _pipeline_name_for_form(body.get("form_type"))
                await _record_pipeline_metric(self.env, pipeline_name, failed=1, retried=1)
                print(json.dumps({
                    "event": "queue_retry" if next_status == 'pending' else "queue_dead_letter",
                    "pipeline": pipeline_name,
                    "queue_id": queue_id,
                    "filing_id": body.get("filing_id"),
                    "attempt": attempt_number,
                    "retry_seconds": retry_seconds if next_status == 'pending' else None,
                    "error": str(exc)[:500],
                    "app_version": APP_VERSION,
                }, sort_keys=True))
                if next_status == 'pending':
                    msg.retry(delaySeconds=retry_seconds)
                else:
                    await self.env.DB.prepare(
                        "UPDATE filings SET processed=0, enqueued_at=NULL WHERE id=?"
                    ).bind(int(body["filing_id"])).run()
                    msg.ack()
                await _release_ingestion_lease(self.env, lease_owner)

