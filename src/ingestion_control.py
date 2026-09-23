"""Adaptive ingestion control and operational receipts.

This module owns the D1-backed control plane for queue throughput. Keeping it
outside main.py reduces entrypoint concentration and makes policy evolution less
risky.
"""

from app_meta import APP_VERSION
from ops import choose_ingestion_policy, estimate_clear_minutes, is_transient_queue_error


def _rows(result):
    return getattr(result, "results", None) or []


async def queue_throughput_snapshot(env) -> dict:
    try:
        result = await env.DB.prepare(
            """
            SELECT
              (SELECT COUNT(*) FROM processing_queue WHERE status='pending') AS pending,
              (SELECT COUNT(*) FROM processing_queue WHERE status='processing') AS processing,
              (SELECT COUNT(*) FROM processing_queue WHERE status='error') AS errors,
              (SELECT COUNT(*) FROM filings WHERE created_at>=DATETIME('now','-1 hour')) AS discovered_1h,
              (SELECT COUNT(*) FROM processing_queue WHERE processed_at>=DATETIME('now','-1 hour') AND status IN ('done','ignored')) AS processed_1h,
              (SELECT ROUND((julianday('now')-julianday(MIN(created_at)))*1440.0,1) FROM processing_queue WHERE status='pending') AS oldest_pending_age_minutes
            """
        ).run()
        row = (_rows(result) or [{}])[0]
    except Exception:
        row = {}
    pending = int(row.get("pending") or 0)
    discovered = int(row.get("discovered_1h") or 0)
    processed = int(row.get("processed_1h") or 0)
    return {
        "pending": pending,
        "processing": int(row.get("processing") or 0),
        "errors": int(row.get("errors") or 0),
        "discovered_per_hour": discovered,
        "processed_per_hour": processed,
        "net_backlog_per_hour": discovered - processed,
        "estimated_clear_minutes": estimate_clear_minutes(
            pending=pending, discovered_per_hour=discovered, processed_per_hour=processed
        ),
        "oldest_pending_age_minutes": float(row.get("oldest_pending_age_minutes")) if row.get("oldest_pending_age_minutes") is not None else None,
    }


async def update_ingestion_controller(env):
    throughput = await queue_throughput_snapshot(env)
    try:
        sec_result = await env.DB.prepare(
            "SELECT SUM(failed) AS failed, MAX(latest_latency_seconds) AS latency FROM sec_request_metrics WHERE metric_date=DATE('now')"
        ).run()
        sec = (_rows(sec_result) or [{}])[0]
    except Exception:
        sec = {}
    policy = choose_ingestion_policy(
        backlog=throughput["pending"],
        sec_failed_today=int(sec.get("failed") or 0),
        sec_latest_latency_seconds=float(sec.get("latency") or 0.0),
    )
    try:
        await env.DB.prepare(
            """
            UPDATE ingestion_controller_state
            SET mode=?, target_concurrency=?, publish_limit=?, identity_refresh_limit=?,
                backlog=?, sec_failed_today=?, sec_latest_latency_seconds=?, reason=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=1
            """
        ).bind(
            policy.mode, policy.target_concurrency, policy.publish_limit, policy.identity_refresh_limit,
            throughput["pending"], int(sec.get("failed") or 0), float(sec.get("latency") or 0.0), policy.reason
        ).run()
    except Exception:
        pass
    return policy, throughput


async def record_backlog_sample(env, policy, throughput: dict):
    try:
        await env.DB.prepare(
            """
            INSERT INTO backlog_samples(
              pending, processing, errors, oldest_pending_age_minutes, discovered_1h, processed_1h,
              net_backlog_per_hour, estimated_clear_minutes, target_concurrency, publish_limit,
              identity_refresh_limit, control_mode, app_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """
        ).bind(
            throughput.get("pending",0), throughput.get("processing",0), throughput.get("errors",0),
            throughput.get("oldest_pending_age_minutes"), throughput.get("discovered_per_hour",0),
            throughput.get("processed_per_hour",0), throughput.get("net_backlog_per_hour",0),
            throughput.get("estimated_clear_minutes"), policy.target_concurrency, policy.publish_limit,
            policy.identity_refresh_limit, policy.mode, APP_VERSION
        ).run()
    except Exception:
        pass


