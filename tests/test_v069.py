from pathlib import Path

from src.sec.thirteen_f import (
    information_table_filename,
    parse_13f_information_table_batch,
    parse_13f_submission_metadata,
)


def _row(i, put_call=None):
    option = f'<putCall>{put_call}</putCall>' if put_call else ''
    return f'''
    <infoTable>
      <nameOfIssuer>Issuer {i}</nameOfIssuer>
      <titleOfClass>COM</titleOfClass>
      <cusip>{i:09d}</cusip>
      <value>{1000 + i}</value>
      <shrsOrPrnAmt><sshPrnamt>{10 + i}</sshPrnamt></shrsOrPrnAmt>
      {option}
    </infoTable>
    '''


def test_v069_metadata_parser_does_not_materialize_holdings():
    text = '''
COMPANY CONFORMED NAME: TEST MANAGER LLC
CENTRAL INDEX KEY: 0001234567
FILED AS OF DATE: 20260415
CONFORMED PERIOD OF REPORT: 20260331
''' + ''.join(_row(i) for i in range(300))
    parsed = parse_13f_submission_metadata(text)
    assert parsed.manager_cik == '1234567'
    assert parsed.period_of_report == '2026-03-31'
    assert parsed.positions == []
    assert parsed.total_positions == 0


def test_v069_batch_parser_advances_cursor_in_bounded_raw_blocks():
    xml = '<informationTable>' + ''.join(
        _row(i, 'CALL' if i == 105 else None) for i in range(205)
    ) + '</informationTable>'

    first, cursor1, ordinal1, exhausted1 = parse_13f_information_table_batch(
        xml, cursor=0, block_ordinal=0, limit=100
    )
    assert len(first) == 100
    assert ordinal1 == 100
    assert not exhausted1

    second, cursor2, ordinal2, exhausted2 = parse_13f_information_table_batch(
        xml, cursor=cursor1, block_ordinal=ordinal1, limit=100
    )
    assert len(second) == 100
    assert ordinal2 == 200
    assert not exhausted2
    assert any(r['put_call'] == 'CALL' for r in second)

    third, _, ordinal3, exhausted3 = parse_13f_information_table_batch(
        xml, cursor=cursor2, block_ordinal=ordinal2, limit=100
    )
    assert len(third) == 5
    assert ordinal3 == 205
    assert exhausted3
    assert third[0]['row_ordinal'] == 200


def test_v069_finds_information_table_attachment_filename_without_body_parse():
    submission = '''
<DOCUMENT>
<TYPE>INFORMATION TABLE
<SEQUENCE>2
<FILENAME>holdings.xml
<DESCRIPTION>INFORMATION TABLE
<XML><informationTable></informationTable></XML>
</DOCUMENT>
'''
    assert information_table_filename(submission) == 'holdings.xml'


def test_v069_pipeline_uses_durable_scan_and_d1_aggregation():
    root = Path(__file__).parents[1]
    pipeline = (root / 'src' / 'pipeline_13f.py').read_text()
    migration = (root / 'migrations' / '0016_13f_staged_prepare.sql').read_text()

    assert 'RAW_SCAN_BATCH = 50' in pipeline
    assert 'parse_13f_submission_metadata(text)' in pipeline
    assert 'parse_13f_submission_top(text' not in pipeline
    assert "stage IN ('scan','assemble')" in pipeline
    assert 'GROUP BY cusip' in pipeline
    assert 'thirteen_f_raw_positions_stage' in migration
    assert 'DELETE FROM institutional_positions' in migration
    assert 'whale_score = 0' in migration
