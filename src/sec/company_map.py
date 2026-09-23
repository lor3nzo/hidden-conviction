import json

from .client import sec_fetch_text

COMPANY_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


def normalize_cik(value) -> str:
    return str(value).strip().lstrip("0") or "0"


def parse_company_tickers_exchange(payload: str) -> list[dict]:
    doc = json.loads(payload)
    fields = doc.get("fields") or []
    data = doc.get("data") or []
    index = {name: i for i, name in enumerate(fields)}

    required = {"name", "cik", "ticker", "exchange"}
    if not required.issubset(index):
        missing = sorted(required - set(index))
        raise ValueError(f"SEC company ticker payload missing fields: {missing}")

    rows: list[dict] = []
    for item in data:
        ticker = item[index["ticker"]]
        if not ticker:
            continue
        rows.append(
            {
                "cik": normalize_cik(item[index["cik"]]),
                "ticker": str(ticker).strip().upper(),
                "company_name": str(item[index["name"]]).strip(),
                "exchange": (str(item[index["exchange"]]).strip() if item[index["exchange"]] else None),
            }
        )
    return rows


async def fetch_company_map() -> list[dict]:
    return parse_company_tickers_exchange(await sec_fetch_text(COMPANY_TICKERS_EXCHANGE_URL))
