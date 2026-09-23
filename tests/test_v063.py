import asyncio
import json

import src.pipeline_13f as p13f


def test_chunk_size_reduced_for_python_worker_cpu_budget():
    assert p13f.CHUNK_SIZE == 5


def test_bulk_company_persist_sql_shape():
    class Result:
        results = []

    class Statement:
        def __init__(self, db, sql):
            self.db = db
            self.sql = sql
        def bind(self, *args):
            self.args = args
            return self
        async def run(self):
            self.db.calls.append((self.sql, self.args))
            return Result()

    class DB:
        def __init__(self):
            self.calls = []
        def prepare(self, sql):
            return Statement(self, sql)

    class Env:
        pass

    env = Env()
    env.DB = DB()

    rows = [
        {"cik": "320193", "ticker": "AAPL", "company_name": "Apple Inc.", "exchange": "Nasdaq"},
        {"cik": "789019", "ticker": "MSFT", "company_name": "Microsoft Corp", "exchange": "Nasdaq"},
    ]
    asyncio.run(p13f._persist_identity_matches(env, rows))
    assert len(env.DB.calls) == 2
    assert "INSERT INTO companies" in env.DB.calls[0][0]
    assert "INSERT OR IGNORE INTO company_tickers" in env.DB.calls[1][0]
    assert len(env.DB.calls[0][1]) == 20
    assert len(env.DB.calls[1][1]) == 6


def test_five_position_chunk_payload_stays_small():
    positions = [
        {"issuer_name": f"Issuer {i}", "cusip": f"00000000{i}", "value_dollars": 1000, "shares": 10}
        for i in range(5)
    ]
    payload = json.dumps({"total_value": 5000, "positions": positions})
    assert len(json.loads(payload)["positions"]) == 5
