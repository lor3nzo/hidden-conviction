"""Operational telemetry helpers shared by the ingestion worker.

Kept deliberately small and failure-tolerant: telemetry must never block filing
processing or score publication.
"""


def _rows(result):
    rows = getattr(result, "results", None)
    if rows is None:
        return []
    return [dict(row) for row in rows]


async def set_runtime_status(env, key: str, value: str):
    try:
        await env.DB.prepare(
            """
            INSERT INTO runtime_status(key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """
        ).bind(str(key)[:100], str(value)[:1000]).run()
    except Exception:
        return


async def record_pipeline_metric(
    env,
    pipeline: str,
    *,
    discovered: int = 0,
    processed: int = 0,
    failed: int = 0,
    retried: int = 0,
    latency_seconds: float | None = None,
):
    try:
        await env.DB.prepare(
            """
            INSERT INTO pipeline_metrics
              (pipeline, metric_date, discovered, processed, failed, retried, latest_latency_seconds, updated_at)
            VALUES (?, DATE('now'), ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(pipeline, metric_date) DO UPDATE SET
              discovered=pipeline_metrics.discovered+excluded.discovered,
              processed=pipeline_metrics.processed+excluded.processed,
              failed=pipeline_metrics.failed+excluded.failed,
              retried=pipeline_metrics.retried+excluded.retried,
              latest_latency_seconds=COALESCE(excluded.latest_latency_seconds, pipeline_metrics.latest_latency_seconds),
              updated_at=CURRENT_TIMESTAMP
            """
        ).bind(
            pipeline,
            int(discovered),
            int(processed),
            int(failed),
            int(retried),
            latency_seconds,
        ).run()
    except Exception:
        return


async def record_sec_request_metric(env, source: str, *, failed: bool, latency_seconds: float):
    """Persist coarse SEC request timing/error telemetry without request contents."""
    try:
        await env.DB.prepare(
            """
            INSERT INTO sec_request_metrics(source, metric_date, requests, failed, latest_latency_seconds, max_latency_seconds, updated_at)
            VALUES (?, DATE('now'), 1, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source, metric_date) DO UPDATE SET
              requests=sec_request_metrics.requests+1,
              failed=sec_request_metrics.failed+excluded.failed,
              latest_latency_seconds=excluded.latest_latency_seconds,
              max_latency_seconds=MAX(COALESCE(sec_request_metrics.max_latency_seconds,0), excluded.max_latency_seconds),
              updated_at=CURRENT_TIMESTAMP
            """
        ).bind(
            source,
            1 if failed else 0,
            float(latency_seconds),
            float(latency_seconds),
        ).run()
    except Exception:
        return


async def filing_latency_seconds(env, filing_id: int) -> float | None:
    try:
        result = await env.DB.prepare(
            "SELECT ROUND((julianday('now') - julianday(created_at)) * 86400.0, 1) AS latency FROM filings WHERE id=? LIMIT 1"
        ).bind(int(filing_id)).run()
        row = (_rows(result) or [{}])[0]
        value = row.get("latency")
        return float(value) if value is not None else None
    except Exception:
        return None
