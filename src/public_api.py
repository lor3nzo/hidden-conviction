"""Pure helpers for the public Hidden Conviction web API."""


def public_signal_families(score: dict | None) -> list[dict]:
    """Return public-facing signal labels without exposing proprietary weights."""
    if not score:
        return []

    signals: list[dict] = []

    def add(key: str, label: str, description: str, direction: str = "positive"):
        signals.append({
            "key": key,
            "label": label,
            "description": description,
            "direction": direction,
        })

    if float(score.get("insider_score") or 0) > 0:
        add(
            "insider",
            "Insider Buying",
            "Recent reported insider purchase activity is contributing to the current HCS.",
        )
    if float(score.get("cluster_score") or 0) > 0:
        add(
            "cluster",
            "Insider Cluster",
            "Multiple insiders have reported purchase activity within the active scoring window.",
        )
    if float(score.get("ownership_score") or 0) > 0:
        add(
            "ownership",
            "Beneficial Ownership",
            "Recent Schedule 13D or 13G ownership activity is contributing to the current HCS.",
        )
    if float(score.get("whale_score") or 0) > 0:
        add(
            "institutional",
            "Institutional Accumulation",
            "Quarterly institutional holdings data is providing confirmation to the current HCS.",
        )

    event_score = float(score.get("event_score") or 0)
    if event_score > 0:
        add(
            "event",
            "Corporate Event",
            "A recent 8-K event is positively contributing to the current HCS.",
        )
    elif event_score < 0:
        add(
            "event",
            "Corporate Risk Event",
            "A recent 8-K event is reducing the current HCS.",
            direction="negative",
        )

    if float(score.get("capital_allocation_score") or 0) > 0:
        add(
            "capital_allocation",
            "Capital Allocation",
            "Recent capital-allocation behavior is contributing to the current HCS.",
        )
    if float(score.get("convergence_bonus") or 0) > 0:
        add(
            "convergence",
            "Signal Convergence",
            "Multiple independent signal families are active at the same time.",
        )
    if float(score.get("penalty") or 0) > 0:
        add(
            "penalty",
            "Risk Adjustment",
            "A scoring risk adjustment is currently reducing the raw conviction signal.",
            direction="negative",
        )

    return signals


def company_lookup_mode(identifier: str) -> tuple[str, str]:
    value = (identifier or "").strip()
    if not value:
        return "ticker", ""
    if value.isdigit():
        return "cik", value.lstrip("0") or "0"
    return "ticker", value.upper()
