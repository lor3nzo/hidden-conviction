from src.sec.eight_k import parse_8k_submission
from src.sec.thirteen_f import parse_13f_submission
from src.scoring.hcs import score_13f_position_details


def test_8k_negative_bankruptcy():
    text = '''
COMPANY CONFORMED NAME: TEST CO
CENTRAL INDEX KEY: 0000012345
FILED AS OF DATE: 20260920
ITEM INFORMATION: 1.03 Bankruptcy or Receivership
Item 1.03 Bankruptcy or Receivership
The company commenced bankruptcy proceedings.
'''
    event = parse_8k_submission(text)
    assert event.cik == '12345'
    assert '1.03' in event.item_numbers
    assert event.event_score < 0
    assert event.sentiment == 'negative'


def test_8k_positive_results():
    text = '''
COMPANY CONFORMED NAME: TEST CO
CENTRAL INDEX KEY: 0000012345
FILED AS OF DATE: 20260920
ITEM INFORMATION: 2.02 Results of Operations
Item 2.02 Results of Operations
The company reported record revenue and raised guidance.
'''
    event = parse_8k_submission(text)
    assert event.event_score > 0
    assert event.sentiment == 'positive'


def test_13f_parser_and_score():
    text = '''
COMPANY CONFORMED NAME: SAMPLE MANAGER LP
CENTRAL INDEX KEY: 0000099999
FILED AS OF DATE: 20260814
<periodOfReport>2026-06-30</periodOfReport>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>Example Corp</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>123456789</cusip>
    <value>250000000</value>
    <shrsOrPrnAmt><sshPrnamt>2000000</sshPrnamt></shrsOrPrnAmt>
  </infoTable>
</informationTable>
'''
    report = parse_13f_submission(text)
    assert report.manager_cik == '99999'
    assert report.period_of_report == '2026-06-30'
    assert len(report.positions) == 1
    assert report.positions[0].cusip == '123456789'
    score = score_13f_position_details(2_000_000, 1_000_000, 250_000_000, 3.0)
    assert score.total == 14.0
    assert score.share_change_pct == 100.0


def test_8k_ignores_exhibit_boilerplate_keywords():
    text = '''
COMPANY CONFORMED NAME: TEST CO
CENTRAL INDEX KEY: 0000012345
FILED AS OF DATE: 20260920
<DOCUMENT>
<TYPE>8-K
<TEXT>
<html><body>
<h2>Item 5.02 Director or Executive Change</h2>
<p>The board appointed Jane Doe as Chief Financial Officer.</p>
<h2>Item 9.01 Financial Statements and Exhibits</h2>
</body></html>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-10.1
<TEXT>
This exhibit contains boilerplate references to default, bankruptcy,
restatement, impairment and going concern remedies.
</TEXT>
</DOCUMENT>
'''
    event = parse_8k_submission(text)
    assert event.event_score > 0
    assert 'default' not in event.matched_keywords
    assert 'bankruptcy' not in event.matched_keywords


def test_8k_generic_other_event_does_not_penalize_ambiguous_default_word():
    text = '''
COMPANY CONFORMED NAME: TEST CO
CENTRAL INDEX KEY: 0000012345
FILED AS OF DATE: 20260920
<DOCUMENT>
<TYPE>8-K
<TEXT>
Item 8.01 Other Events
The agreement uses the default election unless the holder opts out.
Item 9.01 Financial Statements and Exhibits
</TEXT>
</DOCUMENT>
'''
    event = parse_8k_submission(text)
    assert event.event_score == 0
    assert event.sentiment == 'neutral'
