import asyncio
import inspect
from pathlib import Path

import src.pipeline_13f as p13f


class Result:
    def __init__(self, results=None, changes=1):
        self.results = results or []
        self.meta = {"changes": changes}


class Statement:
    def __init__(self, db, sql):
        self.db = db
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def run(self):
        self.db.calls.append((self.sql, self.args))
        if "WHERE status='waiting_predecessor'" in self.sql and "SELECT id" in self.sql:
            return Result([{"id": 136}])
        if "SELECT id, enqueued_at, status" in self.sql:
            return Result([{"id": 999, "status": "pending", "enqueued_at": None}])
        return Result(changes=1)


class DB:
    def __init__(self):
        self.calls = []

    def prepare(self, sql):
        return Statement(self, sql)


class Queue:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class Env:
    def __init__(self):
        self.DB = DB()
        self.THIRTEEN_F_QUEUE = Queue()


def test_v067_generic_wakeup_publishes_direct_successor():
    env = Env()
    woke = asyncio.run(p13f._wake_waiting_successors(env, 137))
    assert woke == 1
    assert env.THIRTEEN_F_QUEUE.sent == [{"work_id": 999, "pipeline": "13f"}]
    assert any(
        "predecessor_report_id=?" in sql and args == (136, 137)
        for sql, args in env.DB.calls
    )


def test_v067_finalize_uses_generic_wakeup_not_historical_only():
    source = inspect.getsource(p13f._finalize_report)
    assert "_wake_waiting_successors" in source
    assert "is_historical" not in source
    assert "parent_report_id" not in source


def test_v067_migration_wakes_only_successors_with_done_predecessors():
    migration = (Path(__file__).parents[1] / "migrations" / "0014_13f_predecessor_wakeup.sql").read_text()
    assert "r.status = 'waiting_predecessor'" in migration
    assert "p.status = 'done'" in migration
    assert "predecessor_report_id" in migration
    assert "prepare_after_prior:" in migration
    assert "DELETE FROM institutional_positions" not in migration
    assert "DELETE FROM thirteen_f_reports" not in migration
    assert "whale_score = 0" in migration
