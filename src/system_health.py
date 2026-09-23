"""Public operational health aggregation for Hidden Conviction.

Extracted from the Worker entrypoint in v0.14 so health semantics and queue
telemetry can evolve without growing main.py.
"""

from ops import estimate_clear_minutes, operational_state


def _rows(result):
    return getattr(result, "results", None) or []


async def _public_system_snapshot(env) -> dict:
    """Return sanitized operational stages for health checks and the System Console."""
    snapshot = {
        "database": {"status": "ok"},
        "sec": {"status": "unknown"},
        "discovery": {"status": "unknown"},
        "queue": {"status": "unknown"},
        "processing": {"status": "unknown"},
        "scoring": {"status": "unknown"},
        "public_api": {"status": "ok"},
        "identity": {"status": "unknown"},
        "query_performance": {"status": "unknown"},
        "cron": {"status": "unknown"},
    }
    try:
        await env.DB.prepare("SELECT 1 AS ok").run()
    except Exception:
        snapshot["database"] = {"status": "error"}
        return snapshot

    try:
        cron_result = await env.DB.prepare(
            """
            SELECT
              (SELECT run_id FROM cron_runs ORDER BY id DESC LIMIT 1) AS last_run_id,
              (SELECT started_at FROM cron_runs ORDER BY id DESC LIMIT 1) AS last_started_at,
              (SELECT finished_at FROM cron_runs WHERE status='success' ORDER BY id DESC LIMIT 1) AS last_success_at,
              (SELECT status FROM cron_runs ORDER BY id DESC LIMIT 1) AS last_status,
              (SELECT duration_ms FROM cron_runs ORDER BY id DESC LIMIT 1) AS last_duration_ms,
              (SELECT records_discovered FROM cron_runs ORDER BY id DESC LIMIT 1) AS records_discovered,
              (SELECT records_processed FROM cron_runs ORDER BY id DESC LIMIT 1) AS records_processed,
              (SELECT records_failed FROM cron_runs ORDER BY id DESC LIMIT 1) AS records_failed,
              (SELECT scores_recomputed FROM cron_runs ORDER BY id DESC LIMIT 1) AS scores_recomputed,
              (SELECT queue_pending_at_finish FROM cron_runs ORDER BY id DESC LIMIT 1) AS queue_pending_at_finish,
              ROUND((julianday('now')-julianday((SELECT finished_at FROM cron_runs WHERE status='success' ORDER BY id DESC LIMIT 1)))*1440.0,1) AS minutes_since_success,
              ROUND((julianday('now')-julianday((SELECT started_at FROM cron_runs ORDER BY id DESC LIMIT 1)))*1440.0,1) AS minutes_since_start
            """
        ).run()
        cron = (_rows(cron_result) or [{}])[0]
        # Runtime receipts predate cron_runs and are also written after every successful run.
        # Use them as a fallback so health cannot say "never succeeded" while a valid receipt exists.
        if not cron.get("last_success_at"):
            try:
                runtime_success = await env.DB.prepare(
                    "SELECT updated_at FROM runtime_status WHERE key='last_cron_success' AND COALESCE(value,'')<>'' LIMIT 1"
                ).run()
                rr = (_rows(runtime_success) or [{}])[0]
                if rr.get("updated_at"):
                    cron["last_success_at"] = rr.get("updated_at")
                    cron["minutes_since_success"] = None
            except Exception:
                pass
        age = cron.get("minutes_since_success")
        if age is None and cron.get("last_success_at"):
            try:
                age_result = await env.DB.prepare(
                    "SELECT ROUND((julianday('now')-julianday(?))*1440.0,1) AS age"
                ).bind(cron.get("last_success_at")).run()
                age = ((_rows(age_result) or [{}])[0]).get("age")
            except Exception:
                age = None
        age_value = float(age) if age is not None else None
        running_age = float(cron.get("minutes_since_start")) if cron.get("minutes_since_start") is not None else None
        last_status = cron.get("last_status")
        cron_status = "ok" if age_value is not None and age_value <= 15 and last_status != "error" else "degraded"
        if (age_value is not None and age_value > 60) or last_status == "error" or (last_status == "running" and running_age is not None and running_age > 20):
            cron_status = "error"
        snapshot["cron"] = {
            "status": cron_status, "last_run_id": cron.get("last_run_id"),
            "last_status": last_status, "last_started_at": cron.get("last_started_at"),
            "last_success_at": cron.get("last_success_at"), "minutes_since_success": age_value,
            "last_duration_ms": cron.get("last_duration_ms"),
            "records_discovered": int(cron.get("records_discovered") or 0),
            "records_processed": int(cron.get("records_processed") or 0),
            "records_failed": int(cron.get("records_failed") or 0),
            "scores_recomputed": int(cron.get("scores_recomputed") or 0),
            "queue_pending_at_finish": cron.get("queue_pending_at_finish"),
        }
    except Exception:
        snapshot["cron"] = {"status": "degraded", "last_success_at": None}

    try:
        sec_result = await env.DB.prepare(
            """
            SELECT SUM(requests) AS requests, SUM(failed) AS failed,
                   MAX(latest_latency_seconds) AS latest_latency_seconds,
                   MAX(max_latency_seconds) AS max_latency_seconds, MAX(updated_at) AS updated_at
            FROM sec_request_metrics WHERE metric_date=DATE('now')
            """
        ).run()
        sec_row = (_rows(sec_result) or [{}])[0]
        sec_failed = int(sec_row.get("failed") or 0)
        sec_requests = int(sec_row.get("requests") or 0)
        sec_status = "error" if sec_failed >= 20 else "degraded" if sec_failed > 0 else "ok"
        snapshot["sec"] = {
            "status": sec_status, "requests_today": sec_requests, "failed_today": sec_failed,
            "latest_latency_seconds": sec_row.get("latest_latency_seconds"),
            "max_latency_seconds": sec_row.get("max_latency_seconds"),
            "updated_at": sec_row.get("updated_at"),
        }
    except Exception:
        snapshot["sec"] = {"status": "degraded"}

    try:
        perf_result = await env.DB.prepare(
            "SELECT SUM(executions) AS executions, SUM(slow_count) AS slow_count, MAX(max_duration_ms) AS max_duration_ms FROM query_metrics WHERE metric_date=DATE('now')"
        ).run()
        perf = (_rows(perf_result) or [{}])[0]
        slow_count = int(perf.get("slow_count") or 0)
        max_ms = float(perf.get("max_duration_ms") or 0)
        perf_status = "error" if max_ms >= 1000 else "degraded" if slow_count >= 10 or max_ms >= 250 else "ok"
        snapshot["query_performance"] = {
            "status": perf_status,
            "executions_today": int(perf.get("executions") or 0),
            "slow_queries_today": slow_count,
            "max_duration_ms": round(max_ms, 1),
        }
    except Exception:
        snapshot["query_performance"] = {"status": "degraded"}

    try:
        discovery_result = await env.DB.prepare(
            """
            SELECT MAX(last_success_at) AS latest_success_at,
                   MAX(consecutive_failures) AS max_consecutive_failures,
                   SUM(CASE WHEN consecutive_failures>0 THEN 1 ELSE 0 END) AS failing_sources,
                   SUM(CASE WHEN needs_backfill=1 THEN 1 ELSE 0 END) AS needs_backfill,
                   MIN(last_reconciled_date) AS reconciled_through
            FROM discovery_health h LEFT JOIN discovery_watermarks w ON w.source=h.source
            """
        ).run()
        row = (_rows(discovery_result) or [{}])[0]
        failures = int(row.get("max_consecutive_failures") or 0)
        discovery_status = "error" if failures >= 5 else "degraded" if failures > 0 or int(row.get("needs_backfill") or 0) > 0 else "ok"
        backfill_error = None
        try:
            backfill_status = await env.DB.prepare(
                "SELECT value, updated_at FROM runtime_status WHERE key='last_daily_index_backfill_error' LIMIT 1"
            ).run()
            backfill_row = (_rows(backfill_status) or [{}])[0]
            backfill_error = (backfill_row.get("value") or "").strip() or None
        except Exception:
            backfill_error = None
        if backfill_error and discovery_status == "ok":
            discovery_status = "degraded"
        snapshot["discovery"] = {
            "status": discovery_status, "latest_success_at": row.get("latest_success_at"),
            "failing_sources": int(row.get("failing_sources") or 0),
            "needs_backfill": int(row.get("needs_backfill") or 0),
            "reconciled_through": row.get("reconciled_through"),
            "daily_index_error": backfill_error,
        }
    except Exception:
        snapshot["discovery"] = {"status": "degraded"}

    try:
        queue_result = await env.DB.prepare(
            """
            SELECT
              SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) AS processing,
              SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
              MIN(CASE WHEN status='pending' THEN created_at END) AS oldest_pending_at,
              ROUND((julianday('now')-julianday(MIN(CASE WHEN status='pending' THEN created_at END)))*1440.0,1) AS oldest_pending_age_minutes,
              (SELECT COUNT(*) FROM filings WHERE created_at>=DATETIME('now','-1 hour')) AS discovered_1h,
              (SELECT COUNT(*) FROM processing_queue WHERE processed_at>=DATETIME('now','-1 hour') AND status IN ('done','ignored')) AS processed_1h
            FROM processing_queue
            """
        ).run()
        row = (_rows(queue_result) or [{}])[0]
        errors = int(row.get("errors") or 0)
        pending = int(row.get("pending") or 0)
        queue_status = "error" if errors >= 20 else "degraded" if errors > 0 or pending >= 250 else "ok"
        discovered_1h = int(row.get("discovered_1h") or 0)
        processed_1h = int(row.get("processed_1h") or 0)
        eta = estimate_clear_minutes(pending=pending, discovered_per_hour=discovered_1h, processed_per_hour=processed_1h)
        controller = {}
        try:
            cr = await env.DB.prepare(
                "SELECT mode,target_concurrency,publish_limit,identity_refresh_limit,reason,updated_at FROM ingestion_controller_state WHERE id=1"
            ).run()
            controller = (_rows(cr) or [{}])[0]
        except Exception:
            controller = {}
        snapshot["queue"] = {
            "status": queue_status, "pending": pending,
            "processing": int(row.get("processing") or 0), "errors": errors,
            "oldest_pending_at": row.get("oldest_pending_at"),
            "oldest_pending_age_minutes": float(row.get("oldest_pending_age_minutes")) if row.get("oldest_pending_age_minutes") is not None else None,
            "discovered_per_hour": discovered_1h, "processed_per_hour": processed_1h,
            "net_backlog_per_hour": discovered_1h - processed_1h,
            "estimated_clear_minutes": eta, "controller": controller,
        }
    except Exception:
        snapshot["queue"] = {"status": "degraded"}

    try:
        processing_result = await env.DB.prepare(
            "SELECT MAX(processed_at) AS latest_processed_at FROM processing_queue WHERE status IN ('done','ignored')"
        ).run()
        row = (_rows(processing_result) or [{}])[0]
        snapshot["processing"] = {"status": "ok" if row.get("latest_processed_at") else "degraded", **row}
    except Exception:
        snapshot["processing"] = {"status": "degraded"}

    try:
        identity_result = await env.DB.prepare(
            """SELECT COUNT(*) AS stale_identity_rows FROM companies
            WHERE active=1 AND (identity_checked_at IS NULL OR identity_checked_at<DATETIME('now','-90 day'))"""
        ).run()
        stale_identity = int(((_rows(identity_result) or [{}])[0]).get("stale_identity_rows") or 0)
        identity_limit = int((((snapshot.get("queue") or {}).get("controller") or {}).get("identity_refresh_limit") or 1))
        estimated_identity_clear_hours = round((stale_identity / max(1, identity_limit)) * 5.0 / 60.0, 1) if stale_identity else 0.0
        snapshot["identity"] = {
            "status": "degraded" if stale_identity >= 500 else "ok",
            "stale_identity_rows": stale_identity,
            "refresh_batch_size": identity_limit,
            "estimated_refresh_hours": estimated_identity_clear_hours,
        }
    except Exception:
        snapshot["identity"] = {"status": "degraded"}

    try:
        scoring_result = await env.DB.prepare(
            """
            SELECT MAX(score_date) AS latest_score_date, MAX(scored_at) AS latest_scored_at,
                   (SELECT COUNT(*) FROM current_scores WHERE hcs_score>0 AND score_date<DATE('now')) AS stale_active_scores,
                   (SELECT COUNT(*) FROM score_recompute_queue WHERE status='error') AS errors,
                   ROUND((julianday('now')-julianday(MAX(scored_at)))*1440.0,1) AS minutes_since_latest_score
            FROM scores
            """
        ).run()
        row = (_rows(scoring_result) or [{}])[0]
        errors = int(row.get("errors") or 0)
        stale = int(row.get("stale_active_scores") or 0)
        scoring_status = "error" if errors >= 20 else "degraded" if errors > 0 or stale > 0 else "ok"
        snapshot["scoring"] = {
            "status": scoring_status, "latest_score_date": row.get("latest_score_date"),
            "latest_scored_at": row.get("latest_scored_at"), "stale_active_scores": stale,
            "minutes_since_latest_score": float(row.get("minutes_since_latest_score")) if row.get("minutes_since_latest_score") is not None else None,
            "errors": errors,
        }
    except Exception:
        snapshot["scoring"] = {"status": "degraded"}
    return snapshot


def _overall_system_status(stages: dict) -> str:
    states = [str((stage or {}).get("status") or "unknown") for stage in stages.values()]
    if "error" in states:
        return "error"
    if "degraded" in states or "unknown" in states:
        return "degraded"
    return "ok"


def _operational_state_from_stages(stages: dict) -> str:
    queue = stages.get("queue") or {}
    return operational_state(
        database=str((stages.get("database") or {}).get("status") or "unknown"),
        queue_status=str(queue.get("status") or "unknown"),
        queue_age_minutes=queue.get("oldest_pending_age_minutes"),
        queue_errors=int(queue.get("errors") or 0),
        cron_status=str((stages.get("cron") or {}).get("status") or "unknown"),
        sec_status=str((stages.get("sec") or {}).get("status") or "unknown"),
        scoring_status=str((stages.get("scoring") or {}).get("status") or "unknown"),
        other_statuses=(
            str((stages.get("discovery") or {}).get("status") or "unknown"),
            str((stages.get("identity") or {}).get("status") or "unknown"),
            str((stages.get("query_performance") or {}).get("status") or "unknown"),
            str((stages.get("public_api") or {}).get("status") or "unknown"),
        ),
    )
