from dataclasses import dataclass


@dataclass
class ScoreBreakdown:
    insider_score: float = 0
    cluster_score: float = 0
    ownership_score: float = 0
    whale_score: float = 0
    event_score: float = 0
    capital_allocation_score: float = 0
    convergence_bonus: float = 0
    penalty: float = 0

    @property
    def raw_score(self) -> float:
        return (
            self.insider_score
            + self.cluster_score
            + self.ownership_score
            + self.whale_score
            + self.event_score
            + self.capital_allocation_score
            + self.convergence_bonus
            - self.penalty
        )

    @property
    def hcs_score(self) -> float:
        return max(0, min(100, round(self.raw_score, 1)))

    @property
    def classification(self) -> str:
        score = self.hcs_score
        if score >= 90:
            return "Exceptional Conviction"
        if score >= 80:
            return "High Conviction"
        if score >= 70:
            return "Watch"
        if score >= 60:
            return "Emerging"
        return "No Signal"


@dataclass
class Form4PurchaseScore:
    total: float
    base_score: float
    role_score: float
    significance_score: float
    dollar_score: float
    small_purchase_penalty: float
    purchase_ratio: float | None


def purchase_ratio_vs_prior_holdings(
    shares_purchased: float | None,
    shares_owned_after: float | None,
) -> float | None:
    """Purchase size relative to the insider's holdings immediately before the purchase.

    For an open-market purchase:
        prior shares = shares owned after - shares purchased
        purchase ratio = shares purchased / prior shares

    If prior shares are zero or negative, treat it as a new/near-new position and
    return 1.0 (100%) for scoring purposes.
    """
    shares = float(shares_purchased or 0)
    after = float(shares_owned_after or 0)

    if shares <= 0 or after <= 0:
        return None

    prior = after - shares
    if prior <= 0:
        return 1.0

    return max(0.0, shares / prior)


def _role_score(role: str | None) -> float:
    role_normalized = (role or "").strip().lower()

    if (
        "chief executive" in role_normalized
        or role_normalized == "ceo"
        or "chief financial" in role_normalized
        or role_normalized == "cfo"
    ):
        return 6.0

    if (
        "president" in role_normalized
        or "chief operating" in role_normalized
        or role_normalized == "coo"
    ):
        return 5.0

    if any(
        title in role_normalized
        for title in (
            "executive vice president",
            "evp",
            "senior vice president",
            "svp",
            "chief accounting",
            "chief legal",
            "general counsel",
        )
    ):
        return 4.0

    if "director" in role_normalized:
        return 3.0

    if role_normalized:
        return 2.0

    return 1.0


def _significance_score(purchase_ratio: float | None) -> float:
    if purchase_ratio is None:
        return 0.0

    if purchase_ratio >= 0.50:
        return 12.0
    if purchase_ratio >= 0.25:
        return 9.0
    if purchase_ratio >= 0.10:
        return 6.0
    if purchase_ratio >= 0.05:
        return 3.0
    if purchase_ratio > 0:
        return 1.0
    return 0.0


def _dollar_score(transaction_value: float | None) -> float:
    value = float(transaction_value or 0)

    if value >= 1_000_000:
        return 4.0
    if value >= 250_000:
        return 3.0
    if value >= 100_000:
        return 2.0
    if value >= 25_000:
        return 1.0
    return 0.0


def score_form4_purchase_details(
    role: str | None,
    transaction_value: float | None,
    shares_purchased: float | None = None,
    shares_owned_after: float | None = None,
) -> Form4PurchaseScore:
    """Score one open-market Form 4 purchase.

    v0.3 weights:
      * 10 points: bona fide open-market purchase baseline
      * up to 6: insider role/information proximity
      * up to 12: purchase size vs pre-purchase holdings
      * up to 4: absolute dollar commitment
      * -4: de minimis purchases below $10,000

    The section remains capped at 30 points.
    """
    base = 10.0
    role_points = _role_score(role)
    ratio = purchase_ratio_vs_prior_holdings(
        shares_purchased,
        shares_owned_after,
    )
    significance = _significance_score(ratio)
    dollars = _dollar_score(transaction_value)

    value = float(transaction_value or 0)
    small_penalty = 4.0 if 0 < value < 10_000 else 0.0

    total = base + role_points + significance + dollars - small_penalty
    total = max(0.0, min(30.0, total))

    return Form4PurchaseScore(
        total=round(total, 1),
        base_score=base,
        role_score=role_points,
        significance_score=significance,
        dollar_score=dollars,
        small_purchase_penalty=small_penalty,
        purchase_ratio=round(ratio, 6) if ratio is not None else None,
    )


def score_form4_purchase(
    role: str | None,
    transaction_value: float | None,
    shares_purchased: float | None = None,
    shares_owned_after: float | None = None,
) -> float:
    return score_form4_purchase_details(
        role=role,
        transaction_value=transaction_value,
        shares_purchased=shares_purchased,
        shares_owned_after=shares_owned_after,
    ).total


@dataclass
class OwnershipScore:
    total: float
    base_score: float
    stake_score: float
    change_score: float
    current_pct: float | None
    previous_pct: float | None


