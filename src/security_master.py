"""Conservative security-to-issuer identity helpers for 13F processing.

The security master intentionally prefers false negatives to false positives.
A position is mapped only when all high-confidence identity evidence agrees.
"""

from dataclasses import dataclass
import re

SECURITY_MASTER_VERSION = "security-master-v0.8.1"
INSTITUTIONAL_VERSION = "institutional-v0.8.1"
HCS_VERSION = "hcs-v0.8.1"

_MAPPING_PRIORITY = {
    "security_master_cusip": 100,
    "ticker_exact": 95,
    "approved_alias": 90,
    "normalized_name": 85,
    "sec_directory_name": 80,
    "legacy_existing": 70,
}

_MAPPING_CONFIDENCE = {
    "security_master_cusip": 1.00,
    "ticker_exact": 1.00,
    "approved_alias": 0.95,
    "normalized_name": 0.95,
    "sec_directory_name": 0.95,
    "legacy_existing": 0.90,
}


@dataclass(frozen=True)
class MappingCandidate:
    cik: str
    ticker: str | None
    company_name: str | None
    method: str
    confidence: float


@dataclass(frozen=True)
class MappingResolution:
    issuer_cik: str | None
    ticker: str | None
    company_name: str | None
    method: str
    confidence: float
    status: str


def normalize_security_name(value: str | None) -> str:
    """Normalize issuer labels without guessing across genuinely different names."""
    text = (value or "").upper().replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    tokens = [token for token in text.split() if token != "AND"]
    aliases = {
        "SYSTEMS": "SYS",
        "SYS": "SYS",
        "LABORATORIES": "LABS",
        "LABS": "LABS",
        "MATERIALS": "MATLS",
        "MATLS": "MATLS",
        "HOLDINGS": "HLDGS",
        "HLDGS": "HLDGS",
    }
    text = " ".join(aliases.get(token, token) for token in tokens)
    text = re.sub(r"\b(CL|CLASS)\s+[A-Z0-9]+$", "", text).strip()
    text = re.sub(r"\b(NEW|DEL)$", "", text).strip()
    suffixes = (
        " INCORPORATED", " CORPORATION", " LIMITED", " INC", " CORP", " LTD",
        " PLC", " COMPANY", " CO",
    )
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
                break
    return " ".join(text.split())


def mapping_confidence(method: str) -> float:
    return _MAPPING_CONFIDENCE.get(method, 0.0)


def make_candidate(
    *, cik: str | None, ticker: str | None = None, company_name: str | None = None,
    method: str,
) -> MappingCandidate | None:
    normalized_cik = str(cik or "").strip().lstrip("0") or ("0" if str(cik or "").strip() else "")
    if not normalized_cik:
        return None
    return MappingCandidate(
        cik=normalized_cik,
        ticker=(str(ticker).upper().strip() if ticker else None),
        company_name=company_name,
        method=method,
        confidence=mapping_confidence(method),
    )


def resolve_candidates(candidates: list[MappingCandidate]) -> MappingResolution:
    """Resolve only unanimous issuer evidence; conflicts remain deliberately unmapped."""
    usable = [c for c in candidates if c and c.cik]
    if not usable:
        return MappingResolution(None, None, None, "unresolved", 0.0, "unresolved")

    distinct_ciks = {c.cik for c in usable}
    if len(distinct_ciks) != 1:
        return MappingResolution(None, None, None, "conflict", 0.0, "conflict")

    best = max(usable, key=lambda c: (_MAPPING_PRIORITY.get(c.method, 0), c.confidence))
    return MappingResolution(
        issuer_cik=best.cik,
        ticker=best.ticker,
        company_name=best.company_name,
        method=best.method,
        confidence=best.confidence,
        status="resolved",
    )


def temporal_compatibility_sql(anchor_expr: str = "?", period_expr: str = "period_of_report", filing_expr: str = "filing_date") -> str:
    """Return the conservative compatibility predicate used by 13F confirmation.

    Current anchor evidence can be no more than 120 days before the reported
    quarter end and no more than 45 days after the 13F filing date.
    """
    return (
        f"DATE({anchor_expr}) BETWEEN DATE({period_expr}, '-120 day') "
        f"AND DATE({filing_expr}, '+45 day')"
    )
