from workers import WorkerEntrypoint
from pipeline_13f import handle_13f_work


async def _metric(env, *, processed: int = 0, failed: int = 0, retried: int = 0):
    """Best-effort pipeline telemetry for the isolated 13F worker."""
    try:
        await env.DB.prepare(
            """
            INSERT INTO pipeline_metrics
              (pipeline, metric_date, processed, failed, retried, updated_at)
            VALUES ('13f', DATE('now'), ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(pipeline, metric_date) DO UPDATE SET
              processed=pipeline_metrics.processed+excluded.processed,
              failed=pipeline_metrics.failed+excluded.failed,
              retried=pipeline_metrics.retried+excluded.retried,
              updated_at=CURRENT_TIMESTAMP
            """
        ).bind(int(processed), int(failed), int(retried)).run()
    except Exception:
        pass


class Default(WorkerEntrypoint):
    async def queue(self, batch, env, ctx):
        for msg in batch.messages:
            body = msg.body or {}
            work_id = body.get("work_id")
            if not work_id:
                msg.ack()
                continue
            try:
                result = await handle_13f_work(self.env, int(work_id))
                if result == "done":
                    await _metric(self.env, processed=1)
                    msg.ack()
                elif result in {"terminal", "missing", "error"}:
                    await _metric(self.env, failed=1)
                    msg.ack()
                elif result == "deferred":
                    await _metric(self.env, retried=1)
                    msg.ack()
                else:
                    await _metric(self.env, retried=1)
                    msg.retry(delaySeconds=60)
            except Exception:
                await _metric(self.env, failed=1, retried=1)
                msg.retry(delaySeconds=60)
