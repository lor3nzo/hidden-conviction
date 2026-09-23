"""Operational policy helpers for v0.14 adaptive ingestion.

Pure functions live here so health semantics and controller policy can be tested
without importing the large Worker entrypoint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IngestionPolicy:
    mode: str
    target_concurrency: int
    publish_limit: int
    identity_refresh_limit: int
    reason: str


def normalize_sec_date(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw[:10]


def choose_ingestion_policy(
    *, backlog: int, sec_failed_today: int, sec_latest_latency_seconds: float | None
) -> IngestionPolicy:
    """Choose a conservative control policy under the SEC fair-access ceiling.

    Queue max_concurrency is capped at five at deployment. This controller
    adjusts how aggressively new work and identity refreshes are introduced.
    """
    backlog = max(0, int(backlog or 0))
    failures = max(0, int(sec_failed_today or 0))
    latency = float(sec_latest_latency_seconds or 0.0)

    if failures >= 5 or latency >= 60:
        return IngestionPolicy("protect", 1, 20, 1, "SEC failures or latency elevated")
    if failures > 0 or latency >= 30:
        return IngestionPolicy("cautious", 1, 40, 1, "SEC health warrants caution")
    if backlog >= 1000 and latency < 20:
        return IngestionPolicy("catch_up", 5, 225, 1, "large backlog: prioritize filing drain over identity refresh")
    if backlog >= 250 and latency < 30:
        return IngestionPolicy("accelerated", 3, 150, 2, "backlog elevated: prioritize filing drain")
    return IngestionPolicy("steady", 1, 75, 5, "normal operating range: resume identity repair")


def estimate_clear_minutes(*, pending: int, discovered_per_hour: int, processed_per_hour: int) -> float | None:
    pending = max(0, int(pending or 0))
    discovered = max(0, int(discovered_per_hour or 0))
    processed = max(0, int(processed_per_hour or 0))
    if pending == 0:
        return 0.0
    net_drain_per_hour = processed - discovered
    if net_drain_per_hour <= 0:
        return None
    return round((pending / net_drain_per_hour) * 60.0, 1)


def operational_state(*, database: str, queue_status: str, queue_age_minutes: float | None,
                      cron_status: str, sec_status: str, scoring_status: str,
                      queue_errors: int = 0, other_statuses: tuple[str, ...] = ()) -> str:
    primary = {database, queue_status, cron_status, sec_status, scoring_status, *other_statuses}
    if "error" in primary or database != "ok":
        return "failed"
    age = float(queue_age_minutes or 0.0)
    # Backlog by itself is a delay, not a broken system. Other degraded stages
    # indicate an operational fault that deserves the stronger label.
    non_queue = {cron_status, sec_status, scoring_status, *other_statuses}
    if "degraded" in non_queue or "unknown" in non_queue or int(queue_errors or 0) > 0:
        return "degraded"
    if queue_status == "degraded" or age >= 30:
        return "delayed"
    return "operational"


def is_transient_queue_error(message: object) -> bool:
    text = str(message or "").lower()
    tokens = (
        "429", "rate limit", "timeout", "timed out", "temporar", "connection",
        "network", "fetch", "http 500", "http 502", "http 503", "http 504",
        "service unavailable", "bad gateway", "gateway timeout",
    )
    return any(token in text for token in tokens)