def score_beneficial_ownership_details(
    schedule_type: str,
    current_pct: float | None,
    previous_pct: float | None = None,
) -> OwnershipScore:
    """Score one filing-level Schedule 13 ownership group, capped at 15.

    v0.4.1 changes the interpretation from "large holder" to "accumulation":

    * An initial 13D/13G above 5% is a valid new-whale event and receives
      base + stake points.
    * An amendment is scored only when the stake increased. Flat or falling
      amendments get zero ownership-conviction points.
    * 13D receives a higher base than 13G because it is the active schedule.
    """
    form = (schedule_type or "").upper()
    current = float(current_pct) if current_pct is not None else None
    previous = float(previous_pct) if previous_pct is not None else None

    if current is None or current < 5.0:
        return OwnershipScore(0.0, 0.0, 0.0, 0.0, current, previous)

    is_amendment = "/A" in form
    base = 10.0 if "13D" in form else 6.0

    if current >= 15.0:
        stake = 5.0
    elif current >= 10.0:
        stake = 4.0
    elif current >= 7.5:
        stake = 3.0
    else:
        stake = 1.0

    # Initial crossing / first observed Schedule 13 filing.
    if not is_amendment:
        total = min(15.0, base + stake)
        return OwnershipScore(
            total=round(total, 1),
            base_score=base,
            stake_score=stake,
            change_score=0.0,
            current_pct=current,
            previous_pct=previous,
        )

    # Amendments are accumulation signals only if we can show an increase.
    if previous is None:
        return OwnershipScore(0.0, 0.0, 0.0, 0.0, current, previous)

    delta = current - previous
    if delta <= 0:
        return OwnershipScore(0.0, 0.0, 0.0, 0.0, current, previous)

    if delta >= 2.0:
        change = 6.0
    elif delta >= 1.0:
        change = 4.0
    elif delta >= 0.5:
        change = 2.0
    else:
        change = 1.0

    # For an amendment, accumulation is the primary evidence. Keep the
    # schedule/stake context, but require a positive delta to unlock it.
    total = min(15.0, base + stake + change)
    return OwnershipScore(
        total=round(total, 1),
        base_score=base,
        stake_score=stake,
        change_score=change,
        current_pct=current,
        previous_pct=previous,
    )


def score_beneficial_ownership(
    schedule_type: str,
    current_pct: float | None,
    previous_pct: float | None = None,
) -> float:
    return score_beneficial_ownership_details(
        schedule_type=schedule_type,
        current_pct=current_pct,
        previous_pct=previous_pct,
    ).total


def confirmed_institutional_score(
    candidate_score: float | None,
    *,
    insider_score: float = 0,
    ownership_score: float = 0,
    event_score: float = 0,
    capital_allocation_score: float = 0,
    hcs_eligible: bool = True,
) -> float:
    """Activate 13F only as confirmation of a fresher positive signal family.

    13F is delayed quarterly data. A positive institutional position score is
    therefore not allowed to create HCS conviction by itself. It can contribute
    up to 15 points only when at least one fresher positive family is active and
    the issuer is inside the public HCS universe.
    """
    if not hcs_eligible:
        return 0.0
    candidate = max(0.0, min(15.0, float(candidate_score or 0)))
    if candidate <= 0:
        return 0.0
    anchor = any(float(value or 0) > 0 for value in (
        insider_score,
        ownership_score,
        event_score,
        capital_allocation_score,
    ))
    return round(candidate, 1) if anchor else 0.0


@dataclass
class InstitutionalScore:
    total: float
    change_score: float
    size_score: float
    weight_score: float
    is_new_position: bool
    share_change_pct: float | None


def score_13f_position_details(
    current_shares: float | None,
    previous_shares: float | None,
    value_dollars: float | None,
    position_weight_pct: float | None = None,
) -> InstitutionalScore:
    """Score delayed 13F accumulation, capped at 15 points.

    v0.6.4 deliberately suppresses ordinary portfolio drift.  Existing
    positions must increase by at least 2% before earning any conviction
    points.  New positions use a separate materiality test so tiny initiations
    do not look like high-conviction buying.
    """
    current = float(current_shares or 0)
    previous = float(previous_shares) if previous_shares is not None else None
    value = float(value_dollars or 0)
    weight = float(position_weight_pct or 0)

    if current <= 0:
        return InstitutionalScore(0, 0, 0, 0, False, None)

    is_new = previous is None or previous <= 0
    change_pct = None

    if is_new:
        # A genuinely new but immaterial position is not a conviction signal.
        if value < 5_000_000 and weight < 0.25:
            return InstitutionalScore(0, 0, 0, 0, True, None)
        change = 4.0
    else:
        change_pct = ((current - previous) / previous) * 100.0
        if change_pct < 2.0:
            return InstitutionalScore(0, 0, 0, 0, False, round(change_pct, 2))
        if change_pct < 5.0:
            change = 1.0
        elif change_pct < 10.0:
            change = 2.0
        elif change_pct < 25.0:
            change = 4.0
        elif change_pct < 50.0:
            change = 6.0
        elif change_pct < 100.0:
            change = 8.0
        else:
            change = 10.0

    if value >= 500_000_000:
        size = 3.0
    elif value >= 100_000_000:
        size = 2.0
    elif value >= 25_000_000:
        size = 1.0
    else:
        size = 0.0

    if weight >= 5.0:
        weight_score = 3.0
    elif weight >= 2.0:
        weight_score = 2.0
    elif weight >= 0.5:
        weight_score = 1.0
    else:
        weight_score = 0.0

    total = min(15.0, change + size + weight_score)
    return InstitutionalScore(
        total=round(total, 1),
        change_score=change,
        size_score=size,
        weight_score=weight_score,
        is_new_position=is_new,
        share_change_pct=round(change_pct, 2) if change_pct is not None else None,
    )


def score_13f_position(
    current_shares: float | None,
    previous_shares: float | None,
    value_dollars: float | None,
    position_weight_pct: float | None = None,
) -> float:
    return score_13f_position_details(
        current_shares, previous_shares, value_dollars, position_weight_pct
    ).total
