"""Durable score-recompute queue helpers.

The queue is keyed by CIK, so enqueueing must be an UPSERT rather than
``INSERT OR IGNORE``. Otherwise a completed issuer can never be queued again.
"""


def _rows(result):
    return getattr(result, "results", None) or []


async def enqueue_score_recompute(
    env,
    cik: str,
    reason: str,
    *,
    revive_errors: bool = True,
) -> bool:
    """Queue or requeue an issuer without disturbing in-flight/pending work.

    New filing evidence may revive an error row because the underlying input has
    changed. Routine daily aging does not revive terminal errors every five
    minutes, preventing a persistent bug from creating an infinite retry loop.
    """
    cik = str(cik or "").strip().lstrip("0") or "0"
    reason = (reason or "score recompute")[:500]

    result = await env.DB.prepare(
        """
        INSERT INTO score_recompute_queue
            (cik, reason, status, attempts, created_at, started_at, processed_at, last_error)
        VALUES (?, ?, 'pending', 0, CURRENT_TIMESTAMP, NULL, NULL, NULL)
        ON CONFLICT(cik) DO UPDATE SET
            reason=excluded.reason,
            status='pending',
            attempts=CASE
                WHEN score_recompute_queue.status='pending' THEN score_recompute_queue.attempts
                ELSE 0
            END,
            created_at=CASE
                WHEN score_recompute_queue.status='pending' THEN score_recompute_queue.created_at
                ELSE CURRENT_TIMESTAMP
            END,
            started_at=NULL,
            processed_at=NULL,
            last_error=CASE
                WHEN score_recompute_queue.status='pending' THEN score_recompute_queue.last_error
                ELSE NULL
            END
        WHERE score_recompute_queue.status <> 'processing'
          AND (score_recompute_queue.status <> 'error' OR ? = 1)
        """
    ).bind(cik, reason, 1 if revive_errors else 0).run()
    meta = getattr(result, "meta", None) or {}
    return int(meta.get("changes", 0) or 0) > 0


async def enqueue_stale_active_scores(env, limit: int = 200) -> int:
    """Requeue yesterday-or-older positive scores so rolling windows can decay.

    Zero scores stop being refreshed until new evidence touches the issuer.
    Positive scores are refreshed each calendar day until they naturally decay
    to zero. This keeps the public leaderboard truthful without recomputing the
    entire issuer universe every day.
    """
    limit = max(1, min(int(limit), 500))
    result = await env.DB.prepare(
        """
        SELECT cs.cik
        FROM current_scores cs
        WHERE cs.hcs_score > 0
          AND cs.score_date < DATE('now')
        ORDER BY cs.hcs_score DESC, cs.score_date ASC, cs.cik ASC
        LIMIT ?
        """
    ).bind(limit).run()

    queued = 0
    for row in _rows(result):
        if await enqueue_score_recompute(
            env,
            row.get("cik"),
            "daily rolling-window refresh",
            revive_errors=False,
        ):
            queued += 1
    return queued