async def auto_replay_transient_failures(env, limit: int = 10) -> int:
    try:
        result = await env.DB.prepare(
            """
            SELECT q.id, q.filing_id, q.last_error
            FROM processing_queue q
            WHERE q.status='error' AND COALESCE(q.auto_replay_count,0)<2
              AND (q.last_auto_replay_at IS NULL OR q.last_auto_replay_at<DATETIME('now','-15 minutes'))
            ORDER BY COALESCE(q.dead_letter_at,q.processed_at,q.created_at) ASC LIMIT ?
            """
        ).bind(max(1, min(int(limit), 25))).run()
    except Exception:
        return 0
    replayed = 0
    for row in _rows(result):
        if not is_transient_queue_error(row.get("last_error")):
            continue
        try:
            await env.DB.prepare(
                """
                UPDATE processing_queue SET status='pending', attempts=0, processed_at=NULL, started_at=NULL,
                  next_retry_at=NULL, dead_letter_at=NULL, last_attempt_at=NULL,
                  auto_replay_count=auto_replay_count+1, last_auto_replay_at=CURRENT_TIMESTAMP
                WHERE id=?
                """
            ).bind(int(row["id"])).run()
            await env.DB.prepare("UPDATE filings SET processed=0, enqueued_at=NULL WHERE id=?").bind(int(row["filing_id"])).run()
            replayed += 1
        except Exception:
            continue
    return replayed


async def acquire_ingestion_lease(env, owner: str, target_concurrency: int) -> int | None:
    try:
        result = await env.DB.prepare(
            """
            UPDATE ingestion_leases SET owner=?, lease_until=DATETIME('now','+3 minutes'), updated_at=CURRENT_TIMESTAMP
            WHERE slot=(
              SELECT slot FROM ingestion_leases
              WHERE slot<=? AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)
              ORDER BY slot LIMIT 1
            )
            RETURNING slot
            """
        ).bind(owner, max(1, min(int(target_concurrency), 5))).run()
        rows = _rows(result)
        return int(rows[0]["slot"]) if rows else None
    except Exception:
        # Fail open to one invocation if D1 does not support RETURNING in a runtime.
        return 1


async def release_ingestion_lease(env, owner: str):
    try:
        await env.DB.prepare(
            "UPDATE ingestion_leases SET owner=NULL, lease_until=NULL, updated_at=CURRENT_TIMESTAMP WHERE owner=?"
        ).bind(owner).run()
    except Exception:
        pass


async def sync_system_alerts(env, stages: dict):
    queue = stages.get("queue") or {}
    cron = stages.get("cron") or {}
    alerts = {}
    age = float(queue.get("oldest_pending_age_minutes") or 0)
    if age >= 30:
        alerts["backlog_age"] = ("warning" if age < 60 else "error", f"Oldest filing has waited {age:.0f} minutes")
    errors = int(queue.get("errors") or 0)
    if errors:
        alerts["queue_errors"] = ("warning" if errors < 20 else "error", f"{errors} filing queue errors require attention")
    cron_age = cron.get("minutes_since_success")
    if cron.get("last_success_at") is None or (cron_age is not None and float(cron_age) > 15):
        alerts["cron_stale"] = ("warning", "No recent successful cron completion")
    identity = stages.get("identity") or {}
    stale_identity = int(identity.get("stale_identity_rows") or 0)
    if stale_identity >= 500:
        alerts["identity_stale"] = ("warning", f"{stale_identity} issuer identity rows are older than 90 days")
    try:
        existing = await env.DB.prepare("SELECT code FROM system_alerts WHERE active=1").run()
        active_codes = {str(r.get("code")) for r in _rows(existing)}
        for code, (severity, message) in alerts.items():
            await env.DB.prepare(
                """INSERT INTO system_alerts(code,severity,active,message,first_seen_at,last_seen_at,resolved_at)
                VALUES (?,?,1,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,NULL)
                ON CONFLICT(code) DO UPDATE SET severity=excluded.severity,active=1,message=excluded.message,
                  last_seen_at=CURRENT_TIMESTAMP,resolved_at=NULL"""
            ).bind(code,severity,message).run()
        for code in active_codes - set(alerts):
            await env.DB.prepare(
                "UPDATE system_alerts SET active=0,resolved_at=CURRENT_TIMESTAMP,last_seen_at=CURRENT_TIMESTAMP WHERE code=?"
            ).bind(code).run()
    except Exception:
        pass
