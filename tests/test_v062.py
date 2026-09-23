from src.pipeline_13f import _normalize_company_name, _previous_quarter_end
from src.sec.thirteen_f import parse_13f_submission_top


def _block(name, cusip, value, shares):
    return f'''<infoTable><nameOfIssuer>{name}</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>{cusip}</cusip><value>{value}</value><shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt></shrsOrPrnAmt></infoTable>'''


def test_previous_quarter_end():
    assert _previous_quarter_end('2024-12-31') == '2024-09-30'
    assert _previous_quarter_end('2025-03-31') == '2024-12-31'
    assert _previous_quarter_end('2025-06-30') == '2025-03-31'
    assert _previous_quarter_end('2025-09-30') == '2025-06-30'


def test_complete_submission_scan_survives_odd_document_wrapping():
    # Even if DOCUMENT parsing is imperfect, whole-submission scanning must
    # still recover the information-table rows.
    text = '''\nCOMPANY CONFORMED NAME: TEST MANAGER\nCENTRAL INDEX KEY: 0000001234\nFILED AS OF DATE: 20260214\nCONFORMED PERIOD OF REPORT: 20251231\n<DOCUMENT>\n<TYPE>13F-HR\n<FILENAME>primary_doc.xml\n<XML><edgarSubmission><reportType>13F HOLDINGS REPORT</reportType></edgarSubmission></XML>\n</DOCUMENT>\n<DOCUMENT>\n<TYPE>INFORMATION TABLE\n<FILENAME>holdings.xml\n''' + '<informationTable>' + _block('APPLE INC', '037833100', 14440491, 57665) + '</informationTable>' + '''\n</DOCUMENT>\n'''
    report = parse_13f_submission_top(text)
    assert report.total_positions == 1
    assert report.positions[0].cusip == '037833100'


def test_name_normalization_common_13f_variants():
    assert _normalize_company_name('WELLS FARGO CO NEW') == _normalize_company_name('Wells Fargo & Company')
    assert _normalize_company_name('CHEVRON CORP NEW') == _normalize_company_name('Chevron Corporation')
    assert _normalize_company_name('CISCO SYS INC') == _normalize_company_name('Cisco Systems, Inc.')
    assert _normalize_company_name('ABBOTT LABS') == _normalize_company_name('Abbott Laboratories')
