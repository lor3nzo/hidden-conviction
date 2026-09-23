import asyncio
import json

import src.pipeline_13f as p13f
from src.sec.thirteen_f import (
    extract_information_table_document,
    has_information_table_document,
    parse_13f_submission_top,
)


def _canonical_submission():
    return '''
<SEC-DOCUMENT>0001760398-26-000003.txt
<SEC-HEADER>
ACCESSION NUMBER: 0001760398-26-000003
CONFORMED SUBMISSION TYPE: 13F-HR
CONFORMED PERIOD OF REPORT: 20251231
FILED AS OF DATE: 20260917
COMPANY CONFORMED NAME: WELLINGTON-ALTUS USA INC.
CENTRAL INDEX KEY: 0001760398
</SEC-HEADER>
<DOCUMENT>
<TYPE>13F-HR
<FILENAME>primary_doc.xml
<TEXT><XML>
<edgarSubmission><periodOfReport>12-31-2025</periodOfReport><reportType>13F HOLDINGS REPORT</reportType></edgarSubmission>
</XML></TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>INFORMATION TABLE
<SEQUENCE>2
<FILENAME>wellington4q25.xml
<TEXT><XML>
<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <ns1:infoTable>
    <ns1:nameOfIssuer>AIR PRODUCTS &amp; CHEMICALS INC</ns1:nameOfIssuer>
    <ns1:titleOfClass>Common Stock</ns1:titleOfClass>
    <ns1:cusip>009158106</ns1:cusip>
    <ns1:value>99302</ns1:value>
    <ns1:shrsOrPrnAmt><ns1:sshPrnamt>402</ns1:sshPrnamt></ns1:shrsOrPrnAmt>
  </ns1:infoTable>
  <ns1:infoTable>
    <ns1:nameOfIssuer>APPLE INC</ns1:nameOfIssuer>
    <ns1:titleOfClass>COM</ns1:titleOfClass>
    <ns1:cusip>037833100</ns1:cusip>
    <ns1:value>500000</ns1:value>
    <ns1:shrsOrPrnAmt><ns1:sshPrnamt>2000</ns1:sshPrnamt></ns1:shrsOrPrnAmt>
  </ns1:infoTable>
</ns1:informationTable>
</XML></TEXT>
</DOCUMENT>
'''


def test_canonical_information_table_document_fast_path():
    text = _canonical_submission()
    assert has_information_table_document(text)
    body, filename = extract_information_table_document(text)
    assert filename == 'wellington4q25.xml'
    assert '<ns1:infoTable>' in body

    report = parse_13f_submission_top(text, limit=250)
    assert report.manager_cik == '1760398'
    assert report.manager_name == 'WELLINGTON-ALTUS USA INC.'
    assert report.period_of_report == '2025-12-31'
    assert report.total_positions == 2
    assert [p.cusip for p in report.positions] == ['037833100', '009158106']
    assert report.positions[1].issuer_name == 'AIR PRODUCTS & CHEMICALS INC'
    assert report.positions[1].shares == 402


def test_canonical_url_can_be_derived_from_edgar_url_without_filing_cik():
    filing = {
        'cik': None,
        'accession_number': '0001760398-26-000003',
        'filing_url': 'https://www.sec.gov/Archives/edgar/data/1760398/000176039826000003/primary_doc.xml',
    }
    assert p13f._canonical_submission_url(filing) == (
        'https://www.sec.gov/Archives/edgar/data/1760398/000176039826000003/0001760398-26-000003.txt'
    )


def test_single_attachment_fallback_fetches_only_one_candidate(monkeypatch):
    info_xml = '''<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>10000000</value><shrsOrPrnAmt><sshPrnamt>40000</sshPrnamt></shrsOrPrnAmt></infoTable></informationTable>'''
    index_json = json.dumps({
        'directory': {
            'item': [
                {'name': 'primary_doc.xml', 'size': 5000},
                {'name': 'random.xml', 'size': 9000},
                {'name': 'holdings_table.xml', 'size': 1000},
                {'name': 'another_13f.xml', 'size': 800},
            ]
        }
    })
    calls = []

    async def fake_fetch(url, user_agent=None):
        calls.append(url)
        if url.endswith('/index.json'):
            return index_json
        if url.endswith('/holdings_table.xml'):
            return info_xml
        raise AssertionError(f'unexpected attachment fetch: {url}')

    monkeypatch.setattr(p13f, 'sec_fetch_text', fake_fetch)

    class Env:
        SEC_USER_AGENT = 'test@example.com'

    positions, total = asyncio.run(
        p13f._fallback_information_table(
            Env(),
            'https://www.sec.gov/Archives/edgar/data/1/abc/0000000000-00-000000.txt',
        )
    )
    assert total == 1
    assert positions[0].cusip == '037833100'
    assert len(calls) == 2  # index.json + exactly one attachment


def test_cpu_safe_submission_cap_is_explicit():
    assert p13f.MAX_CANONICAL_SUBMISSION_CHARS == 12_000_000
