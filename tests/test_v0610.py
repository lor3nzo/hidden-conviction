from pathlib import Path

import src.pipeline_13f as p13f
from src.sec.thirteen_f import infer_13f_value_scale, parse_13f_information_table_summary


def _row(issuer, cusip, value, shares, title='COM'):
    return f'''
    <infoTable>
      <nameOfIssuer>{issuer}</nameOfIssuer>
      <titleOfClass>{title}</titleOfClass>
      <cusip>{cusip}</cusip>
      <value>{value}</value>
      <shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt></shrsOrPrnAmt>
    </infoTable>
    '''


def test_v0610_value_unit_inference_detects_legacy_thousands_cross_section():
    scale, status = infer_13f_value_scale(
        price_samples=100,
        low_price_samples=92,
        average_raw_position_value=3_500,
    )
    assert scale == 1000.0
    assert status == 'legacy_thousands_inferred'


def test_v0610_value_unit_inference_leaves_normal_dollar_filings_unchanged():
    scale, status = infer_13f_value_scale(
        price_samples=100,
        low_price_samples=3,
        average_raw_position_value=3_500_000,
    )
    assert scale == 1.0
    assert status == 'dollars_assumed'


def test_v0610_summary_normalizes_legacy_thousands_before_portfolio_totals():
    # 20 positions with raw implied prices below $2 and small raw position
    # values should be interpreted as legacy $000 values.
    rows = []
    for i in range(20):
        rows.append(_row(f'Issuer {i}', f'{i:09d}', 4000 + i * 10, 8000 + i * 10))
    xml = '<informationTable>' + ''.join(rows) + '</informationTable>'
    positions, total, portfolio_value, raw_rows, option_rows = parse_13f_information_table_summary(xml, limit=250)
    assert total == 20
    assert raw_rows == 20
    assert option_rows == 0
    assert min(p.value_dollars for p in positions) >= 4_000_000
    assert portfolio_value > 80_000_000


def test_v0610_normalized_boston_like_values_trigger_split_suppression():
    # June raw legacy-thousands value 3,872 -> normalized $3,872,000.
    assert p13f._suspect_stock_split(21800, 5530, 4_488_184, 3_872_000)
    decision = p13f._institutional_comparison(
        shares=21800,
        value_dollars=4_488_184,
        weight=1.037,
        previous_shares=5530,
        previous_value_dollars=3_872_000,
        is_historical=False,
        has_predecessor=True,
        predecessor_complete=True,
        is_fund=False,
    )
    assert decision['score'] == 0
    assert decision['corporate_action_suspected'] == 1
    assert decision['comparison_status'] == 'corporate_action_suspected'


def test_v0610_fund_filter_catches_leading_ishares_and_schwab_trust():
    assert p13f._looks_like_fund('ISHARES TR', 'SHS')
    assert p13f._looks_like_fund('SCHWAB STRATEGIC TR', 'SHS')
    assert p13f._looks_like_fund('VANGUARD INDEX FDS', 'S&P 500 ETF SHS')
    assert not p13f._looks_like_fund('SCHWAB CHARLES CORP NEW', 'COM')


def test_v0610_fund_holdings_score_zero():
    decision = p13f._institutional_comparison(
        shares=21399,
        value_dollars=1_129_011,
        weight=0.463,
        previous_shares=13390,
        previous_value_dollars=700_000,
        is_historical=False,
        has_predecessor=True,
        predecessor_complete=True,
        is_fund=True,
    )
    assert decision['score'] == 0
    assert decision['comparison_status'] == 'fund_excluded'


def test_v0610_migration_rebuilds_13f_and_keeps_whale_gated():
    migration = (Path(__file__).parents[1] / 'migrations' / '0017_13f_value_units_and_fund_filter.sql').read_text()
    assert 'value_scale_factor' in migration
    assert 'value_unit_status' in migration
    assert 'DELETE FROM institutional_positions' in migration
    assert 'DELETE FROM thirteen_f_reports' in migration
    assert 'whale_score = 0' in migration
