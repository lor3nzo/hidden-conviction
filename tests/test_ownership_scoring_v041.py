from src.scoring.hcs import score_beneficial_ownership_details


def test_initial_13d_above_five_scores():
    s = score_beneficial_ownership_details("SCHEDULE 13D", 5.4, None)
    assert s.total == 11.0


def test_flat_amendment_scores_zero():
    s = score_beneficial_ownership_details("SCHEDULE 13D/A", 7.4, 7.4)
    assert s.total == 0.0


def test_rising_amendment_scores_positive():
    s = score_beneficial_ownership_details("SCHEDULE 13D/A", 7.8, 5.4)
    assert s.total == 15.0


def test_amendment_without_prior_scores_zero():
    s = score_beneficial_ownership_details("SCHEDULE 13G/A", 8.0, None)
    assert s.total == 0.0


def test_declining_amendment_scores_zero():
    s = score_beneficial_ownership_details("SCHEDULE 13D/A", 8.5, 9.9)
    assert s.total == 0.0
