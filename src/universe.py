"""Issuer-universe classification for public HCS ranking.

The filter is intentionally conservative. It excludes obvious investment funds,
ETFs and blank-check/acquisition vehicles while leaving ordinary operating
companies, REITs and companies with generic words such as "Trust" eligible.
"""

from dataclasses import dataclass
import re

UNIVERSE_VERSION = "universe-v0.8.1"


@dataclass(frozen=True)
class UniverseClassification:
    issuer_type: str
    hcs_eligible: bool
    exclusion_reason: str | None = None
    rule: str = "default_operating_company"
    reason: str | None = None


def _normalized_name(value: str | None) -> str:
    text = (value or "").upper()
    text = re.sub(r"[^A-Z0-9&]+", " ", text)
    return " ".join(text.split())


_FUND_PATTERNS = (
    r"\bFUND\b", r"\bETF\b", r"\bETN\b", r"\bEXCHANGE TRADED\b",
    r"\bMUTUAL FUND\b", r"\bCLOSED END\b", r"\bSERIES TRUST\b",
    r"\bINVESTMENT TRUST\b", r"\bISHARES\b", r"\bSPDR\b",
    r"\bPROSHARES\b", r"\bDIREXION\b", r"\bWISDOMTREE\b",
    r"\bVANECK\b", r"\bGLOBAL X\b", r"\bVANGUARD INDEX\b",
    r"\bSCHWAB STRATEGIC TR(?:UST)?\b",
)

_ACQUISITION_PATTERNS = (
    r"\bSPECIAL PURPOSE ACQUISITION\b", r"\bBLANK CHECK\b", r"\bSPAC\b",
    r"\bACQUISITION CORP(?:ORATION)?(?: [IVX0-9]+)?$",
    r"\bACQUISITION CO(?:MPANY)?(?: [IVX0-9]+)?$",
)


def classify_issuer(
    company_name: str | None,
    ticker: str | None = None,
    exchange: str | None = None,
) -> UniverseClassification:
    """Classify an issuer for the public operating-company HCS universe."""
    del ticker, exchange
    name = _normalized_name(company_name)
    if not name:
        return UniverseClassification(
            "unknown", True, None, "empty_name_permissive",
            "Issuer name is unavailable; retain eligibility until identity is resolved.",
        )

    if any(re.search(pattern, name) for pattern in _FUND_PATTERNS):
        reason = "Excluded from public HCS ranking because the issuer appears to be an investment fund or exchange-traded vehicle."
        return UniverseClassification(
            "fund_or_investment_vehicle", False, reason, "fund_name_rule", reason,
        )

    if any(re.search(pattern, name) for pattern in _ACQUISITION_PATTERNS):
        reason = "Excluded from public HCS ranking because the issuer appears to be a blank-check or acquisition vehicle."
        return UniverseClassification(
            "acquisition_vehicle", False, reason, "acquisition_vehicle_name_rule", reason,
        )

    return UniverseClassification(
        "operating_company", True, None, "default_operating_company",
        "Eligible under the default operating-company universe rule.",
    )


def apply_universe_override(
    base: UniverseClassification,
    override: dict | None,
) -> UniverseClassification:
    """Apply an explicit audited override to a heuristic universe decision."""
    if not override:
        return base
    forced_eligible = override.get("force_eligible")
    forced_type = override.get("force_issuer_type")
    if forced_eligible is None and not forced_type:
        return base
    eligible = base.hcs_eligible if forced_eligible is None else bool(int(forced_eligible))
    issuer_type = forced_type or base.issuer_type
    reason = override.get("reason") or "Explicit issuer-universe override."
    exclusion = None if eligible else reason
    return UniverseClassification(
        issuer_type, eligible, exclusion, "explicit_override", reason,
    )
