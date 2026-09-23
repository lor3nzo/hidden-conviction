import asyncio
import inspect
from pathlib import Path

import src.pipeline_13f as p13f
from src.sec.thirteen_f import ThirteenFPosition


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
        if "SELECT COUNT(*) AS done_count" in self.sql:
            return Result([{"done_count": 0}])
        if "SELECT id, status, enqueued_at FROM thirteen_f_work_items" in self.sql:
            return Result([{"id": 902, "status": "pending", "enqueued_at": None}])
        if "SELECT 1 AS blocked" in self.sql:
            return Result(self.db.blocked_rows)
        return Result()


class DB:
    def __init__(self):
        self.calls = []
        self.blocked_rows = []

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


def _position(i):
    return ThirteenFPosition(
        issuer_name=f"Issuer {i}",
        cusip=f"{i:09d}",
        value_dollars=1000.0 + i,
        shares=10.0 + i,
        title_of_class="COM",
    )


def test_v066_chunk_fanout_is_bulk_created_without_queue_sends():
    env = Env()
    chunks = [[_position(i * 5 + j) for j in range(5)] for i in range(50)]
    done = asyncio.run(p13f._bulk_create_chunk_work(env, 77, chunks, 1_000_000.0))

    assert done == 0
    insert_calls = [sql for sql, _ in env.DB.calls if "INSERT INTO thirteen_f_work_items" in sql]
    assert len(insert_calls) == 5
    assert len(env.DB.calls) == 8  # delete + 5 bulk inserts + count + report update
    assert env.THIRTEEN_F_QUEUE.sent == []
    assert p13f.CHUNK_INSERT_BATCH == 10


def test_v066_each_chunk_enqueues_only_one_successor():
    env = Env()
    published = asyncio.run(p13f._enqueue_next_chunk_after(env, 77, "chunk:000"))
    assert published is True
    assert env.THIRTEEN_F_QUEUE.sent == [{"work_id": 902, "pipeline": "13f"}]


def test_v066_out_of_order_chunk_guard():
    env = Env()
    env.DB.blocked_rows = [{"blocked": 1}]
    assert asyncio.run(p13f._chunk_has_unfinished_predecessor(env, 77, "chunk:012")) is True
    env.DB.blocked_rows = []
    assert asyncio.run(p13f._chunk_has_unfinished_predecessor(env, 77, "chunk:012")) is False


def test_v066_finalize_has_no_hcs_recompute_while_whale_gated():
    source = inspect.getsource(p13f._finalize_report)
    assert "recompute_today_score" not in source
    assert "SELECT DISTINCT issuer_cik" not in source


def test_v066_worker_acks_deferred_legacy_chunk_messages():
    worker_source = (Path(__file__).parents[1] / "src" / "worker_13f.py").read_text()
    assert '"deferred"' in worker_source
    assert "msg.ack()" in worker_source


def test_v066_migration_preserves_parsed_13f_data():
    migration = (Path(__file__).parents[1] / "migrations" / "0013_13f_serial_chunk_chain.sql").read_text()
    assert "DELETE FROM institutional_positions" not in migration
    assert "DELETE FROM thirteen_f_reports" not in migration
    assert "WHERE status='processing'" in migration
    assert "WHERE stage='chunk' AND status='pending'" in migration
    assert "whale_score = 0" in migration
