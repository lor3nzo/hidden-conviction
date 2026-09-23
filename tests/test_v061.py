from src.pipeline_13f import _name_keys
from src.scoring.hcs import score_13f_position_details
from src.sec.thirteen_f import parse_13f_information_table, parse_13f_submission_top


def _block(name, cusip, value, shares):
    return f'''<infoTable><nameOfIssuer>{name}</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>{cusip}</cusip><value>{value}</value><shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt></shrsOrPrnAmt></infoTable>'''


def test_period_from_submission_header():
    text = '''
COMPANY CONFORMED NAME: SAMPLE MANAGER LP
CENTRAL INDEX KEY: 0000099999
FILED AS OF DATE: 20260814
CONFORMED PERIOD OF REPORT: 20260630
<DOCUMENT>
<TYPE>INFORMATION TABLE
<FILENAME>infotable.xml
<XML>
<informationTable>''' + _block('APPLE INC', '037833100', 1753441, 7002) + '''</informationTable>
</XML>
</DOCUMENT>
'''
    report = parse_13f_submission_top(text)
    assert report.period_of_report == '2026-06-30'
    assert report.total_positions == 1
    assert report.positions[0].value_dollars == 1753441
    assert report.positions[0].value_thousands == 1753.441


def test_standalone_information_table_attachment():
    xml = '<informationTable>' + _block('NVIDIA CORP', '67066G104', 87997240, 583304) + '</informationTable>'
    positions, total = parse_13f_information_table(xml, limit=250)
    assert total == 1
    assert positions[0].cusip == '67066G104'
    assert positions[0].value_dollars == 87997240


def test_dollar_value_scoring_no_1000x_inflation():
    # $30m should receive the $25m size tier, not the $500m tier.
    details = score_13f_position_details(
        current_shares=200,
        previous_shares=100,
        value_dollars=30_000_000,
        position_weight_pct=1.0,
    )
    assert details.change_score == 10
    assert details.size_score == 1
    assert details.weight_score == 1
    assert details.total == 12


def test_name_keys_remove_common_13f_noise():
    assert 'BERKSHIRE HATHAWAY' in _name_keys('BERKSHIRE HATHAWAY INC DEL CL B')
    assert 'ALPHABET' in _name_keys('ALPHABET INC CL A')
