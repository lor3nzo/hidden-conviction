import asyncio

from src.ingestion_control import (
    acquire_ingestion_lease,
    auto_replay_transient_failures,
    queue_throughput_snapshot,
    record_backlog_sample,
    release_ingestion_lease,
    sync_system_alerts,
    update_ingestion_controller,
)


class Result:
    def __init__(self, rows=None):
        self.results = rows or []


class Stmt:
    def __init__(self, db, sql):
        self.db = db
        self.sql = " ".join(sql.split())
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def run(self):
        self.db.calls.append((self.sql, self.args))
        if self.db.fail and self.db.fail in self.sql:
            raise RuntimeError("synthetic failure")
        return Result(self.db.result_for(self.sql, self.args))


class DB:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.alert_codes = [{"code": "old_alert"}]

    def prepare(self, sql):
        return Stmt(self, sql)

    def result_for(self, sql, args):
        if "AS pending" in sql and "discovered_1h" in sql:
            return [{
                "pending": 1200,
                "processing": 2,
                "errors": 1,
                "discovered_1h": 100,
                "processed_1h": 400,
                "oldest_pending_age_minutes": 48.5,
            }]
        if "SUM(failed) AS failed" in sql:
            return [{"failed": 0, "latency": 12.0}]
        if "SELECT q.id, q.filing_id" in sql:
            return [
                {"id": 7, "filing_id": 70, "last_error": "HTTP 503 Service unavailable"},
                {"id": 8, "filing_id": 80, "last_error": "permanent parse error"},
            ]
        if "RETURNING slot" in sql:
            return [{"slot": 2}]
        if "SELECT code FROM system_alerts" in sql:
            return self.alert_codes
        return []


class Env:
    def __init__(self):
        self.DB = DB()


def test_queue_snapshot_and_controller_policy_are_data_driven():
    env = Env()
    snap = asyncio.run(queue_throughput_snapshot(env))
    assert snap["pending"] == 1200
    assert snap["net_backlog_per_hour"] == -300
    assert snap["estimated_clear_minutes"] == 240.0

    policy, snap2 = asyncio.run(update_ingestion_controller(env))
    assert policy.mode == "catch_up"
    assert policy.target_concurrency == 5
    assert snap2["pending"] == 1200
    assert any("UPDATE ingestion_controller_state" in sql for sql, _ in env.DB.calls)


def test_queue_snapshot_fails_safe():
    env = Env()
    env.DB.fail = "AS pending"
    snap = asyncio.run(queue_throughput_snapshot(env))
    assert snap["pending"] == 0
    assert snap["estimated_clear_minutes"] == 0.0


def test_backlog_receipt_and_transient_auto_replay():
    env = Env()
    policy, snap = asyncio.run(update_ingestion_controller(env))
    asyncio.run(record_backlog_sample(env, policy, snap))
    assert any("INSERT INTO backlog_samples" in sql for sql, _ in env.DB.calls)

    replayed = asyncio.run(auto_replay_transient_failures(env, limit=10))
    assert replayed == 1
    assert any("auto_replay_count=auto_replay_count+1" in sql for sql, _ in env.DB.calls)
    assert any("UPDATE filings SET processed=0" in sql for sql, _ in env.DB.calls)


def test_auto_replay_fails_safe_when_query_fails():
    env = Env()
    env.DB.fail = "SELECT q.id, q.filing_id"
    assert asyncio.run(auto_replay_transient_failures(env)) == 0


def test_ingestion_leases_acquire_release_and_fallback():
    env = Env()
    slot = asyncio.run(acquire_ingestion_lease(env, "worker-a", 5))
    assert slot == 2
    asyncio.run(release_ingestion_lease(env, "worker-a"))
    assert any("owner=NULL" in sql for sql, _ in env.DB.calls)

    env2 = Env()
    env2.DB.fail = "UPDATE ingestion_leases SET owner=?"
    assert asyncio.run(acquire_ingestion_lease(env2, "worker-b", 2)) == 1


def test_system_alert_sync_creates_and_resolves_alerts():
    env = Env()
    stages = {
        "queue": {"oldest_pending_age_minutes": 70, "errors": 3},
        "cron": {"last_success_at": None, "minutes_since_success": None},
        "identity": {"stale_identity_rows": 937},
    }
    asyncio.run(sync_system_alerts(env, stages))
    calls = env.DB.calls
    inserts = [args for sql, args in calls if "INSERT INTO system_alerts" in sql]
    assert {args[0] for args in inserts} == {"backlog_age", "queue_errors", "cron_stale", "identity_stale"}
    assert any(args == ("old_alert",) for sql, args in calls if "active=0" in sql)


def test_system_alert_sync_clears_old_alert_when_healthy():
    env = Env()
    stages = {
        "queue": {"oldest_pending_age_minutes": 0, "errors": 0},
        "cron": {"last_success_at": "2026-09-22 19:00:00", "minutes_since_success": 1},
        "identity": {"stale_identity_rows": 10},
    }
    asyncio.run(sync_system_alerts(env, stages))
    assert any(args == ("old_alert",) for sql, args in env.DB.calls if "active=0" in sql)
