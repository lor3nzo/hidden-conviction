import json

from src.sec.company_map import parse_company_tickers_exchange


def test_parse_company_tickers_exchange():
    payload = json.dumps({
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
    })
    rows = parse_company_tickers_exchange(payload)
    assert rows == [{
        "cik": "320193",
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "exchange": "Nasdaq",
    }]
