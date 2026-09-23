from dataclasses import dataclass
from xml.etree import ElementTree as ET


@dataclass
class Form4Transaction:
    issuer_cik: str | None
    issuer_name: str | None
    issuer_ticker: str | None
    insider_name: str | None
    is_director: bool
    is_officer: bool
    officer_title: str | None
    transaction_code: str | None
    transaction_date: str | None
    shares: float | None
    price: float | None
    shares_owned_after: float | None
    ownership_type: str | None

    @property
    def transaction_value(self) -> float | None:
        if self.shares is None or self.price is None:
            return None
        return self.shares * self.price

    @property
    def is_open_market_purchase(self) -> bool:
        return self.transaction_code == "P"


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    target = node.find(path)
    if target is None or target.text is None:
        return None
    value = target.text.strip()
    return value or None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _normalize_cik(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lstrip("0") or "0"


def parse_form4(xml_text: str) -> list[Form4Transaction]:
    root = ET.fromstring(xml_text)
    issuer = root.find("issuer")
    owner = root.find("reportingOwner")

    issuer_cik = _normalize_cik(_text(issuer, "issuerCik"))
    issuer_name = _text(issuer, "issuerName")
    issuer_ticker = _text(issuer, "issuerTradingSymbol")

    insider_name = _text(owner, "reportingOwnerId/rptOwnerName")
    is_director = _bool(_text(owner, "reportingOwnerRelationship/isDirector"))
    is_officer = _bool(_text(owner, "reportingOwnerRelationship/isOfficer"))
    officer_title = _text(owner, "reportingOwnerRelationship/officerTitle")

    rows: list[Form4Transaction] = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        rows.append(
            Form4Transaction(
                issuer_cik=issuer_cik,
                issuer_name=issuer_name,
                issuer_ticker=issuer_ticker.upper() if issuer_ticker else None,
                insider_name=insider_name,
                is_director=is_director,
                is_officer=is_officer,
                officer_title=officer_title,
                transaction_code=_text(tx, "transactionCoding/transactionCode"),
                transaction_date=_text(tx, "transactionDate/value"),
                shares=_float(_text(tx, "transactionAmounts/transactionShares/value")),
                price=_float(_text(tx, "transactionAmounts/transactionPricePerShare/value")),
                shares_owned_after=_float(_text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
                ownership_type=_text(tx, "ownershipNature/directOrIndirectOwnership/value"),
            )
        )
    return rows
