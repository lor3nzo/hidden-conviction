from pathlib import Path

import src.pipeline_13f as p13f
from src.sec.thirteen_f import parse_13f_information_table_summary


def _row(issuer, cusip, value, shares, title='COM', put_call=None):
    option = f'<putCall>{put_call}</putCall>' if put_call else ''
    return f'''
    <infoTable>
      <nameOfIssuer>{issuer}</nameOfIssuer>
      <titleOfClass>{title}</titleOfClass>
      <cusip>{cusip}</cusip>
      <value>{value}</value>
      <shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt></shrsOrPrnAmt>
      {option}
    </infoTable>
    '''


def test_v068_duplicate_cusips_are_aggregated_before_ranking():
    xml = '<informationTable>' + ''.join([
        _row('LAM RESEARCH CORP', '512807306', 1000, 10),
        _row('LAM RESEARCH CORP', '512807306', 6817014, 15724),
        _row('APPLE INC', '037833100', 5000000, 20000),
    ]) + '</informationTable>'

    positions, total, portfolio_value, raw_rows, option_rows = parse_13f_information_table_summary(xml, limit=250)
    assert raw_rows == 3
    assert option_rows == 0
    assert total == 2
    lam = next(p for p in positions if p.cusip == '512807306')
    assert lam.shares == 15734
    assert lam.value_dollars == 6818014
    assert lam.source_rows == 2
    assert portfolio_value == 11818014


def test_v068_options_are_excluded_from_long_equity_signal_and_denominator():
    xml = '<informationTable>' + ''.join([
        _row('APPLE INC', '037833100', 2500000, 10000),
        _row('APPLE INC', '037833100', 9000000, 500, put_call='CALL'),
        _row('MICROSOFT CORP', '594918104', 4000000, 8000),
    ]) + '</informationTable>'

    positions, total, portfolio_value, raw_rows, option_rows = parse_13f_information_table_summary(xml, limit=250)
    assert raw_rows == 3
    assert option_rows == 1
    assert total == 2
    assert portfolio_value == 6500000
    apple = next(p for p in positions if p.cusip == '037833100')
    assert apple.shares == 10000
    assert apple.value_dollars == 2500000


def test_v068_full_portfolio_denominator_survives_top_n_retention():
    xml = '<informationTable>' + ''.join([
        _row('A', '000000001', 100, 10),
        _row('B', '000000002', 200, 20),
        _row('C', '000000003', 300, 30),
    ]) + '</informationTable>'
    positions, total, portfolio_value, raw_rows, option_rows = parse_13f_information_table_summary(xml, limit=2)
    assert [p.cusip for p in positions] == ['000000003', '000000002']
    assert total == 3
    assert raw_rows == 3
    assert option_rows == 0
    assert portfolio_value == 600


def test_v068_truncated_predecessor_missing_cusip_is_unknown_not_new():
    decision = p13f._institutional_comparison(
        shares=1000,
        value_dollars=10_000_000,
        weight=1.0,
        previous_shares=None,
        previous_value_dollars=None,
        is_historical=False,
        has_predecessor=True,
        predecessor_complete=False,
        is_fund=False,
    )
    assert decision['score'] == 0
    assert decision['is_new'] == 0
    assert decision['comparison_status'] == 'unknown_predecessor_truncated'


def test_v068_complete_predecessor_missing_cusip_can_be_new():
    decision = p13f._institutional_comparison(
        shares=1000,
        value_dollars=10_000_000,
        weight=1.0,
        previous_shares=None,
        previous_value_dollars=None,
        is_historical=False,
        has_predecessor=True,
        predecessor_complete=True,
        is_fund=False,
    )
    assert decision['is_new'] == 1
    assert decision['comparison_status'] == 'new_position'
    assert decision['score'] > 0


def test_v068_split_like_jump_is_suppressed_but_real_accumulation_is_not():
    # Approximate 4-for-1 split: shares ~4x while economic value stays near flat.
    assert p13f._suspect_stock_split(21800, 5530, 4_488_184, 4_100_000)
    split = p13f._institutional_comparison(
        shares=21800,
        value_dollars=4_488_184,
        weight=1.04,
        previous_shares=5530,
        previous_value_dollars=4_100_000,
        is_historical=False,
        has_predecessor=True,
        predecessor_complete=True,
        is_fund=False,
    )
    assert split['score'] == 0
    assert split['corporate_action_suspected'] == 1
    assert split['comparison_status'] == 'corporate_action_suspected'

    # A real 4x accumulation with roughly unchanged per-share value should not
    # look like a split.
    assert not p13f._suspect_stock_split(400, 100, 4_200_000, 1_000_000)


def test_v068_migration_rebuilds_bad_rows_but_keeps_whale_gated():
    migration = (Path(__file__).parents[1] / 'migrations' / '0015_13f_signal_integrity.sql').read_text()
    assert 'DELETE FROM institutional_positions' in migration
    assert 'DELETE FROM thirteen_f_reports' in migration
    assert 'whale_score = 0' in migration
    assert 'corporate_action_suspected' in migration
    assert 'previous_value_dollars' in migration
