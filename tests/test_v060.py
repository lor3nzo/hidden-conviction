from src.sec.thirteen_f import parse_13f_submission_top


def _block(name, cusip, value, shares):
    return f'''<infoTable><nameOfIssuer>{name}</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>{cusip}</cusip><value>{value}</value><shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt></shrsOrPrnAmt></infoTable>'''


def test_streaming_13f_top_positions_are_bounded_and_ranked():
    text = '''
COMPANY CONFORMED NAME: SAMPLE MANAGER LP
CENTRAL INDEX KEY: 0000099999
FILED AS OF DATE: 20260814
<periodOfReport>2026-06-30</periodOfReport>
<informationTable>
''' + _block('Small Co', '111111111', 10, 100) + _block('Big Co', '222222222', 500, 200) + _block('Mid Co', '333333333', 100, 150) + '''</informationTable>'''
    report = parse_13f_submission_top(text, limit=2)
    assert report.total_positions == 3
    assert len(report.positions) == 2
    assert [p.cusip for p in report.positions] == ['222222222', '333333333']


def test_streaming_13f_zero_limit_counts_without_retaining():
    text = '''
COMPANY CONFORMED NAME: SAMPLE MANAGER LP
CENTRAL INDEX KEY: 0000099999
FILED AS OF DATE: 20260814
<periodOfReport>2026-06-30</periodOfReport>
<informationTable>
''' + _block('One', '111111111', 10, 100) + '''</informationTable>'''
    report = parse_13f_submission_top(text, limit=0)
    assert report.total_positions == 1
    assert report.positions == []
